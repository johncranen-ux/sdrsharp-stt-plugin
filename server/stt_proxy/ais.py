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
import math
import os
import random
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
                # Without this, the index starts empty on every restart: a nameless
                # local position report (types 1/2/3/18) for a vessel already on disk
                # finds nothing here, lands in _pending instead of updating the entry
                # directly, and is only reunited with it if and when a later static
                # message re-triggers the name-adoption path in record() -- which
                # Class-B vessels (type 18) may never send.
                mmsi = str(entry.get("mmsi") or "").strip()
                if mmsi:
                    _mmsi_index[mmsi] = entry
        print(f"[AIS] loaded {len(_vessel_cache)} vessels from cache", flush=True)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[AIS] cache load error: {exc}", flush=True)


def _save_cache() -> None:
    try:
        with _cache_lock:
            # Copies of the entry dicts, not the shared objects themselves: the lock
            # only covers this snapshot, and json.dump below runs outside it while
            # _apply() can still be adding a key to a live entry from another thread.
            # Iterating the SAME dict json.dump was serialising raised `RuntimeError:
            # dictionary changed size during iteration`, caught by the broad except
            # below so the save silently did nothing.
            entries = [dict(entry) for entry in _vessel_cache.values()]
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


# Reconnect backoff. The retry used to be a flat 30s, which was fine for the failure it was
# written for -- a one-off dropped connection -- and actively harmful for the one seen on
# 2026-08-08: aisstream accepted every connection, stopped answering keepalive pings, and the
# client closed with `1011 keepalive ping timeout` roughly 40s in (ping_interval 20 +
# ping_timeout 20, the websockets defaults, which are left alone deliberately -- detecting a
# dead peer is exactly what they are for). A fixed retry against that turns one dead upstream
# into a permanent reconnect every ~70s, and after two cycles aisstream started answering
# HTTP 429. We were part of the problem.
#
# The trap in the obvious fix: the connection SUCCEEDS every time. Backoff reset on
# "connected" would fire on every cycle and never engage at all. It resets only when a
# connection actually delivered a frame -- the one thing that distinguishes a working feed
# from a socket that opens and dies.
_RECONNECT_BASE_SEC   = 5      # a genuine blip deserves a fast retry
_RECONNECT_CAP_SEC    = 300    # backing off must not become never coming back
_RATE_LIMIT_FLOOR_SEC = 60     # 429 is the server naming us specifically; honour it
_RECONNECT_JITTER     = 0.25   # upward only, so it can never undercut the floor above


def _reconnect_delay(attempt: int, *, rate_limited: bool = False,
                     jitter: float | None = None) -> float:
    """Seconds to wait before retry number `attempt` (0-based).

    Pure, with the random draw injectable, so the policy can be tested without a clock.
    Jitter only ever adds: its job is to de-synchronise retries, and a downward jitter
    applied after the 429 floor would quietly reconnect faster than the server just asked.
    """
    delay = min(_RECONNECT_BASE_SEC * (2 ** attempt), _RECONNECT_CAP_SEC)
    if rate_limited:
        delay = max(delay, _RATE_LIMIT_FLOOR_SEC)
    if jitter is None:
        jitter = random.random()
    return delay * (1 + _RECONNECT_JITTER * jitter)


def _is_rate_limited(exc: BaseException) -> bool:
    """Whether a connect failure was the server refusing us for going too fast.

    websockets 16 raises `InvalidStatus` carrying the HTTP response; the status code is read
    off it rather than parsed out of str(exc), because the message text is not an API.
    """
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) == 429


async def _sleep(seconds: float) -> None:
    """Indirection so tests can drive the retry loop without waiting on a real clock."""
    await asyncio.sleep(seconds)


async def _ais_loop(api_key: str) -> None:
    sub_msg = json.dumps({
        "APIKey": api_key,
        "BoundingBoxes": ROTTERDAM_BBOX,
        "FilterMessageTypes": ["ShipStaticData", "PositionReport"],
    })

    import ssl as _ssl
    import certifi
    ssl_ctx = _ssl.create_default_context(cafile=certifi.where())

    consecutive_failures = 0
    while True:
        delivered = 0
        started = time.monotonic()
        rate_limited = False
        # A graceful close ends the `async for` without raising anything, so the reason has
        # to default to something sayable. The delay applies on that path too: pacing lived
        # inside `except` before, which meant a politely-closing server got a hot loop.
        reason = "closed by the server"
        try:
            async with websockets.connect("wss://stream.aisstream.io/v0/stream", ssl=ssl_ctx) as ws:
                await ws.send(sub_msg)
                print("[AIS] connected — watching Rotterdam / Maas Approach area", flush=True)
                # Watches the clock alongside the read loop rather than wrapping recv() in a
                # timeout: cancelling a recv() mid-frame is a way to lose messages, and the
                # only thing needed here is a periodic look at when the last one arrived.
                watchdog = (asyncio.create_task(_watch_silence(time.monotonic()))
                            if AIS_SILENCE_WARN_SEC > 0 else None)
                try:
                    async for raw in ws:
                        _process_ais(json.loads(raw))
                        delivered += 1
                finally:
                    if watchdog is not None:
                        watchdog.cancel()
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            rate_limited = _is_rate_limited(exc)

        # A connection that delivered nothing did not work, however cleanly it opened -- and
        # under the 2026-08-08 fault every one of them opens cleanly, so this is the only
        # signal that separates a working feed from a socket that accepts and dies.
        consecutive_failures = 0 if delivered else consecutive_failures + 1
        delay = _reconnect_delay(max(consecutive_failures - 1, 0), rate_limited=rate_limited)
        print(f"[AIS] disconnected after {time.monotonic() - started:.0f}s "
              f"having received {delivered} message(s) ({reason}), "
              f"reconnecting in {delay:.0f}s...", flush=True)
        await _sleep(delay)


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

# Radius in km from Maas Center for ADMISSION to the cache; 0 disables the filter.
#
# The purpose is pool reduction, not excluding any particular port. Measured over the
# 7,205 cached vessels that carry a position: 20 km admits 349, 30 km admits 654, 40 km
# admits 1,116 (15.5%), 100 km admits 5,878. Cutting the pool by 85% cuts the wrong-match
# surface, where the documented NORDIC SIRA / NORDIC SAGA failure came from.
#
# 40 is a starting point, not a finding. Too tight loses recall, too wide loses precision.
# Tune it against `bench_identify.py --labels ... --resolve --repeats 3`, which reports
# both with a spread. Note Scheveningen sits at 27.7 km, so NO radius separates it from
# inbound traffic -- do not expect this to do that.
AIS_LOCAL_MAX_KM = float(os.environ.get("AIS_LOCAL_MAX_KM", "40"))

# Kept here rather than imported from bench_identify: the proxy must not depend on a
# benchmarking script. Same coordinates as bench_identify._MAAS_CENTER.
_MAAS_CENTER = (52.02, 3.88)


def _km_from_maas(lat: float, lon: float) -> float:
    lat0, lon0 = _MAAS_CENTER
    dlat = math.radians(lat - lat0)
    dlon = math.radians(lon - lon0)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat0)) * math.cos(math.radians(lat))
         * math.sin(dlon / 2) ** 2)
    return 6371 * 2 * math.asin(math.sqrt(a))


_stale_filter_warned = False

# Seconds of total silence on a connected feed before saying so. 0 disables the check.
#
# The failure this exists for, observed 2026-08-07: aisstream accepted the connection,
# accepted the subscription, and then sent nothing -- for the entire session. No error, no
# close, no exception, so the reconnect handler never fired and "[AIS] connected" remained
# the last word on the subject. Meanwhile every lookup went on matching against a cache
# loaded from disk, three days stale, with total confidence. Confirmed external: a fresh key
# and a whole-world bounding box were equally silent (aisstream/aisstream#15).
#
# A feed that fails by going quiet is indistinguishable from a quiet feed unless something
# is watching the clock, which is the whole point of this. 60s is far longer than the
# seconds-apart cadence of a busy estuary, so it cannot fire on normal traffic.
AIS_SILENCE_WARN_SEC = int(os.environ.get("AIS_SILENCE_WARN_SEC", "60"))

# time.monotonic() of the last frame of any recognised type; None until the first arrives.
_last_message_at = None

_unknown_frames_logged = 0
_UNKNOWN_FRAME_LOG_LIMIT = 3


def _silence_report(last_message_at, connected_at, now, threshold):
    """The warning a quiet feed deserves, or None if it is behaving.

    Pure so the decision can be tested without a websocket or a clock. Distinguishes "went
    quiet mid-stream" from "never sent anything", because they point at different causes:
    the first is a stall or an emptied bounding box, the second is the subscription being
    accepted and ignored, which is a server-side or credential fault no retry will fix.
    """
    if threshold <= 0:
        return None

    if last_message_at is None:
        quiet = now - connected_at
        if quiet < threshold:
            return None
        return (f"[AIS] WARNING: connected {quiet:.0f}s ago and has received no data at all. "
                f"The subscription was accepted but nothing is being delivered -- check the "
                f"API key and aisstream.io status. Lookups are running against the cache "
                f"loaded from disk, whose age is unknown.")

    quiet = now - last_message_at
    if quiet < threshold:
        return None
    return (f"[AIS] WARNING: no AIS data for {quiet:.0f}s on a connected feed. "
            f"Cached positions are going stale.")


async def _watch_silence(connected_at: float) -> None:
    """Report a feed that has gone quiet, for as long as it stays quiet."""
    interval = max(AIS_SILENCE_WARN_SEC, 5)
    while True:
        await asyncio.sleep(interval)
        report = _silence_report(_last_message_at, connected_at,
                                 time.monotonic(), AIS_SILENCE_WARN_SEC)
        if report:
            print(report, flush=True)


def _report_unrecognised_frame(msg: dict) -> None:
    """Log a frame that is not an AIS message -- rate-limited, since a fault repeats.

    aisstream reports refusals as {"error": "..."} over an otherwise healthy socket. Such a
    frame carries no MMSI, so it used to be dropped by the `if not mmsi: return` guard and
    never seen -- the most diagnostic thing the server can say, discarded in silence.
    """
    global _unknown_frames_logged
    if _unknown_frames_logged >= _UNKNOWN_FRAME_LOG_LIMIT:
        return
    _unknown_frames_logged += 1
    detail = msg.get("error") or json.dumps(msg)[:200]
    tail = (" (further such frames will not be logged)"
            if _unknown_frames_logged == _UNKNOWN_FRAME_LOG_LIMIT else "")
    print(f"[AIS] server sent a non-AIS frame: {detail}{tail}", flush=True)


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


# MMSI -> the SAME entry object held in _vessel_cache. Raw AIS position reports (types
# 1/2/3) carry no vessel name, where aisstream enriched every one with MetaData.ShipName,
# so without this index a local position report has no way to find its vessel. It also
# retires match_by_mmsi's linear scan over ~8,600 entries.
_mmsi_index: dict[str, dict] = {}

# Observations for vessels not yet admitted, keyed by MMSI and accumulated across
# messages. Raw AIS splits a vessel across message types -- position without a name, name
# without a position -- so neither alone can decide admission.
#
# Deliberately NOT stored in _vessel_cache under a synthetic "MMSI:244..." key: the fuzzy
# name matcher iterates those keys, and junk keys would become candidates for matching.
_pending: dict[str, dict] = {}

_STATIC_FIELDS   = ("name", "callsign", "type", "imo", "length", "beam",
                    "draught", "destination")
_POSITION_FIELDS = ("latitude", "longitude", "sog", "cog", "heading")

# Upper bound on _pending. A vessel that never gets a name, or is named but never enters
# a radius filter that is switched on, would otherwise be re-applied and re-held on every
# message forever -- the proxy is a long-running process, so "forever" is real unbounded
# growth, not a rounding error. 2,000 is generously above the number of distinct MMSIs
# plausible near one receiver at once (the admitted cache itself holds ~8,600 vessels
# accumulated over weeks, not at once); if this cap is ever hit in practice that is a
# signal something upstream is wrong -- a stuck stream or a radius set far too wide -- not
# a reason to raise it blindly. Eviction is oldest-first by the observation's own
# timestamp (`when`), not wall-clock arrival, so it stays deterministic under backdated
# `observed_at` the same way position freshness already is.
_PENDING_MAX = 2000


def record(fields: dict, *, source: str, observed_at: float | None = None) -> None:
    """Merge one observation into the vessel cache, whatever provider saw it.

    One implementation on purpose. The merge is where the subtle bugs lived: static
    messages wholesale-replacing position data left 25% of the vessels in the labelled
    conversations with no position at all until the MERGE-never-replace fix. Two providers
    writing the cache through two code paths would be two chances to get that wrong, with
    only one of them covered by these tests.

    `observed_at` is a UNIX timestamp for the observation; it defaults to now. Position
    writes apply only if newer than the stored fix.
    """
    mmsi = str(fields.get("mmsi") or "").strip()
    if not mmsi:
        return
    when = time.time() if observed_at is None else observed_at

    with _cache_lock:
        entry = _mmsi_index.get(mmsi)
        if entry is None:
            name = (fields.get("name") or "").strip()
            if name:
                candidate = _vessel_cache.get(name.upper())
                # Adopt a name-keyed entry only if its MMSI agrees, or it doesn't have one
                # yet. Vessel names collide -- a fuzzy search for "mistral" in the live
                # cache returns four distinct vessels -- so matching on name alone would
                # permanently alias a second vessel's MMSI onto the first's entry, and
                # "mmsi" is not in _STATIC_FIELDS so nothing would ever correct it. A real
                # name collision still collides in _vessel_cache as it always has; this
                # only stops the MMSI index from silently merging two different ships.
                if candidate is not None and candidate.get("mmsi") in (mmsi, None, ""):
                    entry = candidate

        if entry is not None:
            # A position (or other observation) for this MMSI seen before it was
            # admitted -- held in _pending because nothing existed yet to attach it to
            # -- must not be silently discarded now that something does. Flushed BEFORE
            # the current observation so _apply's newest-wins position logic still picks
            # whichever is actually newer; using pending's own recorded position_at (not
            # `when`) keeps that comparison honest. Without this, the held fix sat
            # orphaned in _pending until the 2000-cap eventually evicted it -- and a
            # Class-B vessel (type 18), which never sends the static message that would
            # retrigger this adoption path, got no position update for the entire run.
            pending = _pending.pop(mmsi, None)
            if pending is not None:
                _apply(entry, pending, pending.get("position_at", when),
                       pending.get("source", source))

            # Already admitted: the radius gates admission, not later updates. Rejecting
            # an update for a vessel that has moved out would freeze a stale fix.
            _apply(entry, fields, when, source)
            _mmsi_index[mmsi] = entry
            if entry.get("callsign"):
                _callsign_cache[entry["callsign"].upper()] = entry
            return

        # Not yet admitted: accumulate until there is a name and, with the filter on, a
        # position inside the radius.
        held = _pending.setdefault(mmsi, {"mmsi": mmsi})
        _apply(held, fields, when, source)
        held["_touched"] = when

        if len(_pending) > _PENDING_MAX:
            oldest_mmsi = min(_pending, key=lambda k: _pending[k].get("_touched", 0.0))
            if oldest_mmsi != mmsi:
                del _pending[oldest_mmsi]

        if not (held.get("name") or "").strip():
            return
        if AIS_LOCAL_MAX_KM > 0:
            lat, lon = held.get("latitude"), held.get("longitude")
            if lat is None or lon is None:
                return
            if _km_from_maas(lat, lon) > AIS_LOCAL_MAX_KM:
                return

        entry = _pending.pop(mmsi)
        entry.pop("_touched", None)
        entry.setdefault("callsign", "")
        # Every static field gets a key even when no observation ever carried a value for
        # it, not just the four checked here: consumers (the /identified-vessels page, the
        # tests) index these directly rather than through .get, the way the pre-record()
        # code always populated them via a dict literal. draught and destination were
        # missing from this list originally, which left the key absent entirely for a
        # vessel whose first ShipStaticData had no draught or an all-padding destination --
        # a KeyError on the direct index, caught by
        # test_destination_padding_is_stripped[None cases].
        for key in ("type", "imo", "length", "beam", "draught", "destination"):
            entry.setdefault(key, None)
        _vessel_cache[entry["name"].upper()] = entry
        _mmsi_index[mmsi] = entry
        if entry.get("callsign"):
            _callsign_cache[entry["callsign"].upper()] = entry


def _apply(entry: dict, fields: dict, when: float, source: str) -> None:
    """Merge one observation's fields into `entry`, in place.

    Static fields fill or update. Position applies only if this observation is newer than
    the stored fix -- newest-wins rather than a blanket 'local always overwrites', because
    a vessel heard locally two hours ago and now out of VHF range must not keep a stale fix
    over a fresh remote one. In practice local AIS is real-time and wins essentially
    always, so this delivers the intent without its pathological case.

    A static field of "" is treated the same as absent (missing/None), never written.
    2026-08-09: `_process_ais`'s PositionReport branch passes `meta.get("ShipName",
    "").strip()` unconditionally, so a bare PositionReport with no MetaData.ShipName was
    handing this "" for `name` -- and `fields.get(key) is not None` let it through, blanking
    an already-admitted vessel's name. The old pre-record() code guarded that one case with
    `if name:`. Generalised here rather than fixed only in the aisstream adapter: "" is
    exactly as uninformative as a missing key for every field record() recognises -- none of
    them has a legitimate use for an explicit empty string as data -- and `callsign` reaches
    this function the same way (aisstream defaults `CallSign` to "" too), so a future
    adapter hitting the same shape is safe by construction instead of by convention.
    """
    applied = False
    for key in _STATIC_FIELDS:
        value = fields.get(key)
        if value is not None and value != "":
            entry[key] = value
            applied = True

    if fields.get("latitude") is not None and fields.get("longitude") is not None:
        if when >= entry.get("position_at", float("-inf")):
            for key in _POSITION_FIELDS:
                if key in fields:
                    entry[key] = fields[key]
            entry["position_at"] = when
            applied = True

    if applied:
        entry["source"] = source
        entry["last_seen"] = _now()


def _process_ais(msg: dict) -> None:
    """aisstream adapter over record(). Kept thin on purpose: the merge lives in one place.

    aisstream enriches PositionReport with MetaData.ShipName, which raw AIS does not --
    that difference is exactly why the recorder holds an MMSI index.
    """
    global _last_message_at
    try:
        msg_type = msg.get("MessageType", "")
        if not msg_type:
            _report_unrecognised_frame(msg)
            return

        # Before the MMSI guard, deliberately: this records that the feed is ALIVE, which
        # is true of any well-formed frame whether or not it names a usable vessel.
        _last_message_at = time.monotonic()

        meta = msg.get("MetaData", {})
        mmsi = str(meta.get("MMSI", "")).strip()
        if not mmsi:
            return

        if msg_type == "ShipStaticData":
            ship = msg.get("Message", {}).get("ShipStaticData", {})
            name = (ship.get("Name") or meta.get("ShipName") or "").strip()
            if not name:
                return
            dim = ship.get("Dimension", {})
            record({
                "mmsi": mmsi, "name": name,
                "callsign": ship.get("CallSign", "").strip(),
                "type": ship.get("Type"), "imo": ship.get("ImoNumber"),
                "length": (dim.get("A", 0) + dim.get("B", 0)) or None,
                "beam": (dim.get("C", 0) + dim.get("D", 0)) or None,
                "draught": ship.get("MaximumStaticDraught"),
                "destination": _clean_destination(ship.get("Destination", "")),
            }, source="aisstream")

        elif msg_type == "PositionReport":
            pos = msg.get("Message", {}).get("PositionReport", {})
            record({
                "mmsi": mmsi, "name": meta.get("ShipName", "").strip(),
                "latitude": pos.get("Latitude"), "longitude": pos.get("Longitude"),
                "sog": pos.get("Sog"), "cog": pos.get("Cog"),
                "heading": pos.get("TrueHeading"),
            }, source="aisstream")
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

# Off until bench_identify --resolve has scored it end to end. See _unique_word_match for the
# class it fixes and test_the_word_path_is_off_until_it_has_been_measured_end_to_end for the
# four false answers that keep it off. AIS_NAME_WORD_MATCH=on to measure.
AIS_NAME_WORD_MATCH = os.environ.get("AIS_NAME_WORD_MATCH", "off").strip().lower() == "on"

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


def _unique_word_match(token: str, keys: list[str]) -> str | None:
    """The one multi-word cache name having `token` as a whole word, or None.

    Traffic calls a ship by a distinguishing word of its name -- "the Townsend" for BERGE
    TOWNSEND -- and whisper drops words on its own. fuzz.ratio cannot serve that: it scores
    whole strings, so the correct longer name is penalised for length the query does not
    have while a similar-looking SHORT name wins outright. Measured on the live cache over
    1,653 single-word references, the fuzzy path alone was 19.0% right and 43.2% wrong.

    Exact, never fuzzy, and None the moment two ships share the word -- WILSON fits both
    WILSON CORK and WILSON GAETA, and a confident wrong ship is worse here than no ship.
    Same contract as match_by_callsign_suffix, for the same reason.
    """
    if len(token) < AIS_NAME_MIN_TOKEN or token in _NAME_SKIP:
        return None
    owners = [name for name in keys if " " in name and token in name.split()]
    return owners[0] if len(owners) == 1 else None


def match_by_name(extracted_name: str) -> dict | None:
    if not extracted_name:
        return None
    query = extracted_name.upper()
    keys, cache = _fresh_snapshot()
    if not keys:
        return None
    cutoff = AIS_NAME_MIN_SCORE if AIS_NAME_FILTER else 80

    if AIS_NAME_FILTER and AIS_NAME_WORD_MATCH:
        # Ahead of the fuzzy pass, because fuzzy answers first and answers wrongly: TASMAN
        # scores 85.7 against TALISMAN and only 70.6 against the ABEL TASMAN that was meant.
        # Behind an exact whole-name hit, because someone who says BRAVO means the ship
        # called BRAVO, not ALFA BRAVO.
        if query in cache:
            return cache[query]
        if " " not in query:
            word_hit = _unique_word_match(query, keys)
            if word_hit:
                return cache[word_hit]

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


def match_by_callsign_suffix(suffix: str) -> dict | None:
    """The one cached vessel whose callsign ENDS with `suffix`, or None.

    Tail-anchored, and that is the whole point. On 2026-08-08 the live cache held 8,008
    callsigns: 79 of them contain "PB8", so a substring search identifies nothing, while
    exactly one ends with it -- 2FPB8, BERGE TOWNSEND, the ship that was actually calling.
    A spelled-out callsign that survives STT only partly tends to lose its opening
    characters to the noise at the start of a transmission, so the tail is what is left.

    Ambiguity returns None, as with `match_by_callsign_pattern`. It has to: a 3-character
    tail is unique for only 23% of the cache, so this is a filter that usually declines.
    """
    wanted = (suffix or "").upper()
    if len(wanted) < 3:
        return None
    with _cache_lock:
        entries = list(_callsign_cache.items())
    found = None
    for callsign, entry in entries:
        if callsign.upper().endswith(wanted):
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
