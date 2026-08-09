"""Local AIS reception: AIS-catcher's UDP envelope into the shared recorder.

aisstream has delivered nothing since 2026-08-05 and the upstream issue describing that
exact symptom has been open since 2026-03-13 with no maintainer response. This reads a
locally-received feed instead.

Verified live against the real dongle 2026-08-09: `-o 5` ("JSON Full") only ever affected
AIS-catcher's own screen output, not the UDP payload -- with `-o 5` AND `JSON on` together
the envelope was still sparse. Over UDP, `JSON on` gives a thin envelope wrapping the raw
NMEA sentence(s) in `msg["nmea"]`; the full decode AIS-catcher shows on screen never reaches
this process. So this module decodes the NMEA itself, using `pyais`, rather than trusting
decoded fields that were never actually there.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

from pyais import decode as ais_decode

from . import ais

# Message types that describe a VESSEL. Everything else is ignored, and two exclusions are
# load-bearing: type 4 is a shore base station, and type 21 is an aid to navigation -- a
# buoy, which carries a `name` and would otherwise enter the name-keyed cache and become a
# candidate for vessel name matching.
_POSITION_TYPES = {1, 2, 3, 18, 19}
_STATIC_TYPES   = {5, 19, 24}


def parse_message(msg: dict) -> dict | None:
    """AIS-catcher JSON envelope -> recorder fields, or None if the message should be
    ignored. `msg["nmea"]` is decoded with `pyais`; see the module docstring for why."""
    # AIS-catcher still decodes a sentence whose checksum failed and flags it with `error`.
    # Observed 2026-08-09: a corrupted checksum produced a full, plausible decode. A wrong
    # vessel name out of a corrupt payload is the failure that costs most here. Guard checks
    # truthiness, not presence: if AIS-catcher uses `error: 0` to mean "no error", presence
    # check would silently reject every clean message and kill the feed.
    if msg.get("error"):
        return None

    # The envelope's own `type` mirrors the AIS message type (confirmed live 2026-08-09)
    # and is cheap to check, so types we're never going to keep -- a type 4 shore station,
    # a type 21 aid to navigation (a buoy, which carries a `name` and would otherwise enter
    # the name-keyed cache) -- are rejected before spending a decode on them.
    msg_type = msg.get("type")
    if msg_type not in _POSITION_TYPES and msg_type not in _STATIC_TYPES:
        return None

    # AIS-catcher groups every part of a multi-part sentence (a type 5's two fragments,
    # say) into this one list in a single datagram -- verified live 2026-08-09, where a
    # type 5 arrived as one datagram carrying both parts. So no reassembly is needed here:
    # the whole list goes to pyais in one call.
    nmea = msg.get("nmea")
    if not nmea:
        return None

    try:
        decoded = ais_decode(*nmea)
    except Exception:
        # pyais raises its own exception hierarchy (AISBaseException and subclasses) for a
        # bad checksum, an unrecognised message type, a missing fragment, and so on, and
        # plain TypeError for a shape it can't even attempt (e.g. a non-string list entry).
        # All of them mean the same thing here: this sentence can't be used. Caught broadly
        # so a malformed or unsupported datagram becomes a dropped message, not an
        # exception -- the listener's own catch-all exists for handler bugs, not for
        # ordinary bad input turning into a logged error every time it occurs.
        return None

    mmsi = str(msg.get("mmsi") or getattr(decoded, "mmsi", "") or "").strip()
    if not mmsi:
        return None

    fields: dict = {"mmsi": mmsi}

    if msg_type in _STATIC_TYPES:
        name = (getattr(decoded, "shipname", "") or "").strip()
        if name:
            fields["name"] = name
        callsign = (getattr(decoded, "callsign", "") or "").strip()
        if callsign:
            fields["callsign"] = callsign
        destination = (getattr(decoded, "destination", "") or "").strip()
        if destination:
            fields["destination"] = destination

        imo = getattr(decoded, "imo", None)
        if imo is not None:
            fields["imo"] = imo
        # pyais's ship type is an IntEnum (e.g. ShipType.OtherType_NoAdditionalInformation);
        # stored as a plain int since nothing downstream expects the enum wrapper.
        ship_type = getattr(decoded, "ship_type", None)
        if ship_type is not None:
            fields["type"] = int(ship_type)
        draught = getattr(decoded, "draught", None)
        if draught is not None:
            fields["draught"] = draught

        to_bow, to_stern = getattr(decoded, "to_bow", None), getattr(decoded, "to_stern", None)
        if to_bow is not None and to_stern is not None:
            fields["length"] = (to_bow + to_stern) or None
        to_port, to_starboard = (getattr(decoded, "to_port", None),
                                  getattr(decoded, "to_starboard", None))
        if to_port is not None and to_starboard is not None:
            fields["beam"] = (to_port + to_starboard) or None

    lat, lon = getattr(decoded, "lat", None), getattr(decoded, "lon", None)
    if lat is not None and lon is not None:
        fields["latitude"] = lat
        fields["longitude"] = lon
        speed = getattr(decoded, "speed", None)
        if speed is not None:
            fields["sog"] = speed
        course = getattr(decoded, "course", None)
        if course is not None:
            fields["cog"] = course
        heading = getattr(decoded, "heading", None)
        if heading is not None:
            fields["heading"] = heading

    return fields


AIS_LOCAL_ENABLED  = os.environ.get("AIS_LOCAL_ENABLED", "on").strip().lower() != "off"
AIS_LOCAL_UDP_PORT = int(os.environ.get("AIS_LOCAL_UDP_PORT", "10110"))

# Owned by this module and read through it, never via an imported name: it is written by
# the listener thread.
_stats: dict = {"messages": 0, "last_message_at": None, "rejected": 0, "errors": 0}
_stats_lock = threading.Lock()

_MALFORMED_LOG_LIMIT = 5
_malformed_logged = 0

_LISTENER_ERROR_LOG_LIMIT = 5
_listener_errors_logged = 0


def stats() -> dict:
    with _stats_lock:
        return dict(_stats)


def bind(port: int) -> socket.socket:
    """A UDP socket on loopback, WITHOUT SO_REUSEADDR.

    Deliberately not reusable. ThreadingHTTPServer sets allow_reuse_address, and a second
    proxy once bound alongside the first on the same port, silently took it, and left the
    original running as a zombie -- so restarting quietly did nothing. Binding a port
    someone else owns must fail loudly here.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", port))
    return sock


def handle_datagram(raw: bytes) -> bool:
    """Parse one datagram and record it. True if it produced a recorder call."""
    global _malformed_logged
    try:
        msg = json.loads(raw.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError) as exc:
        with _stats_lock:
            _stats["errors"] += 1
        if _malformed_logged < _MALFORMED_LOG_LIMIT:
            _malformed_logged += 1
            print(f"[AIS-local] malformed datagram: {exc}", flush=True)
        return False

    fields = parse_message(msg) if isinstance(msg, dict) else None
    if fields is None:
        with _stats_lock:
            _stats["rejected"] += 1
        return False

    ais.record(fields, source="local")
    with _stats_lock:
        _stats["messages"] += 1
        _stats["last_message_at"] = time.time()
    return True


def _listen(sock: socket.socket) -> None:
    global _listener_errors_logged
    while True:
        try:
            raw, _ = sock.recvfrom(65535)
        except OSError:
            return
        try:
            handle_datagram(raw)
        except Exception as exc:
            # A handler bug -- a TypeError from an unexpected value shape in otherwise
            # well-formed JSON, or anything ais.record raises -- must not kill this bare
            # daemon thread. If it does, the socket is abandoned, no further datagrams are
            # read, and stats() freezes at its last value forever. Task 5's silence
            # watchdog keys off last_message_at, so a dead thread would then be reported as
            # "the feed went quiet" -- a true statement with a misleading cause, sending
            # someone to check AIS-catcher, the dongle and the antenna, all of which are
            # fine. Counted under "errors" and rate-limited so a persistent bad sender
            # cannot flood the log.
            with _stats_lock:
                _stats["errors"] += 1
            if _listener_errors_logged < _LISTENER_ERROR_LOG_LIMIT:
                _listener_errors_logged += 1
                print(f"[AIS-local] listener caught an unexpected error: "
                      f"{type(exc).__name__}: {exc}", flush=True)


def start() -> None:
    """Start the listener thread. Called once at proxy startup."""
    if not AIS_LOCAL_ENABLED:
        print("[AIS-local] disabled (AIS_LOCAL_ENABLED=off)", flush=True)
        return
    try:
        sock = bind(AIS_LOCAL_UDP_PORT)
    except OSError as exc:
        # Fatal is correct -- something really does already own this port -- but a bare
        # `OSError: [WinError 10048]` traceback leaves the operator guessing what. Name
        # the likely culprit and the port before it propagates and kills the proxy.
        print(f"[AIS-local] FATAL: could not bind 127.0.0.1:{AIS_LOCAL_UDP_PORT}: {exc}. "
              f"Is AIS-catcher (or another instance of this proxy) already using that "
              f"port? Set AIS_LOCAL_UDP_PORT to change it, or AIS_LOCAL_ENABLED=off to "
              f"disable the local feed.", flush=True)
        raise
    global _started_at
    _started_at = time.time()
    threading.Thread(target=_listen, args=(sock,), daemon=True,
                     name="ais-local").start()
    print(f"[AIS-local] listening on 127.0.0.1:{AIS_LOCAL_UDP_PORT}", flush=True)
    # Mirrors ais.py's _watch_silence for aisstream: a feed that fails by going quiet is
    # indistinguishable from a quiet channel unless something is watching the clock.
    # AIS_LOCAL_UDP_PORT sits idle whether AIS-catcher never started or died mid-stream,
    # so this is the only thing that will ever say so.
    if AIS_SILENCE_WARN_SEC > 0:
        threading.Thread(target=_watch_silence, daemon=True,
                         name="ais-local-silence").start()


AIS_SILENCE_WARN_SEC = int(os.environ.get("AIS_SILENCE_WARN_SEC", "60"))

_started_at: float | None = None


def silence_report(now: float) -> str | None:
    """A message if the local feed has gone quiet, else None.

    Two distinct faults, and telling them apart is the point: 'AIS-catcher was never
    started or cannot reach us' looks identical to 'it was running and stopped' unless you
    say so. That distinction is what made the aisstream outage diagnosable.
    """
    if AIS_SILENCE_WARN_SEC <= 0 or _started_at is None:
        return None
    last = stats()["last_message_at"]
    if last is None:
        quiet = now - _started_at
        if quiet >= AIS_SILENCE_WARN_SEC:
            return (f"local AIS has never received a message, {quiet:.0f}s since start "
                    f"-- is AIS-catcher running and pointed at "
                    f"127.0.0.1:{AIS_LOCAL_UDP_PORT}?")
        return None
    quiet = now - last
    if quiet >= AIS_SILENCE_WARN_SEC:
        return f"local AIS went quiet {quiet:.0f}s ago after {stats()['messages']} messages"
    return None


def _watch_silence() -> None:
    """Print silence_report()'s message, for as long as it keeps returning one.

    Bare polling loop, deliberately untested directly (silence_report itself, the pure
    decision, is what Task 5's tests cover) -- same shape as ais.py's `_watch_silence`.
    """
    interval = max(AIS_SILENCE_WARN_SEC, 5)
    while True:
        time.sleep(interval)
        report = silence_report(time.time())
        if report:
            print(f"[AIS-local] {report}", flush=True)
