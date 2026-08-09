"""Local AIS reception: AIS-catcher's decoded JSON into the shared recorder.

aisstream has delivered nothing since 2026-08-05 and the upstream issue describing that
exact symptom has been open since 2026-03-13 with no maintainer response. This reads a
locally-received feed instead.

AIS-catcher does all the AIVDM work -- 6-bit unpacking, multi-part reassembly, checksums --
and emits decoded JSON with `-o 5`. This module is an adapter, not a decoder.
"""

from __future__ import annotations

import json
import os
import socket
import threading
import time

from . import ais

# Message types that describe a VESSEL. Everything else is ignored, and two exclusions are
# load-bearing: type 4 is a shore base station, and type 21 is an aid to navigation -- a
# buoy, which carries a `name` and would otherwise enter the name-keyed cache and become a
# candidate for vessel name matching.
_POSITION_TYPES = {1, 2, 3, 18, 19}
_STATIC_TYPES   = {5, 19, 24}


def parse_message(msg: dict) -> dict | None:
    """AIS-catcher JSON -> recorder fields, or None if the message should be ignored."""
    # AIS-catcher still decodes a sentence whose checksum failed and flags it with `error`.
    # Observed 2026-08-09: a corrupted checksum produced a full, plausible decode. A wrong
    # vessel name out of a corrupt payload is the failure that costs most here. Guard checks
    # truthiness, not presence: if AIS-catcher uses `error: 0` to mean "no error", presence
    # check would silently reject every clean message and kill the feed.
    if msg.get("error"):
        return None

    msg_type = msg.get("type")
    if msg_type not in _POSITION_TYPES and msg_type not in _STATIC_TYPES:
        return None

    mmsi = str(msg.get("mmsi") or "").strip()
    if not mmsi:
        return None

    fields: dict = {"mmsi": mmsi}

    if msg_type in _STATIC_TYPES:
        name = (msg.get("shipname") or "").strip()
        if name:
            fields["name"] = name
        callsign = (msg.get("callsign") or "").strip()
        if callsign:
            fields["callsign"] = callsign
        for src, dst in (("imo", "imo"), ("shiptype", "type"),
                         ("draught", "draught"), ("destination", "destination")):
            if msg.get(src) is not None:
                fields[dst] = msg[src]
        if msg.get("to_bow") is not None and msg.get("to_stern") is not None:
            fields["length"] = (msg["to_bow"] + msg["to_stern"]) or None
        if msg.get("to_port") is not None and msg.get("to_starboard") is not None:
            fields["beam"] = (msg["to_port"] + msg["to_starboard"]) or None

    if msg.get("lat") is not None and msg.get("lon") is not None:
        fields["latitude"] = msg["lat"]
        fields["longitude"] = msg["lon"]
        for src, dst in (("speed", "sog"), ("course", "cog"), ("heading", "heading")):
            if msg.get(src) is not None:
                fields[dst] = msg[src]

    return fields


AIS_LOCAL_ENABLED  = os.environ.get("AIS_LOCAL_ENABLED", "on").strip().lower() != "off"
AIS_LOCAL_UDP_PORT = int(os.environ.get("AIS_LOCAL_UDP_PORT", "10110"))

# Owned by this module and read through it, never via an imported name: it is written by
# the listener thread.
_stats: dict = {"messages": 0, "last_message_at": None, "rejected": 0, "errors": 0}
_stats_lock = threading.Lock()

_MALFORMED_LOG_LIMIT = 5
_malformed_logged = 0


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
    while True:
        try:
            raw, _ = sock.recvfrom(65535)
        except OSError:
            return
        handle_datagram(raw)


def start() -> None:
    """Start the listener thread. Called once at proxy startup."""
    if not AIS_LOCAL_ENABLED:
        print("[AIS-local] disabled (AIS_LOCAL_ENABLED=off)", flush=True)
        return
    sock = bind(AIS_LOCAL_UDP_PORT)
    threading.Thread(target=_listen, args=(sock,), daemon=True,
                     name="ais-local").start()
    print(f"[AIS-local] listening on 127.0.0.1:{AIS_LOCAL_UDP_PORT}", flush=True)
