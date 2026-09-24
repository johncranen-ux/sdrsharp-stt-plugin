"""adsb.fi as the live aircraft source for airband flight identification.

Polls adsb.fi's free point/radius endpoint and keeps a snapshot of aircraft currently within
range. Unlike ais.py's cache, this is a full REPLACE on every poll, not an accumulating merge
-- an aircraft is only relevant for the short window it is actually within radio range, so
there is no AIS-style "still in scope from an hour ago" question to answer.

Checked live before choosing this source (2026-09-08): airplanes.live's documented no-key
access now returns HTTP 403 asking for manual approval; adsb.lol did not respond at all;
adsb.one is behind a Cloudflare JS challenge a server-side poller cannot pass. adsb.fi is the
only one of four candidates that actually works.
"""

import collections
import datetime
import json
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

API_HOST = "https://opendata.adsb.fi"

# Mirrors ais.py's AIS_SOURCE: "off" disables the feed entirely. adsb.fi has no equivalent of
# AIS_SOURCE=aishub/aisstream to choose between -- it is the only source that actually works
# (see the module docstring) -- so this only needs one real value plus "off". Unlike AIS, this
# poller had no switch at all until the whole-branch review (finding 5): it always ran, for
# every user of this now-public repo, including anyone who never uses airband.
def _resolve_source() -> str:
    return os.environ.get("ADSB_SOURCE", "adsbfi").strip().lower()


ADSB_SOURCE = _resolve_source()

# Lowest sane poll interval: a malformed/typo'd ADSB_POLL_SEC (or a deliberately tiny one)
# must not tight-loop an external API.
MIN_POLL_SEC = 5


def _resolve_float(env_var: str, default: float) -> float:
    """A float env var, or `default` on anything that doesn't parse.

    whisper-proxy.py imports this module at load time, so a bad env var here must not raise
    -- that would take down the entire proxy over a typo in a setting that only affects flight
    identification. Same reasoning as aishub.py's _resolve_poll_sec (review finding 6).
    """
    try:
        return float(os.environ.get(env_var, str(default)))
    except (TypeError, ValueError):
        return default


def _resolve_poll_sec(default: int = 15) -> int:
    """Seconds between polls, never below MIN_POLL_SEC whatever the environment says."""
    try:
        wanted = int(os.environ.get("ADSB_POLL_SEC", str(default)))
    except (TypeError, ValueError):
        wanted = default
    return max(wanted, MIN_POLL_SEC)


# Centred to cover Schiphol, Rotterdam, and the Scheveningen coastal corridor -- the exact
# point tested live during design (2026-09-08), which returned 59 real aircraft including
# KLM281, RYR37DV, AFR16JN, EZY85FV and PHVSY (a Dutch-registered light aircraft).
POINT_LAT     = _resolve_float("ADSB_LAT", 52.15)
POINT_LON     = _resolve_float("ADSB_LON", 4.3)
POINT_DIST_NM = _resolve_float("ADSB_DIST_NM", 40)

# No published rate limit was found for adsb.fi during design (unlike AISHub's documented
# "once per minute"), so this is a conservative starting point, not an enforced server fact.
# Aircraft move at 150-250 m/s on approach -- far faster than ships -- so it needs to be much
# more frequent than AISHub's 900s.
POLL_SEC = _resolve_poll_sec(15)


class AdsbError(Exception):
    """The response was not a real observation. The cache must not be updated from it."""


def parse_response(payload: bytes) -> list[dict]:
    """The aircraft in an adsb.fi response, or raise AdsbError.

    An empty list is a real answer -- a radius that genuinely holds no traffic right now.
    A missing or wrongly-shaped `aircraft` key, or unparseable content, is "we learned
    nothing", which must never reach the cache disguised as "there is no traffic".
    """
    try:
        body = json.loads(payload.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError) as exc:
        raise AdsbError(f"response was not JSON: {exc}") from exc

    if not isinstance(body, dict):
        raise AdsbError(f"unexpected response shape: {type(body).__name__}")

    aircraft = body.get("aircraft")
    if not isinstance(aircraft, list):
        raise AdsbError(f"response carried no aircraft list: {type(aircraft).__name__}")

    return aircraft


def map_aircraft(ac: dict) -> dict | None:
    """One adsb.fi aircraft record as the fields this module caches, or None if unusable.

    hex is the cache key (always present and stable for the aircraft's session, unlike
    `flight` which can be blank or padded) -- the same reasoning aishub.py's map_ship uses
    keying on MMSI rather than NAME.
    """
    hex_id = str(ac.get("hex") or "").strip()
    if not hex_id:
        return None
    return {
        "hex": hex_id,
        "flight": str(ac.get("flight") or "").strip(),
        "r": ac.get("r"),
        "t": ac.get("t"),
        "alt_baro": ac.get("alt_baro"),
        "gs": ac.get("gs"),
        "track": ac.get("track"),
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "squawk": ac.get("squawk"),
        # The autopilot's SELECTED values (adsb.fi carries them for roughly half the traffic,
        # measured 2026-09-24: 39/81 had nav_altitude_mcp). A controller's "descend seven
        # thousand" shows up here seconds later -- the evidence flight_attribution's echo clue
        # is built on. Dropped until 2026-09-24, which is why the 09-10 log cannot replay it.
        "nav_altitude_mcp": ac.get("nav_altitude_mcp"),
        "nav_heading": ac.get("nav_heading"),
        "nav_qnh": ac.get("nav_qnh"),
        "baro_rate": ac.get("baro_rate"),
    }


def build_url(lat: float, lon: float, dist_nm: float) -> str:
    return f"{API_HOST}/api/v2/lat/{lat}/lon/{lon}/dist/{dist_nm}"


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": "sdrsharp-stt-proxy/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise AdsbError(f"fetch failed: {exc}") from exc


_cache_lock = threading.Lock()
_aircraft_cache: dict[str, dict] = {}


def current_aircraft() -> list[dict]:
    """A snapshot of the live cache. Safe to iterate without holding the lock yourself."""
    with _cache_lock:
        return list(_aircraft_cache.values())


def poll_once(lat: float, lon: float, dist_nm: float, fetch=None,
              record: bool = True, now: float | None = None) -> int:
    """One poll. Returns aircraft cached, or raises AdsbError having changed nothing.

    Every aircraft is validated and mapped in a first pass, entirely before the cache is
    replaced -- a malformed element anywhere in the response therefore raises AdsbError with
    the cache exactly as it was, the same guarantee aishub.py's poll_once gives for ships.
    """
    aircraft = parse_response((fetch or _fetch)(build_url(lat, lon, dist_nm)))

    mapped: dict[str, dict] = {}
    for ac in aircraft:
        if not isinstance(ac, dict):
            raise AdsbError(f"malformed aircraft record: {type(ac).__name__}")
        fields = map_aircraft(ac)
        if fields is None:
            continue
        fields["last_seen"] = time.time() if now is None else now
        mapped[fields["hex"]] = fields

    with _cache_lock:
        _aircraft_cache.clear()
        _aircraft_cache.update(mapped)

    if record:
        stamp = time.time() if now is None else now
        _record_snapshot([{k: v for k, v in f.items() if k != "last_seen"}
                          for f in mapped.values()], stamp)

    return len(mapped)


# -- snapshot history: what flight_attribution's stage 2 looks back over ---------------

SNAPSHOT_LOG_DIR = Path(os.environ.get("ADSB_SNAPSHOT_DIR", "").strip()
                        or Path(__file__).resolve().parent.parent / "logs")

# 5 minutes at the 15 s default -- stage 2 needs t-10 s .. t+60 s, plus slack for a slow loop.
_RING_LEN = 20
_ring_lock = threading.Lock()
_ring: collections.deque = collections.deque(maxlen=_RING_LEN)
_log_warned = False


def reset_snapshot_ring() -> None:
    """Forget every in-memory snapshot. What a restart does; tests call it for isolation."""
    with _ring_lock:
        _ring.clear()


def snapshot_log_path(day: str) -> Path:
    return Path(SNAPSHOT_LOG_DIR) / f"adsb-{day}.jsonl"


def _append_snapshot_log(snapshot: dict) -> None:
    """One line per poll, the same shape as the 09-10 side-car bench_flight_identify reads."""
    stamp = datetime.datetime.fromtimestamp(snapshot["t"]).astimezone()
    path = snapshot_log_path(stamp.date().isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"t": stamp.isoformat(), "aircraft": snapshot["aircraft"]},
                      ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _record_snapshot(aircraft: list[dict], now: float) -> None:
    """Ring first, log second; a log failure is reported once and never fails the poll."""
    global _log_warned
    snapshot = {"t": now, "aircraft": aircraft}
    with _ring_lock:
        _ring.append(snapshot)
    try:
        _append_snapshot_log(snapshot)
    except Exception as exc:
        if not _log_warned:
            print(f"[adsb.fi] could not write the snapshot log: {exc}", flush=True)
            _log_warned = True


def _snapshots_from_log(t0: float, t1: float) -> list[dict]:
    days = {datetime.datetime.fromtimestamp(t).astimezone().date().isoformat()
            for t in (t0, t1)}
    out = []
    for day in sorted(days):
        path = snapshot_log_path(day)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                t = datetime.datetime.fromisoformat(row["t"]).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            if t0 <= t <= t1:
                out.append({"t": t, "aircraft": row.get("aircraft") or []})
    return sorted(out, key=lambda s: s["t"])


def snapshots_between(t0: float, t1: float) -> list[dict]:
    """Every recorded snapshot with t0 <= t <= t1, oldest first.

    The ring answers when it reaches back to t0; otherwise (a restart emptied it, or t0 is
    older than five minutes) the daily log is read instead, so a restart between stage 1 and
    stage 2 costs evidence only if the log is missing too.
    """
    with _ring_lock:
        ring = list(_ring)
    if ring and ring[0]["t"] <= t0:
        return [s for s in ring if t0 <= s["t"] <= t1]
    return _snapshots_from_log(t0, t1)


# -- what the feed can be asked about itself, same shape as aishub.py's feed_status ----

_feed_lock = threading.Lock()
_last_ok_at: float | None = None
_last_error_at: float | None = None
_last_error: str | None = None
_last_count: int | None = None
_consecutive_failures = 0


def reset_feed_state() -> None:
    """Back to "never polled". For tests; nothing in the running proxy calls this."""
    global _last_ok_at, _last_error_at, _last_error, _last_count, _consecutive_failures
    with _feed_lock:
        _last_ok_at = _last_error_at = _last_error = _last_count = None
        _consecutive_failures = 0


def feed_status() -> dict:
    with _feed_lock:
        return {
            "last_ok_at": _last_ok_at,
            "last_error_at": _last_error_at,
            "last_error": _last_error,
            "last_count": _last_count,
            "consecutive_failures": _consecutive_failures,
            "poll_sec": POLL_SEC,
        }


def _record_success(count: int) -> None:
    """Update feed state for one successful poll.

    Deliberately quieter than aishub.py's unconditional per-poll line: AISHub polls every
    900s, so one line a poll is not noise, but adsb.fi polls every ~15s and a line every poll
    would flood the console. Print only on the first successful poll ever, when the aircraft
    count actually changes from the previous poll, or when recovering from a run of failures
    (mirroring aishub.py's "recovered after N failed poll(s)" line) -- review finding 7.
    """
    global _last_ok_at, _last_count, _consecutive_failures
    with _feed_lock:
        recovered = _consecutive_failures
        previous_count = _last_count
        _last_ok_at = time.time()
        _last_count = count
        _consecutive_failures = 0
    if recovered:
        print(f"[adsb.fi] recovered after {recovered} failed poll(s)", flush=True)
    if previous_count is None or count != previous_count:
        print(f"[adsb.fi] {count} aircraft", flush=True)


def _record_failure(reason: str) -> None:
    global _last_error_at, _last_error, _consecutive_failures
    with _feed_lock:
        _last_error_at = time.time()
        _last_error = reason
        _consecutive_failures += 1
        failures = _consecutive_failures
    # Same pacing as aishub.py: say it early, then stop repeating so a long outage doesn't
    # drown the console.
    if failures <= 3 or failures % 20 == 0:
        print(f"[adsb.fi] poll failed ({failures}): {reason}. Cache left untouched.",
              flush=True)


def poll_and_record(lat: float, lon: float, dist_nm: float, fetch=None) -> None:
    """One poll and its consequences for the feed's own state. Never raises.

    Every exception is caught here because the caller is a daemon thread with nothing above
    it -- an error that escaped would end polling silently and leave the cache frozen.
    """
    try:
        _record_success(poll_once(lat, lon, dist_nm, fetch=fetch))
    except AdsbError as exc:
        _record_failure(str(exc))
    except Exception as exc:
        _record_failure(f"{type(exc).__name__}: {exc}")


def poll_loop(lat: float, lon: float, dist_nm: float) -> None:
    """Poll forever. Daemon-thread entry point; never raises."""
    print(f"[adsb.fi] polling ({lat}, {lon}) within {dist_nm}nm every {POLL_SEC}s", flush=True)
    while True:
        poll_and_record(lat, lon, dist_nm)
        time.sleep(POLL_SEC)


def start(lat: float, lon: float, dist_nm: float) -> None:
    threading.Thread(target=poll_loop, args=(lat, lon, dist_nm), daemon=True).start()
