"""Live AIS vessel data, and matching transcribed names against it.

Holds the vessel cache fed by the aisstream.io websocket, and the lookups that turn a name
or callsign heard on the radio into a real ship. Both halves live here because the matching
thresholds only make sense against the shape of the cache they search.

State is module-level and shared with the feed thread, guarded by `_cache_lock`. Read it
through this module (`ais._vessel_cache`) rather than importing the name, or you will bind
a snapshot and, in tests, patch something nothing reads.

Everything here degrades to a no-op when AISSTREAM_API_KEY is unset: the cache stays empty,
lookups return None, and the rest of the pipeline carries on without vessel enrichment.
"""

import asyncio
import datetime
import json
import os
import threading
import time

import websockets
from rapidfuzz import fuzz as rf_fuzz, process as rf_process

# Rotterdam / Maas Approach bounding box  [SW corner, NE corner]
ROTTERDAM_BBOX = [[[51.0, 2.95], [52.85, 6.0]]]

AIS_CACHE_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "ais_cache.json")
AIS_CACHE_FILE    = os.path.normpath(AIS_CACHE_FILE)
AIS_SAVE_INTERVAL = 300


# ---------------------------------------------------------------------------

AIS_SHIP_TYPES = {
    30: "Fishing", 31: "Tug", 32: "Tug", 33: "Military ops", 34: "Dive ops",
    35: "Medical transport", 36: "Sailing", 37: "Pleasure craft",
    40: "Pilot vessel", 41: "Search & rescue", 42: "Tug", 43: "Port tender",
    50: "Pilot vessel", 51: "Search & rescue", 52: "Tug", 53: "Port tender",
    60: "Passenger ship", 61: "Cargo ship", 62: "Tanker", 70: "Tanker",
    71: "Tanker", 72: "Tanker", 73: "Tanker", 74: "Tanker", 75: "Tanker",
    76: "Tanker", 77: "Tanker", 78: "Tanker", 80: "General cargo",
    81: "General cargo", 82: "General cargo", 83: "General cargo",
    84: "General cargo", 85: "General cargo", 86: "General cargo",
    87: "General cargo", 88: "General cargo", 89: "Other",
    90: "Container ship", 91: "Container ship", 92: "Container ship",
    93: "Container ship", 94: "Container ship", 95: "Container ship",
    96: "Container ship", 97: "Container ship", 98: "Container ship",
    100: "Bulk carrier", 101: "Bulk carrier", 102: "Bulk carrier",
    103: "Bulk carrier", 104: "Bulk carrier", 105: "Bulk carrier",
}

def _get_ship_type_name(type_code) -> str | None:
    if type_code is None:
        return None
    return AIS_SHIP_TYPES.get(type_code, f"Type {type_code}")


# ---------------------------------------------------------------------------
# AIS vessel cache
# ---------------------------------------------------------------------------

_vessel_cache:   dict[str, dict] = {}
_callsign_cache: dict[str, dict] = {}
_cache_lock = threading.Lock()


def _load_cache() -> None:
    global _vessel_cache, _callsign_cache
    try:
        with open(AIS_CACHE_FILE, "r", encoding="utf-8") as f:
            entries = json.load(f)
        with _cache_lock:
            for entry in entries:
                _vessel_cache[entry["name"].upper()] = entry
                if entry.get("callsign"):
                    _callsign_cache[entry["callsign"].upper()] = entry
        print(f"[AIS] loaded {len(_vessel_cache)} vessels from cache", flush=True)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[AIS] cache load error: {exc}", flush=True)


def _save_cache() -> None:
    try:
        with _cache_lock:
            entries = list(_vessel_cache.values())
        with open(AIS_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(entries, f)
    except Exception as exc:
        print(f"[AIS] cache save error: {exc}", flush=True)


def _periodic_save() -> None:
    while True:
        threading.Event().wait(AIS_SAVE_INTERVAL)
        _save_cache()


def _ais_thread(api_key: str) -> None:
    import asyncio
    asyncio.run(_ais_loop(api_key))


async def _ais_loop(api_key: str) -> None:
    try:
        import websockets
    except ImportError:
        print("[AIS] 'websockets' not installed — run: pip install websockets", flush=True)
        return

    sub_msg = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": ROTTERDAM_BBOX,
        "FilterMessageTypes": ["ShipStaticData", "PositionReport"],
    })

    import ssl as _ssl
    import certifi
    ssl_ctx = _ssl.create_default_context(cafile=certifi.where())

    while True:
        try:
            async with websockets.connect("wss://stream.aisstream.io/v0/stream", ssl=ssl_ctx) as ws:
                await ws.send(sub_msg)
                print("[AIS] connected — watching Rotterdam / Maas Approach area", flush=True)
                async for raw in ws:
                    _process_ais(json.loads(raw))
        except Exception as exc:
            print(f"[AIS] disconnected ({exc}), reconnecting in 30s...", flush=True)
            import asyncio as _a
            await _a.sleep(30)


def _process_ais(msg: dict) -> None:
    try:
        msg_type = msg.get("MessageType", "")
        meta     = msg.get("MetaData", {})
        mmsi     = str(meta.get("MMSI", "")).strip()
        if not mmsi:
            return

        if msg_type == "ShipStaticData":
            ship     = msg.get("Message", {}).get("ShipStaticData", {})
            name     = (ship.get("Name") or meta.get("ShipName") or "").strip()
            callsign = ship.get("CallSign", "").strip()
            if name:
                dim    = ship.get("Dimension", {})
                imo    = ship.get("IMO")
                stype  = ship.get("Type")
                length = (dim.get("A", 0) + dim.get("B", 0)) or None
                beam   = (dim.get("C", 0) + dim.get("D", 0)) or None
                entry  = {"name": name, "callsign": callsign, "mmsi": mmsi,
                          "type": stype, "imo": imo, "length": length, "beam": beam}
                with _cache_lock:
                    _vessel_cache[name.upper()] = entry
                    if callsign:
                        _callsign_cache[callsign.upper()] = entry

        elif msg_type == "PositionReport":
            name = meta.get("ShipName", "").strip()
            if name:
                key = name.upper()
                pos = msg.get("Message", {}).get("PositionReport", {})
                lat = pos.get("Latitude")
                lon = pos.get("Longitude")
                sog = pos.get("SOG")
                cog = pos.get("COG")
                heading = pos.get("TrueHeading")
                with _cache_lock:
                    if key not in _vessel_cache:
                        _vessel_cache[key] = {"name": name, "callsign": "", "mmsi": mmsi,
                                              "type": None, "imo": None, "length": None, "beam": None,
                                              "latitude": lat, "longitude": lon, "sog": sog,
                                              "cog": cog, "heading": heading}
                    else:
                        e = _vessel_cache[key]
                        e["latitude"] = lat; e["longitude"] = lon
                        e["sog"] = sog; e["cog"] = cog; e["heading"] = heading
    except Exception as exc:
        print(f"[AIS] process error: {exc}", flush=True)


def _cache_size() -> int:
    with _cache_lock:
        return len(_vessel_cache)


def match_by_name(extracted_name: str) -> dict | None:
    if not extracted_name:
        return None
    query = extracted_name.upper()
    with _cache_lock:
        if not _vessel_cache:
            return None
        keys  = list(_vessel_cache.keys())
        cache = dict(_vessel_cache)
    hit = rf_process.extractOne(query, keys, scorer=rf_fuzz.WRatio, score_cutoff=80)
    if hit:
        return cache[hit[0]]
    SKIP = {"MV", "MT", "MS", "SV", "SS", "TUG", "MOTOR", "TANKER",
            "BULKER", "VESSEL", "CONTAINER", "MOTORTANKER", "MOTORVESSEL"}
    words = [w for w in query.split() if w not in SKIP and len(w) >= 3]
    candidates = []
    for length in range(len(words), 0, -1):
        for start in range(len(words) - length + 1):
            candidates.append(" ".join(words[start:start + length]))
    for candidate in candidates:
        hit = rf_process.extractOne(candidate, keys, scorer=rf_fuzz.WRatio, score_cutoff=88)
        if hit:
            return cache[hit[0]]
    return None


def match_by_callsign(extracted_callsign: str) -> dict | None:
    if not extracted_callsign:
        return None
    with _cache_lock:
        return _callsign_cache.get(extracted_callsign.upper())


# AIS hint matching
#
# Hints are candidate vessels shown to Claude alongside a transcript. The original
# settings (WRatio, cutoff 65, 3-char tokens) were far too loose: measured over 307 real
# transcripts they produced 1,993 distinct spurious probe->vessel pairs, because WRatio
# falls back to partial matching when lengths differ, so an ordinary short word scores ~90
# against any long name containing it -- 'THE'->'SYNTHESE 11', 'ONE'->'RIVER DRONE 1',
# 'AND'->'ALEXANDER-M', 'GOOD DAY'->'GOOD WAY'. Claude was then told to use those hints to
# correct vessel names, so the pipeline was inventing vessels and handing them over as
# evidence.
#
# fuzz.ratio compares whole strings and does not reward a short substring, which is what
# this needs. Measured on the same corpus: 1,993 -> 114 pairs, with all known real vessel
# names still matched. The stopword guard below removes a further 19 (phonetic letters,
# numbers and radio procedure words), giving 95 -- a 21x reduction with no loss of recall.
#
# Set AIS_HINT_FILTER=off to restore the original behaviour exactly.
AIS_HINT_FILTER    = os.environ.get("AIS_HINT_FILTER", "on").strip().lower() != "off"
AIS_HINT_MIN_SCORE = int(os.environ.get("AIS_HINT_MIN_SCORE", "85"))
AIS_HINT_MIN_TOKEN = int(os.environ.get("AIS_HINT_MIN_TOKEN", "4"))

# Words that are never a vessel name on their own here. Numbers and NATO phonetics are
# how callsigns and positions get read out (callsigns have their own exact lookup in
# match_by_callsign, so nothing is lost); the rest is ordinary speech and radio procedure.
# A probe is skipped only when *every* token is in this set, so "MSC PANTERA" survives
# while "GOOD DAY" does not.
_HINT_STOPWORDS = frozenset("""
ZERO ONE TWO THREE FOUR FIVE SIX SEVEN EIGHT NINE TEN ELEVEN TWELVE HUNDRED THOUSAND
DECIMAL POINT
ALPHA BRAVO CHARLIE DELTA ECHO FOXTROT GOLF HOTEL INDIA JULIET KILO LIMA MIKE NOVEMBER
OSCAR PAPA QUEBEC ROMEO SIERRA TANGO UNIFORM VICTOR WHISKEY XRAY YANKEE ZULU
OVER OUT ROGER WILCO COPY AFFIRM NEGATIVE STANDBY CHANNEL APPROACH PILOT PILOTS VESSEL
SHIP TANKER MOTORTANKER BULKER CONTAINER TRAFFIC ANCHOR ANCHORAGE STARBOARD PORTSIDE
BERTH BUOY BUOYS DRAUGHT CALLSIGN
GOOD MORNING AFTERNOON EVENING DAY NIGHT THANK THANKS PLEASE SIR MADAM YEAH YES WELL OKAY
NEW OLD NEXT LAST FIRST SECOND
WATER AREA TIME READ VERY WILL ENTERING PROCEEDING UNDERSTOOD INFORMATION POSITION SPEED
COURSE
THAT THIS THESE THOSE THERE HERE WITH FROM YOUR OURS HAVE HAS BEEN WOULD SHALL SHOULD
WHAT WHEN WHERE WHICH WHILE ABOUT ABOVE BELOW AFTER BEFORE
""".split())


def _hint_probes(text: str) -> list[str]:
    """Single words and adjacent pairs worth looking up as vessel names."""
    min_token = AIS_HINT_MIN_TOKEN if AIS_HINT_FILTER else 3
    words = [w.strip(".,!?;:") for w in text.upper().split()] if AIS_HINT_FILTER \
        else text.upper().split()

    probes = []
    for i, w in enumerate(words):
        if len(w) >= min_token:
            probes.append(w)
        if i < len(words) - 1:
            nxt = words[i + 1]
            # A pair only needs ONE substantial token: real names routinely pair a short
            # word with a long one ("NQ TULIPA", "GOOD WAY"), and requiring both to clear
            # the bar silently drops them. Pairs are specific enough that the length guard
            # matters far less than it does for a lone word.
            if (max(len(w), len(nxt)) >= min_token if AIS_HINT_FILTER
                    else len(nxt) >= min_token):
                probes.append(f"{w} {nxt}")

    if AIS_HINT_FILTER:
        probes = [p for p in probes if not all(tok in _HINT_STOPWORDS for tok in p.split())]
    return probes


def _find_ais_hints(text: str, n: int = 5) -> list[dict]:
    if not text.strip():
        return []
    with _cache_lock:
        if not _vessel_cache:
            return []
        keys  = list(_vessel_cache.keys())
        cache = dict(_vessel_cache)

    scorer = rf_fuzz.ratio if AIS_HINT_FILTER else rf_fuzz.WRatio
    cutoff = AIS_HINT_MIN_SCORE if AIS_HINT_FILTER else 65

    seen:    set[str]  = set()
    results: list[dict] = []
    for probe in _hint_probes(text):
        hit = rf_process.extractOne(probe, keys, scorer=scorer, score_cutoff=cutoff)
        if hit:
            entry = cache[hit[0]]
            mmsi  = entry.get("mmsi", "")
            if mmsi and mmsi not in seen:
                seen.add(mmsi)
                results.append(entry)
                if len(results) >= n:
                    break
    return results
