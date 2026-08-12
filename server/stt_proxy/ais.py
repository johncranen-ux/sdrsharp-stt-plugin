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
        names = set()
        with _cache_lock:
            for entry in entries:
                _vessel_cache[entry["name"].upper()] = entry
                if entry.get("mmsi"):
                    _mmsi_index[str(entry["mmsi"])] = entry
                if entry.get("callsign"):
                    _callsign_cache[entry["callsign"].upper()] = entry
                _index_name(entry)
                names.add(entry["name"].strip().upper())
            # The loop above seeds _vessel_cache last-entry-in-file-wins, same as record()'s
            # admission branch. Now that every entry AND _mmsi_index are fully loaded, re-pick
            # the best candidate for each name -- the same seed-then-refresh composition
            # record() uses, and for the same reason: without this, a reloaded cache stays
            # keyed on whichever duplicate happened to be written last, not the best-ranked
            # one, until a poll touches that name again.
            for name in names:
                _refresh_name_view(name)
        print(f"[AIS] loaded {len(_vessel_cache)} vessels from cache", flush=True)
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"[AIS] cache load error: {exc}", flush=True)


def _save_cache() -> None:
    try:
        with _cache_lock:
            # Persist from _mmsi_index, not _vessel_cache: _vessel_cache holds only the single
            # best-ranked entry per NAME (Task 4), so saving from it would silently discard
            # every non-best duplicate on every save -- exactly the ALBATROS x14 problem this
            # index exists to solve, reintroduced across a restart. _mmsi_index holds every
            # ship; id()-dedup guards against two MMSI keys ever aliasing the same dict, which
            # should not happen but would otherwise double an entry in the saved file.
            seen_ids = set()
            entries = []
            for entry in _mmsi_index.values():
                if id(entry) not in seen_ids:
                    seen_ids.add(id(entry))
                    entries.append(entry)
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
#
# DEFAULT CHANGED TO 0 (off) 2026-08-11. The instrument is correct and the diagnosis it
# reports is true -- which is exactly the problem. aisstream has delivered nothing since
# 2026-08-05 13:31 UTC, so this fires every 60s forever, ~8,600 times and counting, and
# drowns the console output that is still worth reading. A warning that is permanently on
# carries no information; it only costs attention.
#
# This is a mute, not a removal: the mechanism is the only thing that would catch aisstream
# failing again after it recovers. Restore with AIS_SILENCE_WARN_SEC=60 (there is a
# commented line in start-all.bat), and do so the moment the feed comes back.
AIS_SILENCE_WARN_SEC = int(os.environ.get("AIS_SILENCE_WARN_SEC", "0"))

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


# Vessels by MMSI -- the only identifier that actually distinguishes two ships. _vessel_cache
# is keyed by name, and names collide: a live AISHub snapshot of the Maas approach carries 17
# duplicate-name groups (ALBATROS x3, CORNELIA x3), and the wider box carries 777. Without
# this index those merge into one entry and take the MMSI of whichever spoke last.
_mmsi_index: dict[str, dict] = {}

# Observations for vessels not yet admitted, keyed by MMSI and accumulated across messages.
# Raw AIS splits a vessel across message types -- position without a name, name without a
# position -- so neither alone can decide admission.
#
# Deliberately NOT stored in _vessel_cache under a synthetic "MMSI:244..." key: the fuzzy name
# matcher iterates those keys, and junk keys would become candidates for matching.
_pending: dict[str, dict] = {}

# MMSIs returned by the most recent SUCCESSFUL poll. Empty means "no source has reported yet"
# and is treated as "everything is in scope", so aisstream and a cold start behave as before.
#
# Scope is defined against the last good poll rather than against wall-clock age deliberately.
# "last_seen within N minutes of now" would make a feed outage indistinguishable from every
# ship leaving the estuary -- and this project has already lost six days to a feed that failed
# quietly.
_in_scope: set[str] = set()


def set_in_scope(mmsis: set[str]) -> None:
    """Publish the vessels the latest good poll saw, and re-rank every name view against it.

    Called only on success, and (by `aishub.poll_once`) only AFTER that poll has finished
    writing every vessel -- publishing the new scope is what makes every ranking decision
    `record()` made during the write loop stale, since each of those used whatever scope was
    published BEFORE this call (the previous poll's, or none). Rather than wait for some
    future record() to touch a given name and refresh it incidentally, this re-ranks every
    known name itself, in the same lock acquisition that publishes the new scope, so
    _vessel_cache is never observably stale against the scope it should be ranked by. Cost is
    bounded: _refresh_name_view over ~7,900 names at ~51us each is well under a second,
    against a 900s poll interval.

    Calls _refresh_name_view directly rather than through record() -- this already holds
    _cache_lock, and _refresh_name_view's contract (documented on itself) is exactly "caller
    holds _cache_lock, reads _in_scope directly, never calls get_in_scope()". Do not call
    get_in_scope() from in here; that reacquires the lock and deadlocks the feed thread the
    same way a `_refresh_name_view` call from inside `record()` would.
    """
    global _in_scope
    with _cache_lock:
        _in_scope = set(mmsis)
        for name in _name_index:
            _refresh_name_view(name)


def get_in_scope() -> set[str]:
    with _cache_lock:
        return set(_in_scope)


# NAME -> the MMSIs of every ship carrying it. _vessel_cache can only hold one entry per name,
# so this is the only thing that keeps fourteen ALBATROS apart. Ranking them is Task 4's job;
# this task only has to stop them overwriting each other.
_name_index: dict[str, list[str]] = {}

_STATIC_FIELDS   = ("name", "callsign", "type", "imo", "length", "beam",
                    "draught", "destination")
_POSITION_FIELDS = ("latitude", "longitude", "sog", "cog", "heading")

# Upper bound on _pending. A vessel that never gets a name would otherwise be re-held on every
# message forever -- the proxy is long-running, so "forever" is real unbounded growth. If this
# cap is ever hit that is a signal something upstream is wrong, not a reason to raise it.
# Eviction is oldest-first by the observation's own timestamp, so it stays deterministic under
# a backdated observed_at the same way position freshness does.
_PENDING_MAX = 2000


def record(fields: dict, *, source: str, observed_at: float | None = None) -> None:
    """Merge one observation into the vessel cache, whatever provider saw it.

    One implementation on purpose. The merge is where the subtle bugs lived: static messages
    wholesale-replacing position data left 25% of the vessels in the labelled conversations
    with no position at all until the MERGE-never-replace fix. Two providers writing the cache
    through two code paths would be two chances to get that wrong, with only one covered by
    these tests.

    `observed_at` is a UNIX timestamp for the observation; it defaults to now. Position writes
    apply only if newer than the stored fix.
    """
    mmsi = str(fields.get("mmsi") or "").strip()
    if not mmsi:
        return
    when = time.time() if observed_at is None else observed_at
    stamp_now = observed_at is None

    with _cache_lock:
        entry = _mmsi_index.get(mmsi)
        if entry is None:
            name = (fields.get("name") or "").strip()
            if name:
                candidate = _vessel_cache.get(name.upper())
                # Adopt a name-keyed entry only if its MMSI agrees, or it doesn't have one
                # yet. Matching on name alone would permanently alias a second vessel's MMSI
                # onto the first's entry, and "mmsi" is not in _STATIC_FIELDS so nothing would
                # ever correct it.
                if candidate is not None and candidate.get("mmsi") in (mmsi, None, ""):
                    entry = candidate
                    # The candidate's mmsi was missing or "" -- fill it in now. Without this,
                    # entry["mmsi"] stays falsy and _index_name() below silently drops the
                    # vessel from _name_index (it early-returns on a falsy mmsi), even though
                    # _mmsi_index[mmsi] correctly points at it.
                    entry["mmsi"] = mmsi

        if entry is not None:
            # An observation for this MMSI seen before it was admitted -- held in _pending
            # because nothing existed to attach it to -- must not be discarded now that
            # something does. Flushed BEFORE the current observation so the newest-wins
            # position logic still picks whichever is actually newer.
            pending = _pending.pop(mmsi, None)
            if pending is not None:
                _apply(entry, pending, pending.get("position_at", when),
                       pending.get("source", source))

            _apply(entry, fields, when, source, stamp_now=stamp_now)

            # A rename (ShipStaticData.Name differing from the MetaData.ShipName the vessel
            # was first admitted under) must not leave the cache keyed on the stale name --
            # _fresh_snapshot hands _vessel_cache.keys() straight to the fuzzy matcher, so a
            # vessel invisible under its own current name is a vessel that cannot be found.
            key = entry["name"].upper()
            if _vessel_cache.get(key) is not entry:
                _vessel_cache[key] = entry

            _mmsi_index[mmsi] = entry
            _index_name(entry)
            _refresh_name_view(entry.get("name", ""))
            if entry.get("callsign"):
                _callsign_cache[entry["callsign"].upper()] = entry
            return

        # Not yet admitted: accumulate until there is a name.
        held = _pending.setdefault(mmsi, {"mmsi": mmsi})
        _apply(held, fields, when, source, stamp_now=stamp_now)
        held["_touched"] = when

        if len(_pending) > _PENDING_MAX:
            oldest_mmsi = min(_pending, key=lambda k: _pending[k].get("_touched", 0.0))
            if oldest_mmsi != mmsi:
                del _pending[oldest_mmsi]

        if not (held.get("name") or "").strip():
            return

        entry = _pending.pop(mmsi)
        entry.pop("_touched", None)
        entry.setdefault("callsign", "")
        # Every static field gets a key even when no observation carried a value: consumers
        # index these directly rather than through .get, the way the pre-record() code always
        # populated them via a dict literal.
        for key in ("type", "imo", "length", "beam", "draught", "destination"):
            entry.setdefault(key, None)
        _vessel_cache[entry["name"].upper()] = entry
        _mmsi_index[mmsi] = entry
        _index_name(entry)
        _refresh_name_view(entry["name"])
        if entry.get("callsign"):
            _callsign_cache[entry["callsign"].upper()] = entry


def _index_name(entry: dict) -> None:
    """Record this MMSI under its name. Caller holds _cache_lock."""
    name = (entry.get("name") or "").strip().upper()
    mmsi = str(entry.get("mmsi") or "").strip()
    if not name or not mmsi:
        return
    holders = _name_index.setdefault(name, [])
    if mmsi not in holders:
        holders.append(mmsi)


# How likely a vessel of this type is to be working Maas Approach. Used only to break ties
# between ships that share a name, never to exclude anything: a sailing yacht CAN call, it is
# just the least likely of several candidates at the same place.
_TYPE_PLAUSIBILITY = {
    "Tanker": 3, "General cargo": 3, "Container ship": 3, "Bulk carrier": 3,
    "Cargo ship": 3, "Passenger ship": 3,
    "Sailing": 1, "Pleasure craft": 1,
}
_TYPE_PLAUSIBILITY_DEFAULT = 2


def _type_plausibility(type_code) -> int:
    return _TYPE_PLAUSIBILITY.get(_get_ship_type_name(type_code),
                                  _TYPE_PLAUSIBILITY_DEFAULT)


def _candidate_sort_key(entry: dict, in_scope: set[str]) -> tuple:
    """Sort key for one candidate; lower sorts first.

    Order: in scope, then nearest Maas Center, then most plausible type, then most recent fix.
    Proximity outranks type because it discriminates even when every candidate is equally
    live -- which is the case that actually occurs, with 17 duplicate-name groups
    simultaneously present in the approach box.
    """
    mmsi = str(entry.get("mmsi") or "")
    out_of_scope = 1 if (in_scope and mmsi not in in_scope) else 0

    lat, lon = entry.get("latitude"), entry.get("longitude")
    km = _km_from_maas(lat, lon) if lat is not None and lon is not None else float("inf")

    return (out_of_scope, km, -_type_plausibility(entry.get("type")),
            -entry.get("position_at", 0.0))


def candidates_for_name(name: str) -> list[dict]:
    """Every cached vessel carrying exactly this name, best first.

    Exact-name only. Fuzzy matching happens a layer up in match_by_name, which then asks this
    for the ships behind the name it landed on.

    Reads _mmsi_index directly rather than _fresh_snapshot(), so AIS_MAX_AGE_MIN does not
    filter here. Deliberate and currently inert: that setting defaults to 0, and the in-scope
    set is what replaces it -- scope against the last good poll rather than against wall-clock
    age, which is the distinction a feed outage turns on. The caller still derives its
    searchable NAMES from _fresh_snapshot(), so the age filter still bounds what can be found.
    """
    key = (name or "").strip().upper()
    if not key:
        return []
    in_scope = get_in_scope()
    with _cache_lock:
        # _name_index is append-only (Task 1): a renamed vessel's old name keeps listing its
        # MMSI forever. Filtering by the entry's CURRENT name excludes ships that used to
        # carry this name but no longer do -- otherwise a rename leaves a ghost candidate
        # here indefinitely, contradicting "every cached vessel carrying exactly this name".
        entries = [_mmsi_index[m] for m in _name_index.get(key, [])
                   if m in _mmsi_index and _mmsi_index[m].get("name", "").strip().upper() == key]
    # sorted() runs after the lock is released, dereferencing dicts the feed thread may be
    # concurrently mutating. Deliberate, not an oversight: _apply() only ever writes
    # latitude/longitude together, never leaves one None while the other is set, so the worst
    # case is one candidate ranked on a fix that finishes updating a moment later -- never a
    # crash. Locking around the sort too would serialise every lookup behind the feed thread
    # for no correctness gain.
    return sorted(entries, key=lambda e: _candidate_sort_key(e, in_scope))


def _refresh_name_view(name: str) -> None:
    """Point _vessel_cache at the best candidate for this name. Caller holds _cache_lock.

    _vessel_cache stays {NAME: entry} rather than becoming {NAME: [entry]}: twenty production
    call sites and a large number of test fixtures index it that way, and it holds references
    to the same dicts, so this is an ordering choice and not a second copy of the data.

    Reads _in_scope directly rather than calling get_in_scope() -- this runs inside record(),
    which already holds _cache_lock, and that lock is not reentrant. Do not "tidy" this into
    get_in_scope(); that reacquires the lock and deadlocks the feed thread.

    Filters holders down to entries whose CURRENT name still matches `key`, the same fix as
    candidates_for_name and for the same reason: _name_index is append-only, so a renamed
    vessel's old name keeps listing its MMSI here forever. Without the filter, a vacated name
    could stay pointed at the renamed vessel's (now differently-named) entry indefinitely;
    with it, calling this again for the vacated name lets the genuine holder reclaim it.
    """
    key = (name or "").strip().upper()
    holders = _name_index.get(key, [])
    entries = [_mmsi_index[m] for m in holders
               if m in _mmsi_index and _mmsi_index[m].get("name", "").strip().upper() == key]
    if not entries:
        return
    in_scope = set(_in_scope)
    _vessel_cache[key] = min(entries, key=lambda e: _candidate_sort_key(e, in_scope))


def _apply(entry: dict, fields: dict, when: float, source: str, *, stamp_now: bool = False) -> None:
    """Merge one observation's fields into `entry`, in place.

    Static fields fill or update. Position applies only if this observation is newer than the
    stored fix -- newest-wins, so a vessel heard two hours ago does not keep a stale fix over
    a fresh one.

    A static field of "" is treated the same as absent, never written: "" is exactly as
    uninformative as a missing key for every field record() recognises, and adapters routinely
    default absent strings to "".

    `last_seen` is stamped from `when` -- the OBSERVATION time -- not from the clock -- UNLESS
    `stamp_now` is set, in which case it goes through `_now()` instead. The two are
    value-equivalent whenever `when` is `time.time()` (aisstream's case, `record()` passing no
    `observed_at`): `fromtimestamp(time.time())` and `_now()`'s `datetime.now()` land on the
    same wall-clock second either way. But they are not the same CALL, and `_now()` is what the
    rest of this module -- including a pre-existing test -- patches to control "the current
    time"; `stamp_now` is what lets that keep working instead of the mock silently doing
    nothing. AISHub's explicit `observed_at` always leaves `stamp_now` False, since its TIME
    field is when the position was reported and making last_seen true to that is the whole
    reason for adopting it.

    LOCAL time, not UTC, and that is not an oversight. _now() used datetime.now() and
    _is_fresh compares the parsed stamp against a naive local cutoff, so every last_seen in
    the cache and in the stored conversations is local wall-clock. Writing UTC here would
    shift new entries two hours away from the old ones and silently break the freshness
    comparison. parse_time() resolves AISHub's GMT stamp to a true epoch first, so the
    conversion is correct rather than merely consistent.
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
        entry["last_seen"] = (_now() if stamp_now else
                               datetime.datetime.fromtimestamp(when).strftime(_LAST_SEEN_FMT))


def _process_ais(msg: dict) -> None:
    """aisstream adapter over record(). Kept thin on purpose: the merge lives in one place."""
    global _last_message_at
    try:
        msg_type = msg.get("MessageType", "")
        if not msg_type:
            _report_unrecognised_frame(msg)
            return

        # Before the MMSI guard below, deliberately: this records that the feed is ALIVE,
        # which is true of any well-formed frame whether or not it names a usable vessel.
        _last_message_at = time.monotonic()

        meta = msg.get("MetaData", {})
        mmsi = str(meta.get("MMSI", "")).strip()
        if not mmsi:
            return

        if msg_type == "ShipStaticData":
            ship = msg.get("Message", {}).get("ShipStaticData", {})
            dim  = ship.get("Dimension", {})
            record({
                "mmsi": mmsi,
                "name": (ship.get("Name") or meta.get("ShipName") or "").strip(),
                "callsign": ship.get("CallSign", "").strip(),
                "type": ship.get("Type"),
                "imo": ship.get("ImoNumber"),
                "length": (dim.get("A", 0) + dim.get("B", 0)) or None,
                "beam": (dim.get("C", 0) + dim.get("D", 0)) or None,
                "draught": ship.get("MaximumStaticDraught"),
                "destination": _clean_destination(ship.get("Destination", "")),
            }, source="aisstream")

        elif msg_type == "PositionReport":
            pos = msg.get("Message", {}).get("PositionReport", {})
            record({
                "mmsi": mmsi,
                "name": meta.get("ShipName", "").strip(),
                "latitude": pos.get("Latitude"),
                "longitude": pos.get("Longitude"),
                "sog": pos.get("Sog"),
                "cog": pos.get("Cog"),
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

# Two cache names within this many points of each other are a tie, not a winner and a loser.
# Measured: "Delta" scores 83.3 against both DELTA 3 and DELTA D, and one dropped letter puts
# VOLGA MAERSK and VAGA MAERSK 4.7 apart. 3.0 catches the exact ties and the tightest
# near-misses without flagging the ordinary 13-point gap of clean speech as contested.
AIS_NAME_AMBIGUOUS_GAP = float(os.environ.get("AIS_NAME_AMBIGUOUS_GAP", "3.0"))

_NAME_SKIP = {"MV", "MT", "MS", "SV", "SS", "TUG", "MOTOR", "TANKER",
              "BULKER", "VESSEL", "CONTAINER", "MOTORTANKER", "MOTORVESSEL"}


def _scored_name_matches(query: str, keys: list[str], cutoff: int) -> list[tuple[str, float]]:
    """(name, score) for every cache name at or above `cutoff`, best first.

    _best_name_match keeps only the winner, which is what made a tie invisible: it used
    `score > best[1]`, so an exact draw was settled by list order and reported as an
    identification. This keeps the runners-up so the caller can see a close call.

    AIS_NAME_FILTER=off is a documented full revert (see the block comment above
    AIS_NAME_FILTER) to the pre-fix WRatio scorer with no short-name guard, and
    test_name_filter_can_be_disabled pins that it reproduces the old bug exactly. This branch
    exists so match_by_name_candidates -- which now owns all scoring -- still honours that
    revert instead of silently always using the guarded ratio scorer.
    """
    if not AIS_NAME_FILTER:
        hits = rf_process.extract(query, keys, scorer=rf_fuzz.WRatio,
                                  limit=None, score_cutoff=cutoff)
        return sorted([(name, score) for name, score, _ in hits], key=lambda pair: -pair[1])

    hits = rf_process.extract(query, keys, scorer=rf_fuzz.ratio,
                              limit=None, score_cutoff=cutoff)
    kept = [(name, score) for name, score, _ in hits
            if len(name.replace(" ", "")) >= AIS_NAME_MIN_TOKEN or name == query]
    return sorted(kept, key=lambda pair: -pair[1])


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
    """The single best vessel for a heard name, or None.

    Unchanged contract for the live path. It is now the head of the candidate ranking rather
    than the highest fuzzy score, so a tie is settled by presence and proximity instead of by
    list order.
    """
    candidates = match_by_name_candidates(extracted_name)
    return candidates[0] if candidates else None


def match_by_name_candidates(extracted_name: str) -> list[dict]:
    """Every vessel a heard name plausibly refers to, best first.

    Two sources of ambiguity, and both matter:
      - several cache NAMES score within AIS_NAME_AMBIGUOUS_GAP of the best ("Delta" against
        DELTA 3 and DELTA D at 83.3 apiece);
      - one name carried by several SHIPS (FORTUNA twice, ALBATROS three times).

    Returns [] when nothing matches, and a single-element list when the identification is
    clear -- so a caller can treat len() > 1 as "contested" without a second rule.
    """
    if not extracted_name:
        return []
    query = extracted_name.upper()
    keys, cache = _fresh_snapshot()
    if not keys:
        return []

    cutoff = AIS_NAME_MIN_SCORE if AIS_NAME_FILTER else 80
    scored = _scored_name_matches(query, keys, cutoff)
    if not scored:
        words = [w for w in query.split() if w not in _NAME_SKIP and len(w) >= 3]
        probes = []
        for length in range(len(words), 0, -1):
            for start in range(len(words) - length + 1):
                probes.append(" ".join(words[start:start + length]))
        for probe in probes:
            scored = _scored_name_matches(probe, keys, cutoff)
            if scored:
                break
    if not scored:
        return []

    best = scored[0][1]
    names = [name for name, score in scored if best - score <= AIS_NAME_AMBIGUOUS_GAP]

    in_scope = get_in_scope()
    out: list[dict] = []
    seen: set[str] = set()
    for name in names:
        # candidates_for_name reads _mmsi_index/_name_index, which only record() populates.
        # Falling back to the plain _fresh_snapshot() entry keeps this correct for a vessel
        # cache written directly (record() is the only production path, but several tests
        # pre-dating Task 4 build _vessel_cache by hand) -- the same single entry match_by_name
        # returned before candidate expansion existed.
        entries = candidates_for_name(name)
        if not entries and name in cache:
            entries = [cache[name]]
        for entry in entries:
            mmsi = str(entry.get("mmsi") or "")
            if mmsi and mmsi in seen:
                continue
            seen.add(mmsi)
            out.append(entry)
    return sorted(out, key=lambda e: _candidate_sort_key(e, in_scope))


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
