"""Local AIS reception: AIS-catcher's decoded JSON into the shared recorder.

aisstream has delivered nothing since 2026-08-05 and the upstream issue describing that
exact symptom has been open since 2026-03-13 with no maintainer response. This reads a
locally-received feed instead.

AIS-catcher does all the AIVDM work -- 6-bit unpacking, multi-part reassembly, checksums --
and emits decoded JSON with `-o 5`. This module is an adapter, not a decoder.
"""

from __future__ import annotations

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
    # vessel name out of a corrupt payload is the failure that costs most here.
    if msg.get("error") is not None:
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
