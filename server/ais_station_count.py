"""Count distinct vessels heard by the remote AIS station, per hour.

Runs on THIS machine and accepts AIS-catcher's TCP output from the Windows 10 station
(`AIS-catcher.exe ... -P 192.168.2.18 10111`). Answers one question: does the station meet
AISHub's "coverage of at least 10 vessels (average over the last 7 days)"?

Three deliberate choices, each of which cost something to get wrong before:

TCP, not UDP. The output of this script is a count judged against a threshold, so silent
datagram loss would be indistinguishable from genuinely hearing fewer vessels. UDP loss on a
wired LAN would probably be negligible, but "probably negligible" is not a property you want
underneath a number you are submitting to someone.

No pyais. The only field needed is the MMSI, which sits at bits 8-37 of every AIS payload
regardless of message type -- about fifteen lines of stdlib. `stt_proxy/ais_local.py` uses
pyais because it needs full decodes; this does not, and staying dependency-free means it runs
on any machine with a Python and needs no maintenance during a week-long run.

A heartbeat every minute, traffic or not. A gap in this log must be attributable. No
heartbeat means THIS process was not running; a heartbeat with zero messages means the
station really was quiet. Without that distinction, our own laptop sleeping looks exactly
like the station going down -- and the thing being evidenced is a 90% uptime figure, so an
unattributable gap would argue against the very claim the run exists to support. Same
reasoning as `_watch_silence` in the proxy: a feed that fails by going quiet is invisible
unless something is watching the clock.
"""

from __future__ import annotations

import argparse
import json
import math
import socket
import sys
import threading
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone

# Receiver position, needed for every range and bearing figure. Overridable on the command
# line; moving the antenna within the same building does not move this enough to matter.
RX_LAT, RX_LON = 52.111188, 4.292962
# Maas Center sits at bearing 250 deg / 30.0 km from the above -- inside the SW sector.
MAAS_LAT, MAAS_LON = 52.02, 3.88

# AISHub's published bar for API access, alongside >=90% uptime over the same window.
AISHUB_VESSEL_THRESHOLD = 10

HEARTBEAT_SEC = 60
# Hourly buckets are kept for longer than the 7-day window AISHub averages over, so a run
# that overshoots still has the whole window in memory.
RETAIN_HOURS = 24 * 9


def mmsi_of(sentence: str) -> int | None:
    """Extract the MMSI from one AIVDM/AIVDO sentence, or None if it carries no usable one.

    Every AIS message type puts the MMSI in the same place -- 6 bits of message type, 2 bits
    of repeat indicator, then 30 bits of MMSI -- so this needs no per-type knowledge. Only
    the first fragment of a multi-fragment message contains those bits; later fragments are
    the tail of a payload that started elsewhere and are skipped rather than misparsed.
    """
    if not sentence.startswith(("!AIVDM", "!AIVDO", "!BSVDM", "!ABVDM")):
        return None
    parts = sentence.split(",")
    if len(parts) < 6:
        return None
    try:
        fragment_number = int(parts[2])
    except ValueError:
        return None
    if fragment_number != 1:
        return None

    payload = parts[5]
    if len(payload) < 7:
        return None

    # Six-bit ASCII: subtract 48, and subtract a further 8 for the upper block, giving 0-63.
    value = 0
    for ch in payload[:7]:
        code = ord(ch) - 48
        if code > 40:
            code -= 8
        if code < 0 or code > 63:
            return None
        value = (value << 6) | code

    # 7 chars = 42 bits. Drop the low 4 to land on bit 37, then keep 30 bits.
    mmsi = (value >> 4) & 0x3FFFFFFF
    return mmsi or None


SECTORS = {0: "N", 45: "NE", 90: "E", 135: "SE",
           180: "S", 225: "SW", 270: "W", 315: "NW"}

# No terrestrial AIS reception approaches this. A position beyond it is a corrupted message
# that happened to pass CRC -- one such put a vessel 5,137 km away on 2026-08-11, with a
# name of binary garbage and MMSI 171003622, whose 171 is not even an allocated MID.
MAX_PLAUSIBLE_KM = 150.0


def mmsi_class(mmsi) -> str:
    """Classify an MMSI by its prefix, per ITU-R M.585.

    The range map exists to answer "how far away can this station hear a SHIP", and most of
    the transmitters on the band are not ships. On 2026-08-11 three of four sector records
    were set by non-vessels: a SAR aircraft at 20 km (airborne, so its horizon is enormous
    and says nothing about surface reception), a virtual AtoN that does not physically exist,
    and a shore base station. Counting those inflates the map precisely where it is being
    used to make a decision.
    """
    try:
        digits = f"{int(mmsi):09d}"
    except (TypeError, ValueError):
        return "invalid"
    if len(digits) != 9:
        return "invalid"
    if digits.startswith("111"):
        return "sar-aircraft"
    if digits.startswith("970"):
        return "ais-sart"
    if digits.startswith("972"):
        return "mob"
    if digits.startswith("974"):
        return "epirb"
    if digits.startswith("99"):
        return "aton"
    if digits.startswith("98"):
        return "craft"
    if digits.startswith("00"):
        return "coast-station"
    if digits.startswith("0"):
        return "ship-group"
    # A ship station is MID + 6 digits, and every allocated MID begins 2-7.
    return "ship" if digits[0] in "234567" else "invalid"


def great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def is_continuation(sentence: str) -> bool:
    """True for the 2nd or later fragment of a multi-fragment message.

    Those fragments are the tail of a payload that began in an earlier sentence, so they
    carry no MMSI bits and `mmsi_of` correctly declines them. Counting them as unparsed
    would report a perfectly healthy feed as partly corrupt -- type 5, which carries every
    vessel name, is two fragments, so they arrive constantly.
    """
    parts = sentence.split(",")
    if len(parts) < 3:
        return False
    try:
        return int(parts[2]) != 1
    except ValueError:
        return False


class Coverage:
    """Range reached per 45-degree sector, accumulated by polling the station's own decode.

    The counting path above is deliberately position-blind, but a count alone cannot answer
    the question that actually matters -- whether this station can ever hear the Maas
    approach, which lies at bearing 250 degrees. A single 8-minute snapshot on 2026-08-11
    showed 13.9 km to the west but only 4.5 km to the south-west, and that is ambiguous
    between an obstruction and simply no ships being on that bearing at the time. Only
    accumulating over hours separates the two.

    Polling /ships.json rather than decoding positions from the NMEA stream is deliberate:
    AIS-catcher has already decoded them correctly, and re-implementing type-1/18/19 position
    unpacking here would add a second, less-tested decoder for no gain.
    """

    def __init__(self, station: str, rx_lat: float, rx_lon: float) -> None:
        self.url = f"http://{station}/ships.json"
        self.rx = (rx_lat, rx_lon)
        self.max_km: dict[int, float] = defaultdict(float)
        # Who and when, for every sector maximum. A range figure with no vessel attached
        # cannot be audited after the fact, which is exactly how a retracted 69.5 km claim
        # survived here once -- it came from a stale cached position rather than from where
        # the vessel was when heard. `last_signal` is AIS-catcher's seconds-since-heard, so
        # a large value on a record-breaking range is the signal to distrust it.
        self.max_detail: dict[int, dict] = {}
        self.seen: dict[int, int] = defaultdict(int)
        self.rejected: dict[str, int] = defaultdict(int)
        self.polls = 0
        self.failures = 0
        # The outcome of the MOST RECENT poll, which the running totals above cannot express:
        # a station that failed once an hour ago and has answered ever since carries the same
        # non-zero `failures` as one that is unreachable right now. None until the first poll,
        # because "never asked" is a third state and the dashboard shows an unlit lamp for it.
        self.last_poll_ok: bool | None = None
        self.last_poll_at: datetime | None = None
        self.best_toward_maas = 0.0
        self._lock = threading.Lock()

    def poll(self) -> str | None:
        """One snapshot. Returns an error string rather than raising: a station that has gone
        away must not take the heartbeat thread with it, since the heartbeat is the only
        record distinguishing 'we were down' from 'it was quiet'."""
        try:
            with urllib.request.urlopen(self.url, timeout=10) as response:
                ships = json.load(response).get("ships", [])
        except Exception as exc:
            with self._lock:
                self.failures += 1
                # Timestamped even in failure: "last tried at 10:04, no answer" is what tells
                # the watchkeeper how long the station has been unreachable.
                self.last_poll_ok = False
                self.last_poll_at = datetime.now(timezone.utc)
            return f"{type(exc).__name__}: {exc}"

        with self._lock:
            self.polls += 1
            self.last_poll_ok = True
            self.last_poll_at = datetime.now(timezone.utc)
            for ship in ships:
                lat, lon = ship.get("lat"), ship.get("lon")
                if not lat or not lon:
                    continue
                bearing = bearing_deg(self.rx[0], self.rx[1], lat, lon)
                km = great_circle_km(self.rx[0], self.rx[1], lat, lon)
                sector = int(bearing // 45) * 45

                # Excluded rather than silently dropped: a map that quietly discards things
                # is as misleading as one that counts the wrong things.
                kind = mmsi_class(ship.get("mmsi"))
                if km > MAX_PLAUSIBLE_KM:
                    self.rejected["implausible-range"] += 1
                    continue
                if kind != "ship":
                    self.rejected[kind] += 1
                    continue

                self.seen[sector] += 1
                if km > self.max_km[sector]:
                    self.max_km[sector] = km
                    self.max_detail[sector] = {
                        "km": round(km, 2),
                        "bearing": round(bearing),
                        "mmsi": ship.get("mmsi"),
                        "name": (ship.get("shipname") or "").strip() or None,
                        # Seconds since that vessel was last heard, as the station reports
                        # it. A fresh record-breaker is believable; a stale one is not.
                        "last_signal": ship.get("last_signal"),
                        # False means a genuine received position rather than one AIS-catcher
                        # estimated -- an approximated position proves nothing about range.
                        "approx": ship.get("approx"),
                        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    }
                # 225-270 is the arc containing Maas Center; this is the number that decides
                # whether local AIS can ever contribute to identification.
                if sector == 225 and km > self.best_toward_maas:
                    self.best_toward_maas = km
        return None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "polls": self.polls,
                "poll_failures": self.failures,
                "last_poll_ok": self.last_poll_ok,
                "last_poll_at": (self.last_poll_at.isoformat(timespec="seconds")
                                 if self.last_poll_at else None),
                "max_km_by_sector": {SECTORS[k]: round(v, 2)
                                     for k, v in sorted(self.max_km.items())},
                "max_detail_by_sector": {SECTORS[k]: v
                                         for k, v in sorted(self.max_detail.items())},
                "best_km_toward_maas": round(self.best_toward_maas, 2),
                "excluded_from_range_map": dict(sorted(self.rejected.items())),
            }


class Counter:
    def __init__(self, log_path: str) -> None:
        self._lock = threading.Lock()
        self._hours: dict[str, set[int]] = defaultdict(set)
        self._messages: dict[str, int] = defaultdict(int)
        self._unparsed = 0
        self._continuations = 0
        self._connections = 0
        self._connected = 0
        self._log = open(log_path, "a", encoding="utf-8", buffering=1)
        self._started = time.time()

    @staticmethod
    def _hour_key(when: datetime | None = None) -> str:
        when = when or datetime.now(timezone.utc)
        return when.strftime("%Y-%m-%dT%H")

    def record(self, sentence: str) -> None:
        mmsi = mmsi_of(sentence)
        key = self._hour_key()
        with self._lock:
            self._messages[key] += 1
            if mmsi is not None:
                self._hours[key].add(mmsi)
            elif is_continuation(sentence):
                self._continuations += 1
            else:
                self._unparsed += 1

    def connection_opened(self, peer: str) -> None:
        with self._lock:
            self._connections += 1
            self._connected += 1
        self._write({"type": "connect", "peer": peer})

    def connection_closed(self, peer: str, reason: str) -> None:
        with self._lock:
            self._connected -= 1
        self._write({"type": "disconnect", "peer": peer, "reason": reason})

    def rolling_distinct(self, hours: int) -> int:
        """Distinct MMSIs across the last `hours` clock hours."""
        now = datetime.now(timezone.utc)
        keys = [self._hour_key(now - timedelta(hours=n)) for n in range(hours)]
        with self._lock:
            seen: set[int] = set()
            for key in keys:
                seen |= self._hours.get(key, set())
            return len(seen)

    def hourly_series(self) -> list[tuple[str, int, int]]:
        with self._lock:
            return [(key, len(self._hours[key]), self._messages[key])
                    for key in sorted(self._hours)]

    def _prune(self) -> None:
        cutoff = self._hour_key(datetime.now(timezone.utc) - timedelta(hours=RETAIN_HOURS))
        with self._lock:
            for key in [k for k in self._hours if k < cutoff]:
                del self._hours[key]
                self._messages.pop(key, None)

    def _write(self, record: dict) -> None:
        record["t"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._log.write(json.dumps(record) + "\n")

    def heartbeat(self, extra: dict | None = None) -> dict:
        key = self._hour_key()
        with self._lock:
            vessels = len(self._hours.get(key, set()))
            messages = self._messages.get(key, 0)
            connected = self._connected
            unparsed = self._unparsed
            continuations = self._continuations
        record = {
            "type": "heartbeat",
            "hour": key,
            "connected": connected > 0,
            "vessels_this_hour": vessels,
            "messages_this_hour": messages,
            "unparsed_total": unparsed,
            "continuation_fragments": continuations,
            "distinct_24h": self.rolling_distinct(24),
            "distinct_7d": self.rolling_distinct(24 * 7),
        }
        if extra:
            record.update(extra)
        self._write(record)
        self._prune()
        return record


def serve_client(conn: socket.socket, peer: str, counter: Counter) -> None:
    """Read newline-delimited NMEA from one AIS-catcher connection until it closes."""
    reason = "clean close"
    buffer = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buffer += chunk
            # A TCP read boundary lands wherever it likes, so the tail of the buffer is held
            # back until its newline arrives rather than being parsed as a short sentence.
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                text = line.decode("ascii", errors="replace").strip()
                if text:
                    counter.record(text)
    except OSError as exc:
        reason = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()
        counter.connection_closed(peer, reason)


def heartbeat_loop(counter: Counter, threshold: int, stop: threading.Event,
                   coverage: Coverage | None = None) -> None:
    last_hour = None
    poll_error_logged = False
    while not stop.wait(HEARTBEAT_SEC):
        extra = None
        if coverage is not None:
            error = coverage.poll()
            if error and not poll_error_logged:
                poll_error_logged = True
                print(f"  [coverage] poll failed: {error} "
                      f"(counting continues; only the range map is affected)", flush=True)
            elif not error:
                poll_error_logged = False
            extra = coverage.snapshot()
        record = counter.heartbeat(extra)
        if last_hour is not None and record["hour"] != last_hour:
            for key, vessels, messages in counter.hourly_series():
                if key == last_hour:
                    verdict = "OK" if vessels >= threshold else "BELOW THRESHOLD"
                    print(f"  [hour {key}Z complete] {vessels} vessels, "
                          f"{messages} messages -- {verdict}", flush=True)
        last_hour = record["hour"]

        state = "connected" if record["connected"] else "NO STATION CONNECTED"
        toward_maas = ""
        if coverage is not None and record.get("polls"):
            toward_maas = f"  SW_max={record['best_km_toward_maas']:.1f}km"
        print(f"{record['t']}  {state}  hour={record['hour']}Z  "
              f"vessels={record['vessels_this_hour']}  "
              f"msgs={record['messages_this_hour']}  "
              f"24h_distinct={record['distinct_24h']}{toward_maas}", flush=True)


def summarise(counter: Counter, threshold: int, coverage: Coverage | None = None) -> None:
    series = counter.hourly_series()
    print("\n=== hourly ===", flush=True)
    for key, vessels, messages in series:
        print(f"{key}Z  vessels={vessels:4d}  messages={messages:6d}", flush=True)

    complete = [vessels for _, vessels, _ in series[1:-1]] if len(series) > 2 else []
    print("\n=== against the AISHub bar ===", flush=True)
    print(f"threshold                : {threshold} vessels", flush=True)
    print(f"distinct, last 24h       : {counter.rolling_distinct(24)}", flush=True)
    print(f"distinct, last 7d        : {counter.rolling_distinct(24 * 7)}", flush=True)
    if complete:
        worst = min(complete)
        mean = sum(complete) / len(complete)
        # The first and last hours are partial, so they would understate the rate through no
        # fault of the station; only whole hours are eligible for a pass/fail read.
        print(f"whole hours measured     : {len(complete)}", flush=True)
        print(f"mean vessels per hour    : {mean:.1f}", flush=True)
        print(f"worst hour               : {worst} "
              f"({'clears' if worst >= threshold else 'BELOW'} the bar)", flush=True)
    else:
        print("not enough whole hours yet for a per-hour verdict", flush=True)

    if coverage is None:
        return
    snap = coverage.snapshot()
    print(f"\n=== coverage by sector, SHIP STATIONS ONLY ({snap['polls']} polls, "
          f"{snap['poll_failures']} failed) ===", flush=True)
    excluded = snap.get("excluded_from_range_map") or {}
    if excluded:
        print("excluded: " + ", ".join(f"{k}={v}" for k, v in excluded.items()), flush=True)
    for name in ("N", "NE", "E", "SE", "S", "SW", "W", "NW"):
        reached = snap["max_km_by_sector"].get(name, 0.0)
        detail = snap["max_detail_by_sector"].get(name)
        marker = "  <- toward Maas Center (250 deg)" if name == "SW" else ""
        if detail:
            who = f"{detail['mmsi']} {detail['name'] or '(unnamed)'}"
            age = detail["last_signal"]
            age_text = f"heard {age}s before the poll" if age is not None else "age unknown"
            suspect = " [APPROXIMATED POSITION -- distrust]" if detail.get("approx") else ""
            print(f"{name:<3} max {reached:6.2f} km  {who:<28} {age_text}"
                  f"{suspect}{marker}", flush=True)
        else:
            print(f"{name:<3} max {reached:6.2f} km{marker}", flush=True)
    maas_km = great_circle_km(RX_LAT, RX_LON, MAAS_LAT, MAAS_LON)
    best = snap["best_km_toward_maas"]
    print(f"\nMaas Center is {maas_km:.1f} km on that bearing; the approach area proper "
          f"starts ~15 km out.", flush=True)
    print(f"best reached toward it   : {best:.2f} km", flush=True)
    if best < 5:
        print("verdict: unchanged from the pre-move ~4 km shell -- either the building "
              "blocks that arc, or no ships were on it. More hours separate the two.",
              flush=True)
    elif best < 15:
        print("verdict: improved on that bearing but still short of the approach area.",
              flush=True)
    else:
        print("verdict: REACHING the approach area -- local AIS can now contribute to "
              "identification.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=10111,
                        help="TCP port to listen on (default: 10111)")
    parser.add_argument("--log", default="ais-station-count.jsonl",
                        help="append-only JSONL log (default: ais-station-count.jsonl)")
    parser.add_argument("--threshold", type=int, default=AISHUB_VESSEL_THRESHOLD,
                        help=f"vessels/hour to clear (default: {AISHUB_VESSEL_THRESHOLD})")
    parser.add_argument("--station", default=None, metavar="HOST:PORT",
                        help="poll AIS-catcher's /ships.json here once a minute to build a "
                             "range-by-bearing map (e.g. 192.168.2.1:8100). Counting works "
                             "without it; the coverage map does not.")
    parser.add_argument("--lat", type=float, default=RX_LAT, help="receiver latitude")
    parser.add_argument("--lon", type=float, default=RX_LON, help="receiver longitude")
    args = parser.parse_args()

    counter = Counter(args.log)
    coverage = Coverage(args.station, args.lat, args.lon) if args.station else None
    stop = threading.Event()

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # Unlike the proxy's UDP listener, reuse is wanted here: a restart after Ctrl+C must not
    # be blocked for a TIME_WAIT interval in the middle of a measurement.
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("0.0.0.0", args.port))
    listener.listen(4)
    # Windows will not deliver KeyboardInterrupt to a thread blocked in accept(): the signal
    # is recorded but the interpreter never regains control to run the handler, so Ctrl+C
    # does nothing and the run can only be ended by killing the process -- which skips the
    # summary. A timeout hands control back once a second so the signal can land.
    listener.settimeout(1.0)

    print(f"listening on 0.0.0.0:{args.port}, logging to {args.log}", flush=True)
    print(f"point the station at it:  -P <this-pc-ip> {args.port}", flush=True)
    print("waiting for AIS-catcher to connect; Ctrl+C to stop and summarise\n", flush=True)

    if coverage is not None:
        print(f"polling {coverage.url} once a minute for the range map", flush=True)

    threading.Thread(target=heartbeat_loop, args=(counter, args.threshold, stop, coverage),
                     daemon=True, name="heartbeat").start()

    try:
        while True:
            try:
                conn, addr = listener.accept()
            except socket.timeout:
                continue
            peer = f"{addr[0]}:{addr[1]}"
            counter.connection_opened(peer)
            print(f"station connected from {peer}", flush=True)
            threading.Thread(target=serve_client, args=(conn, peer, counter),
                             daemon=True, name=f"client-{peer}").start()
    except KeyboardInterrupt:
        print("\nstopping...", flush=True)
        stop.set()
        listener.close()
        summarise(counter, args.threshold, coverage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
