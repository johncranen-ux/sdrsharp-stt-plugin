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
import re
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

def _clean_destination(raw: str) -> str | None:
    """The destination a ship broadcasts, with its AIS padding removed.

    '@' is the null character in AIS's 6-bit alphabet, so the field arrives padded out to its
    fixed width with them and everything from the first one is padding -- including the stray
    trailing character in aisstream's own example, "COASTGUARD@@@@@@@@H". Never a real part of
    a destination, so splitting on it is safe rather than merely convenient.
    """
    cleaned = (raw or "").split("@")[0].strip()
    return cleaned or None


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


# aisstream.io names every field in PascalCase, and three were read here in upper case:
# `IMO`, `SOG` and `COG`, where the feed sends `ImoNumber`, `Sog` and `Cog`. They parsed to
# None on every message ever received -- 0 of 8,434 cached vessels carried any of the three,
# while every correctly-cased key (`CallSign`, `Type`, `Latitude`, `TrueHeading`) sat at
# 83-94% -- so /identified-vessels showed a dash for IMO, speed and course from the day it
# was written, and nothing failed. Field names are pinned by tests against the documented
# message shape now, because a typo here is invisible: the feed is external, the value is
# optional, and None is indistinguishable from "this ship did not broadcast it".
_LAST_SEEN_FMT = "%Y-%m-%d %H:%M:%S"

# Exclude vessels not heard from in this many minutes when matching. 0 disables it, which is
# the default -- the field it depends on only started being written on 2026-08-06, so there
# is no data yet from which to choose a threshold.
#
# This EXCLUDES rather than deletes, deliberately. A purge is destructive and, worse, silent:
# once an entry is gone there is no way to ask how often the vessel removed was the one that
# went on to call. Filtering at match time gives the same candidate-pool reduction -- the
# cache holds 8,642 names accumulated over weeks, where a live 15-minute window would hold a
# few hundred -- while staying an env knob that can be A/B'd and reverted.
#
# Calibrating it needs live data, not judgement: for each conversation, how old was the
# correct vessel's last_seen at the moment of the call? Note the bounding box reaches only
# 64 km west of Maas Center while inbound traffic arrives from the west, so a vessel calling
# from further out is not being updated at all and any threshold will exclude it.
AIS_MAX_AGE_MIN = int(os.environ.get("AIS_MAX_AGE_MIN", "0"))

_stale_filter_warned = False


def _now() -> str:
    """Timestamp for `last_seen`, in the format the rest of the stores use.

    Rolling, not an entry time: AIS transmits position every 2-10 seconds underway, so a
    vessel in the box has this rewritten constantly, and it freezes only once the vessel
    leaves, stops transmitting, or the proxy stops running. That is the whole value of it --
    a cached position is otherwise uninterpretable, since the cache is reloaded from disk at
    startup and entries never expire, so "48 km from Maas Center" could be from forty
    seconds ago or from three weeks ago and nothing in the data distinguishes them.

    It cannot be backfilled: entries written before this existed have no timestamp and never
    will. Treat a missing `last_seen` as unknown age, not as recent.

    Caveat: this conflates "the vessel left the area" with "the proxy was not running", so
    it is a floor on freshness rather than a measurement of it. That is the safe direction --
    everything reads as unknown rather than as confidently current.
    """
    return datetime.datetime.now().strftime(_LAST_SEEN_FMT)


def _is_fresh(entry: dict, cutoff: datetime.datetime) -> bool:
    """Whether this vessel was heard from recently enough to be a plausible candidate.

    A missing or unparseable `last_seen` counts as NOT fresh. Unknown age is not evidence of
    recency, and every entry written before 2026-08-06 lacks the field -- so enabling the
    filter against an old cache excludes almost everything until the feed has run again.
    That is loud (see the warning in _fresh_snapshot) rather than subtly wrong, and it
    self-corrects as vessels are seen.
    """
    stamp = entry.get("last_seen")
    if not stamp:
        return False
    try:
        return datetime.datetime.strptime(stamp, _LAST_SEEN_FMT) >= cutoff
    except (TypeError, ValueError):
        return False


def _fresh_snapshot() -> tuple[list[str], dict[str, dict]]:
    """(keys, cache) for matching, honouring AIS_MAX_AGE_MIN.

    Shared by every matcher so one setting means one thing everywhere -- the same reason the
    decoder params live in one place.
    """
    global _stale_filter_warned
    with _cache_lock:
        if not _vessel_cache:
            return [], {}
        if AIS_MAX_AGE_MIN <= 0:
            return list(_vessel_cache.keys()), dict(_vessel_cache)
        total  = len(_vessel_cache)
        cutoff = datetime.datetime.now() - datetime.timedelta(minutes=AIS_MAX_AGE_MIN)
        cache  = {k: v for k, v in _vessel_cache.items() if _is_fresh(v, cutoff)}

    if not _stale_filter_warned and total and len(cache) < total * 0.05:
        _stale_filter_warned = True
        print(f"[AIS] age filter left {len(cache)} of {total} vessels matchable "
              f"(AIS_MAX_AGE_MIN={AIS_MAX_AGE_MIN}). Expected right after enabling it, or "
              f"after a feed outage: entries with no last_seen are treated as unknown age.",
              flush=True)
    return list(cache.keys()), cache


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
                imo    = ship.get("ImoNumber")
                stype  = ship.get("Type")
                length = (dim.get("A", 0) + dim.get("B", 0)) or None
                beam   = (dim.get("C", 0) + dim.get("D", 0)) or None
                # Draught and destination are what CH01 actually asks about, on nearly every
                # call: "what is your maximum draught" and where the ship is bound.
                entry  = {"name": name, "callsign": callsign, "mmsi": mmsi,
                          "type": stype, "imo": imo, "length": length, "beam": beam,
                          "draught": ship.get("MaximumStaticDraught"),
                          "destination": _clean_destination(ship.get("Destination", "")),
                          "last_seen": _now()}
                with _cache_lock:
                    # MERGE, never replace. Static data carries no position, so assigning
                    # this dict wholesale deleted whatever PositionReport had recorded --
                    # and static messages repeat every ~6 minutes, so a vessel sitting in
                    # the box lost its position over and over. Measured before this fix:
                    # 25% of the vessels in the labelled conversations had no position at
                    # all, which is the failure that made distance data unusable.
                    existing = _vessel_cache.get(name.upper())
                    if existing is not None:
                        existing.update(entry)
                        entry = existing
                    else:
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
                sog = pos.get("Sog")
                cog = pos.get("Cog")
                heading = pos.get("TrueHeading")
                with _cache_lock:
                    if key not in _vessel_cache:
                        _vessel_cache[key] = {"name": name, "callsign": "", "mmsi": mmsi,
                                              "type": None, "imo": None, "length": None, "beam": None,
                                              "latitude": lat, "longitude": lon, "sog": sog,
                                              "cog": cog, "heading": heading,
                                              "last_seen": _now()}
                    else:
                        e = _vessel_cache[key]
                        e["latitude"] = lat; e["longitude"] = lon
                        e["sog"] = sog; e["cog"] = cog; e["heading"] = heading
                        e["last_seen"] = _now()
    except Exception as exc:
        print(f"[AIS] process error: {exc}", flush=True)


def _cache_size() -> int:
    with _cache_lock:
        return len(_vessel_cache)


# Vessel name matching
#
# This carried WRatio long after _find_ais_hints was moved off it, and failed the same way
# one layer down. WRatio switches to partial_ratio*0.9 when the strings differ in length by
# 1.5x-8x, so a short cache name scores 90 against any longer name containing it. The
# reported case: 'Motortanker Orason' was identified as 'RA' (MMSI 244729064), because RA is
# a substring of o-RA-son and scored 90, while the ship actually being called -- ORASUND,
# in the cache the whole time -- scored 76.9 and fell under the cutoff. The same path reaches
# RA from MARATHON, GRACE and RADAR. There are ~85 names of three characters or fewer in the
# live cache, each a substring landmine.
#
# Measured over the 7,640-name live cache by corrupting real names the way STT does and
# checking whether the matcher recovers the ship that was said (percentages are end-to-end
# through match_by_name, so they include the fallback path):
#
#                            one edit (n=3000)          two edits (n=2893)
#   WRatio 80 (before)       84.6% right, 14.8% wrong   63.0% right, 30.1% wrong
#   ratio 76 + guard (now)   91.9% right,  6.7% wrong   80.9% right, 11.1% wrong
#
# The two-edit corpus is the class the reported case belongs to, and where the old scorer was
# losing outright: a single edit to a long name still scores 85+, so the damage concentrates
# where STT garbles a name twice.
#
# 76 is the floor, not a preference: on the two-edit corpus, going from 80 down to 76 gains
# 163 correct matches for 13 wrong ones, and the next step to 75 costs 42 wrong for 23 right
# while the one-edit corpus turns bad at exactly the same point. A confident wrong
# identification is worse here than none, which is what picks the last favourable step rather
# than the highest recall.
#
# The word-window fallback below now shares that cutoff instead of holding its own stricter
# 88. Measured separately on 2,500 "<type word> <misheard name>" queries -- the shape that
# actually reaches the fallback, and which the corpus above barely exercised -- every step
# from 88 down to 76 pays (+555 right for +191 wrong in total) and 74 turns bad at the same
# point the main cutoff does. Once the type words are stripped, the candidate is an ordinary
# name query, so there was never a reason for it to face a higher bar than one.
#
# Set AIS_NAME_FILTER=off to restore the original behaviour exactly.
AIS_NAME_FILTER    = os.environ.get("AIS_NAME_FILTER", "on").strip().lower() != "off"
AIS_NAME_MIN_SCORE = int(os.environ.get("AIS_NAME_MIN_SCORE", "76"))
AIS_NAME_MIN_TOKEN = int(os.environ.get("AIS_NAME_MIN_TOKEN", "4"))

_NAME_SKIP = {"MV", "MT", "MS", "SV", "SS", "TUG", "MOTOR", "TANKER",
              "BULKER", "VESSEL", "CONTAINER", "MOTORTANKER", "MOTORVESSEL"}


def _best_name_match(query: str, keys: list[str], cutoff: int) -> str | None:
    """Highest-scoring cache name for `query`, with short names held to equality.

    A name of three characters or fewer is a substring of much of the corpus, so it is
    accepted only when the speaker said exactly that -- which keeps the real short vessels
    (AMY, RED, P99...) reachable while removing the class of match that produced 'RA'.
    """
    if not AIS_NAME_FILTER:
        hit = rf_process.extractOne(query, keys, scorer=rf_fuzz.WRatio, score_cutoff=cutoff)
        return hit[0] if hit else None

    hits = rf_process.extract(query, keys, scorer=rf_fuzz.ratio,
                              limit=None, score_cutoff=cutoff)
    best = None
    for name, score, _ in hits:
        if len(name.replace(" ", "")) < AIS_NAME_MIN_TOKEN and name != query:
            continue
        if best is None or score > best[1]:
            best = (name, score)
    return best[0] if best else None


def match_by_name(extracted_name: str) -> dict | None:
    if not extracted_name:
        return None
    query = extracted_name.upper()
    keys, cache = _fresh_snapshot()
    if not keys:
        return None
    cutoff = AIS_NAME_MIN_SCORE if AIS_NAME_FILTER else 80
    hit = _best_name_match(query, keys, cutoff)
    if hit:
        return cache[hit]
    words = [w for w in query.split() if w not in _NAME_SKIP and len(w) >= 3]
    candidates = []
    for length in range(len(words), 0, -1):
        for start in range(len(words) - length + 1):
            candidates.append(" ".join(words[start:start + length]))
    for candidate in candidates:
        hit = _best_name_match(candidate, keys, cutoff if AIS_NAME_FILTER else 88)
        if hit:
            return cache[hit]
    return None


def match_by_callsign(extracted_callsign: str) -> dict | None:
    if not extracted_callsign:
        return None
    with _cache_lock:
        return _callsign_cache.get(extracted_callsign.upper())


def match_by_mmsi(mmsi: str) -> dict | None:
    """The cached vessel with this MMSI, or None.

    A scan rather than a dict lookup: the cache is keyed by name because that is what every
    other path searches by, and 7,000-odd entries is nothing next to the fuzzy matching
    happening either side of this call.
    """
    if not mmsi:
        return None
    wanted = str(mmsi)
    with _cache_lock:
        for entry in _vessel_cache.values():
            if entry.get("mmsi") == wanted:
                return entry
    return None


def match_by_callsign_pattern(pattern: str) -> dict | None:
    """The one cached vessel whose callsign matches `pattern`, or None.

    Returns None when several match: a pattern that fits more than one ship carries no
    identification, and picking any of them would be a guess wearing evidence's clothes.
    """
    if not pattern:
        return None
    try:
        matcher = re.compile(f"^{pattern}$")
    except re.error:
        return None
    with _cache_lock:
        entries = list(_callsign_cache.items())
    found = None
    for callsign, entry in entries:
        if matcher.match(callsign):
            if found is not None:
                return None
            found = entry
    return found


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
# Longest word span offered to the matcher. 4 covers every multi-word name seen in this
# traffic ("SANTA ISABEL MAERSK", "MSC MARIA PIA"); raising it further costs probes per
# transmission without a name to find. Set to 2 to get the pre-2026-08-06 behaviour.
AIS_HINT_MAX_NGRAM = int(os.environ.get("AIS_HINT_MAX_NGRAM", "4"))

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
    """Contiguous word spans worth looking up as vessel names.

    Spans run up to AIS_HINT_MAX_NGRAM words because the matcher scores WHOLE strings. A
    probe shorter than the name it should match cannot clear the cutoff however perfectly it
    overlaps -- "SANTA ISABEL" scores 77 against SANTA ISABEL MAERSK and "ISABEL MAERSK" 81,
    both under 85 -- so a name longer than the longest probe is unreachable at every length
    that exists, while "ISABEL" matches a different, real vessel at 100. Adjacent pairs alone
    therefore lost every vessel with a three-word name.

    Measured 2026-08-06 over the 59 verified conversations: raising the limit from 2 to 4
    recovers 3 of 24 unmatched conversations for +8% spurious probe->vessel pairs, and
    crowds nothing out of the 5-slot hint list. Phonetic matching was sized against this and
    rejected -- it cost 2.3-5.6x the spurious pairs. See docs/design-notes.md.
    """
    if not AIS_HINT_FILTER:
        return _legacy_hint_probes(text)

    words = [w.strip(".,!?;:") for w in text.upper().split()]

    probes = []
    # Grouped by span length, shortest first -- the order the recovery and crowding-out
    # measurements were taken in.
    for n in range(1, max(AIS_HINT_MAX_NGRAM, 1) + 1):
        for i in range(len(words) - n + 1):
            span = words[i:i + n]
            # A span needs only ONE substantial token: real names routinely pair a short
            # word with a long one ("NQ TULIPA", "GOOD WAY"), and requiring every token to
            # clear the bar silently drops them. A span is specific enough that the length
            # guard matters far less than it does for a lone word.
            if max(len(t) for t in span) < AIS_HINT_MIN_TOKEN:
                continue
            if all(t in _HINT_STOPWORDS for t in span):
                continue
            probes.append(" ".join(span))
    return probes


def _legacy_hint_probes(text: str) -> list[str]:
    """The pre-filter behaviour, reproduced exactly -- bug, ordering and all.

    AIS_HINT_FILTER=off is only a trustworthy revert if it restores what was there before,
    so this path must not inherit improvements made to the one above.
    """
    words = text.upper().split()
    probes = []
    for i, w in enumerate(words):
        if len(w) >= 3:
            probes.append(w)
        if i < len(words) - 1 and len(words[i + 1]) >= 3:
            probes.append(f"{w} {words[i + 1]}")
    return probes


def _find_ais_hints(text: str, n: int = 5) -> list[dict]:
    if not text.strip():
        return []
    keys, cache = _fresh_snapshot()
    if not keys:
        return []

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
