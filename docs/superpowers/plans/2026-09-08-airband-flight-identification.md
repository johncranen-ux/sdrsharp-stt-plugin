# Airband Flight Identification (v1, API-only) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Resolve a heard callsign in airband transcripts against a live ADS-B feed and prefix
it onto the displayed text, the same way vessel names are already resolved against AIS.

**Architecture:** Two new modules mirroring the existing AIS pipeline's shape —
`stt_proxy/adsb.py` (a threaded poll loop against adsb.fi's free point/radius API, building an
in-memory cache of live aircraft keyed by ICAO hex) and `stt_proxy/flight_identify.py`
(extracts a candidate callsign from corrected transcript text, fuzzy-matches it against the
live cache, formats a `[CALLSIGN/TYPE]` prefix). One new call site in `whisper-proxy.py`'s
existing airband branch, restricted to the channels that actually carry callsigns.

**Tech Stack:** Python 3.10+, stdlib `urllib`/`json`/`threading` (matches `aishub.py`'s
zero-extra-dependency approach), `rapidfuzz` (already a dependency), `pytest`.

**Spec:** `docs/superpowers/specs/2026-09-08-airband-flight-identification-design.md`

## Global Constraints

- Data source is **adsb.fi** (`https://opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}`),
  confirmed live and open with no API key during design. airplanes.live is rejected — verified
  403, requires manual approval.
- No Claude/LLM extraction in this version — regex/dictionary + `rapidfuzz` only.
- No conversation-level resolution — single-transmission matching only.
- No local ADS-B receiver — API-only. A second dongle is explicitly deferred.
- Cache is a **full-replace snapshot per poll**, not an accumulating merge like `ais.py`'s.
  This is a deliberate deviation from the AIS cache model: aircraft are only relevant for the
  ~30-60s they're within radio range of this station, unlike ships which stay meaningfully
  "in scope" for hours. Accumulating stale aircraft would only get in the way of matching, not
  help it, so there is no AIS-style staleness/in-scope machinery to build here.
- Airline-code fuzzy matching is restricted to words of 5+ characters. `corrections.py` already
  documents that fuzzy-matching short tokens (its NATO phonetic-letter table) was tried and
  **rejected** — at any threshold that catches real garbling, `"gulf"`/`"golf"` (ratio 75)
  scores the same as unrelated common words like `"the"`/`"three"`. `"KLM"` is 3 characters, in
  the same danger zone. It is handled by an explicit variant dictionary (real garbled forms
  seen in this session's own log — `kalm`, `rklm`, `klmx`), the same policy `corrections.py`
  already uses for its phonetic table: *"variants are added only where a real transmission
  produced them."*

---

## File Structure

- **Create:** `server/stt_proxy/adsb.py` — poll loop, live aircraft cache, feed-status tracking.
- **Create:** `server/tests/test_adsb.py` — unit tests for `adsb.py`, synthetic fixtures only.
- **Create:** `server/stt_proxy/flight_identify.py` — callsign extraction, fuzzy matching
  against the live cache, plugin-display formatting.
- **Create:** `server/tests/test_flight_identify.py` — unit tests for `flight_identify.py`.
- **Modify:** `server/whisper-proxy.py` — one new call in the existing
  `elif mode == "airband":` branch (currently at line ~459), a new `APPROACH_TOWER_CHANNELS`
  constant, and imports for the two new modules, following the same `from stt_proxy import X`
  plus `from stt_proxy.X import (...)` pattern already used for `ais`/`aishub`/`backends`.
- **Modify:** `server/tests/test_whisper_proxy.py` — tests for the wiring itself (channel
  restriction, prefix appears/doesn't appear).

---

### Task 1: `adsb.py` — live aircraft cache

**Files:**
- Create: `server/stt_proxy/adsb.py`
- Test: `server/tests/test_adsb.py`

**Interfaces:**
- Consumes: nothing from other new modules.
- Produces (used by Task 2 and Task 3):
  - `class AdsbError(Exception)`
  - `parse_response(payload: bytes) -> list[dict]`
  - `map_aircraft(ac: dict) -> dict | None`
  - `build_url(lat: float, lon: float, dist_nm: float) -> str`
  - `poll_once(lat: float, lon: float, dist_nm: float, fetch=None) -> int`
  - `poll_and_record(lat: float, lon: float, dist_nm: float, fetch=None) -> None`
  - `poll_loop(lat: float, lon: float, dist_nm: float) -> None`
  - `start(lat: float, lon: float, dist_nm: float) -> None`
  - `current_aircraft() -> list[dict]` — a snapshot list of cache entries, each
    `{"flight": str, "hex": str, "r": str, "t": str, "alt_baro": int | str | None,
    "gs": float | None, "track": float | None, "lat": float | None, "lon": float | None,
    "squawk": str | None, "last_seen": float}`
  - `feed_status() -> dict` — `{"last_ok_at", "last_error_at", "last_error", "last_count",
    "consecutive_failures", "poll_sec"}`
  - `reset_feed_state() -> None` — test-only, resets the module-level feed state.
  - `POINT_LAT: float`, `POINT_LON: float`, `POINT_DIST_NM: float`, `POLL_SEC: int`

- [ ] **Step 1: Write the failing tests for `parse_response`**

```python
# server/tests/test_adsb.py
"""Tests for stt_proxy/adsb.py: the adsb.fi poll loop and live aircraft cache."""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import adsb  # noqa: E402


def test_parse_response_returns_aircraft_list():
    payload = json.dumps({"now": 123.0, "aircraft": [{"hex": "abc123"}]}).encode("utf-8")
    result = adsb.parse_response(payload)
    assert result == [{"hex": "abc123"}]


def test_parse_response_empty_aircraft_list_is_a_real_observation():
    """A genuinely empty sky is a valid answer, not a failure -- same reasoning as
    aishub.py's parse_response for a genuinely empty box."""
    payload = json.dumps({"now": 123.0, "aircraft": []}).encode("utf-8")
    assert adsb.parse_response(payload) == []


def test_parse_response_missing_aircraft_key_raises():
    payload = json.dumps({"now": 123.0}).encode("utf-8")
    with pytest.raises(adsb.AdsbError):
        adsb.parse_response(payload)


def test_parse_response_aircraft_not_a_list_raises():
    payload = json.dumps({"now": 123.0, "aircraft": "not a list"}).encode("utf-8")
    with pytest.raises(adsb.AdsbError):
        adsb.parse_response(payload)


def test_parse_response_malformed_json_raises():
    with pytest.raises(adsb.AdsbError):
        adsb.parse_response(b"not json at all")


def test_parse_response_non_dict_envelope_raises():
    payload = json.dumps([1, 2, 3]).encode("utf-8")
    with pytest.raises(adsb.AdsbError):
        adsb.parse_response(payload)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_adsb.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'stt_proxy.adsb'` (or similar —
the module doesn't exist yet).

- [ ] **Step 3: Write `parse_response`**

```python
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

import json
import threading
import time
import urllib.error
import urllib.request

API_HOST = "https://opendata.adsb.fi"


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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_adsb.py -v`
Expected: PASS (all 6 tests from Step 1)

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/adsb.py server/tests/test_adsb.py
git commit -m "Add adsb.fi response parsing for airband flight identification"
```

- [ ] **Step 6: Write the failing tests for `map_aircraft`**

```python
def test_map_aircraft_extracts_known_fields():
    ac = {
        "hex": "484443", "flight": "KLM281  ", "r": "PH-BHA", "t": "A332",
        "alt_baro": 8125, "gs": 245.3, "track": 91.2,
        "lat": 52.333237, "lon": 4.445724, "squawk": "1000",
    }
    result = adsb.map_aircraft(ac)
    assert result["hex"] == "484443"
    assert result["flight"] == "KLM281"          # trimmed
    assert result["r"] == "PH-BHA"
    assert result["t"] == "A332"
    assert result["alt_baro"] == 8125
    assert result["gs"] == 245.3
    assert result["track"] == 91.2
    assert result["lat"] == 52.333237
    assert result["lon"] == 4.445724
    assert result["squawk"] == "1000"


def test_map_aircraft_handles_ground_altitude():
    """adsb.fi reports alt_baro as the literal string "ground" for aircraft on the runway,
    not a number -- observed live on EZY85FV during the 2026-09-08 feasibility check."""
    ac = {"hex": "406012", "flight": "EZY85FV ", "alt_baro": "ground"}
    result = adsb.map_aircraft(ac)
    assert result["alt_baro"] == "ground"


def test_map_aircraft_returns_none_without_hex():
    """hex is the cache key -- an aircraft record without one cannot be cached, the same
    reasoning aishub.py's map_ship uses for a ship record without an MMSI."""
    assert adsb.map_aircraft({"flight": "KLM281"}) is None


def test_map_aircraft_defaults_missing_optional_fields_to_none():
    result = adsb.map_aircraft({"hex": "abc123"})
    assert result["flight"] == ""
    assert result["r"] is None
    assert result["t"] is None
    assert result["lat"] is None
    assert result["lon"] is None
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_adsb.py -v`
Expected: FAIL with `AttributeError: module 'stt_proxy.adsb' has no attribute 'map_aircraft'`

- [ ] **Step 8: Write `map_aircraft`**

Add to `server/stt_proxy/adsb.py`:

```python
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
    }
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_adsb.py -v`
Expected: PASS (10 tests total so far)

- [ ] **Step 10: Commit**

```bash
git add server/stt_proxy/adsb.py server/tests/test_adsb.py
git commit -m "Map adsb.fi aircraft records to the fields this module caches"
```

- [ ] **Step 11: Write the failing tests for `build_url`, `poll_once`, and the cache**

```python
def test_build_url_shape():
    url = adsb.build_url(52.15, 4.3, 40)
    assert url == "https://opendata.adsb.fi/api/v2/lat/52.15/lon/4.3/dist/40"


@pytest.fixture(autouse=True)
def _clear_cache():
    adsb._aircraft_cache.clear()
    adsb.reset_feed_state()
    yield
    adsb._aircraft_cache.clear()
    adsb.reset_feed_state()


def _fake_fetch(payload: bytes):
    return lambda url: payload


def test_poll_once_populates_the_cache():
    payload = json.dumps({"now": 1.0, "aircraft": [
        {"hex": "484443", "flight": "KLM281"},
        {"hex": "4caa5a", "flight": "RYR37DV"},
    ]}).encode("utf-8")

    count = adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(payload))

    assert count == 2
    cached = {ac["hex"]: ac["flight"] for ac in adsb.current_aircraft()}
    assert cached == {"484443": "KLM281", "4caa5a": "RYR37DV"}


def test_poll_once_replaces_rather_than_accumulates():
    """A full-replace snapshot, not an accumulating merge -- see the Global Constraints
    note in the plan for why aircraft don't get AIS-style staleness tracking."""
    first  = json.dumps({"now": 1.0, "aircraft": [{"hex": "111111", "flight": "AAA1"}]}).encode()
    second = json.dumps({"now": 2.0, "aircraft": [{"hex": "222222", "flight": "BBB2"}]}).encode()

    adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(first))
    adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(second))

    hexes = {ac["hex"] for ac in adsb.current_aircraft()}
    assert hexes == {"222222"}


def test_poll_once_skips_aircraft_without_hex_but_keeps_the_rest():
    payload = json.dumps({"now": 1.0, "aircraft": [
        {"flight": "NOHEX"}, {"hex": "abc123", "flight": "OK1"},
    ]}).encode("utf-8")

    count = adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(payload))

    assert count == 1
    assert [ac["hex"] for ac in adsb.current_aircraft()] == ["abc123"]


def test_poll_once_raises_and_leaves_cache_untouched_on_bad_response(monkeypatch):
    payload = json.dumps({"now": 1.0, "aircraft": [{"hex": "abc123", "flight": "OK1"}]}).encode()
    adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(payload))

    def _broken_fetch(url):
        return b"not json"

    with pytest.raises(adsb.AdsbError):
        adsb.poll_once(52.15, 4.3, 40, fetch=_broken_fetch)

    # Cache from the earlier good poll is untouched.
    assert [ac["hex"] for ac in adsb.current_aircraft()] == ["abc123"]
```

- [ ] **Step 12: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_adsb.py -v`
Expected: FAIL with `AttributeError` on `adsb.build_url` / `adsb.poll_once` /
`adsb._aircraft_cache` / `adsb.current_aircraft`

- [ ] **Step 13: Write `build_url`, the cache, and `poll_once`**

Add to `server/stt_proxy/adsb.py`:

```python
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


def poll_once(lat: float, lon: float, dist_nm: float, fetch=None) -> int:
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
        fields["last_seen"] = time.time()
        mapped[fields["hex"]] = fields

    with _cache_lock:
        _aircraft_cache.clear()
        _aircraft_cache.update(mapped)

    return len(mapped)
```

- [ ] **Step 14: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_adsb.py -v`
Expected: PASS (16 tests total so far)

- [ ] **Step 15: Commit**

```bash
git add server/stt_proxy/adsb.py server/tests/test_adsb.py
git commit -m "Add adsb.fi poll_once with a full-replace aircraft cache"
```

- [ ] **Step 16: Write the failing tests for feed-status tracking and the poll loop wrapper**

```python
def test_poll_and_record_success_updates_feed_status():
    payload = json.dumps({"now": 1.0, "aircraft": [{"hex": "abc123", "flight": "OK1"}]}).encode()

    adsb.poll_and_record(52.15, 4.3, 40, fetch=_fake_fetch(payload))

    status = adsb.feed_status()
    assert status["last_ok_at"] is not None
    assert status["last_count"] == 1
    assert status["consecutive_failures"] == 0


def test_poll_and_record_never_raises_on_failure():
    def _broken_fetch(url):
        raise OSError("network is down")

    adsb.poll_and_record(52.15, 4.3, 40, fetch=_broken_fetch)  # must not raise

    status = adsb.feed_status()
    assert status["last_error"] is not None
    assert status["consecutive_failures"] == 1


def test_poll_and_record_never_raises_on_unexpected_exception():
    """poll_and_record is a daemon thread's entry point with nothing above it -- an escaped
    exception silently ends polling forever, the exact failure aishub.py's docstring
    describes for the eight-day aisstream outage. This must never happen here either."""
    def _exploding_fetch(url):
        raise ValueError("something else went wrong")

    adsb.poll_and_record(52.15, 4.3, 40, fetch=_exploding_fetch)  # must not raise

    assert adsb.feed_status()["consecutive_failures"] == 1
```

- [ ] **Step 17: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_adsb.py -v`
Expected: FAIL with `AttributeError: module 'stt_proxy.adsb' has no attribute
'poll_and_record'`

- [ ] **Step 18: Write feed-status tracking, `poll_and_record`, `poll_loop`, `start`**

Add to `server/stt_proxy/adsb.py`:

```python
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
    global _last_ok_at, _last_count, _consecutive_failures
    with _feed_lock:
        _last_ok_at = time.time()
        _last_count = count
        _consecutive_failures = 0
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
```

Add near the top of the file, after `API_HOST`:

```python
import os

# Centred to cover Schiphol, Rotterdam, and the Scheveningen coastal corridor -- the exact
# point tested live during design (2026-09-08), which returned 59 real aircraft including
# KLM281, RYR37DV, AFR16JN, EZY85FV and PHVSY (a Dutch-registered light aircraft).
POINT_LAT     = float(os.environ.get("ADSB_LAT", "52.15"))
POINT_LON     = float(os.environ.get("ADSB_LON", "4.3"))
POINT_DIST_NM = float(os.environ.get("ADSB_DIST_NM", "40"))

# No published rate limit was found for adsb.fi during design (unlike AISHub's documented
# "once per minute"), so this is a conservative starting point, not an enforced server fact.
# Aircraft move at 150-250 m/s on approach -- far faster than ships -- so it needs to be much
# more frequent than AISHub's 900s.
POLL_SEC = int(os.environ.get("ADSB_POLL_SEC", "15"))
```

- [ ] **Step 19: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_adsb.py -v`
Expected: PASS (19 tests total)

- [ ] **Step 20: Commit**

```bash
git add server/stt_proxy/adsb.py server/tests/test_adsb.py
git commit -m "Add adsb.fi feed-status tracking and the daemon poll loop"
```

---

### Task 2: `flight_identify.py` — callsign extraction and matching

**Files:**
- Create: `server/stt_proxy/flight_identify.py`
- Test: `server/tests/test_flight_identify.py`

**Interfaces:**
- Consumes:
  - `stt_proxy.adsb.current_aircraft() -> list[dict]` (Task 1)
  - `stt_proxy.corrections._decode_spoken_word(word: str) -> str | None` (existing — decodes
    both spoken digits and NATO phonetic letters, needed because real callsign suffixes mix
    both, e.g. "six november" -> "6N")
- Produces (used by Task 3):
  - `extract_callsign_candidate(text: str) -> str | None`
  - `match_flight(candidate: str) -> dict | None`
  - `format_flight_for_plugin(result: dict, text: str) -> str`
  - `identify_flight(text: str) -> str` — the one-call convenience wrapper Task 3 uses:
    extracts, matches, formats if matched, else returns `text` unchanged.

- [ ] **Step 1: Write the failing tests for the airline table and `extract_callsign_candidate`**

```python
# server/tests/test_flight_identify.py
"""Tests for stt_proxy/flight_identify.py: callsign extraction and matching against the
live adsb.fi cache."""

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import adsb, flight_identify  # noqa: E402


def test_extract_clean_airline_name_and_digits():
    assert flight_identify.extract_callsign_candidate(
        "Transavia, six november, deviating heading zero two zero."
    ) == "TRA6N"


def test_extract_klm_said_plainly():
    assert flight_identify.extract_callsign_candidate(
        "Hello KLM, Greenland runway two thousand one one, ILS, cleared ILS."
    ) == "KLM"


def test_extract_known_garbled_klm_variants():
    """Real garbled forms observed in the 2026-09-08 session log -- the same policy
    corrections.py already uses: variants are added only where a real transmission
    produced them."""
    assert flight_identify.extract_callsign_candidate("KALM six seven six") == "KLM676"
    assert flight_identify.extract_callsign_candidate("RKLM seven four four") == "KLM744"


def test_extract_american_two_two_one():
    assert flight_identify.extract_callsign_candidate(
        "American two two one on bird two Sierra, passing two thousand four hundred."
    ) == "AAL221"


def test_extract_bare_digits_without_airline_anchor_returns_none():
    """A heading, QNH, or flight level is just digits -- without an airline word, treating
    it as a callsign would be a guess wearing evidence's clothes."""
    assert flight_identify.extract_callsign_candidate("One eight zero, over.") is None


def test_extract_airline_word_with_no_following_digits_returns_none():
    assert flight_identify.extract_callsign_candidate("Delta, say again please.") is None


def test_extract_returns_none_for_empty_text():
    assert flight_identify.extract_callsign_candidate("") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_flight_identify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'stt_proxy.flight_identify'`

- [ ] **Step 3: Write the airline table and `extract_callsign_candidate`**

```python
"""Airband flight identification: match a heard callsign against the live adsb.fi cache.

Mirrors identify.py's role for vessels, deliberately smaller -- regex/dictionary extraction
and rapidfuzz matching, no Claude call, no conversation-level resolution. See
docs/superpowers/specs/2026-09-08-airband-flight-identification-design.md.
"""

import re

from rapidfuzz import fuzz as rf_fuzz

from stt_proxy import adsb
from stt_proxy.corrections import _decode_spoken_word

# Spoken/heard word -> ICAO 3-letter airline code. Seeded from carriers actually observed
# during the 2026-09-08 feasibility check (both the live log and the live adsb.fi traffic).
# Grow this table from real transmissions, the same policy corrections.py's phonetic table
# already uses.
#
# KLM's known garbled forms (kalm, rklm, klmx) are listed explicitly rather than relying on
# fuzzy matching -- "KLM" is 3 characters, in the same danger zone corrections.py's own
# comments document for its phonetic-letter table: at any fuzzy threshold loose enough to
# catch real garbling of a 3-character token, unrelated short words start scoring just as
# high. Longer airline names (5+ characters) are safe for fuzzy matching; see
# _find_airline_anchor below.
AIRLINE_TELEPHONY: dict[str, str] = {
    "klm": "KLM", "kalm": "KLM", "rklm": "KLM", "klmx": "KLM",
    "transavia": "TRA",
    "american": "AAL",
    "united": "UAL",
    "delta": "DAL",
    "envoy": "ENY",
    "qatar": "QTR", "qatari": "QTR",
    "ryanair": "RYR",
    "speedbird": "BAW",
    "lufthansa": "DLH",
    "easyjet": "EZY",
}

# Only words this long or longer are tried against the table with fuzzy matching -- see the
# module docstring's note on why short tokens (KLM's 3 characters) are handled by explicit
# variants instead.
_FUZZY_MIN_WORD_LEN = 5
_FUZZY_THRESHOLD = 70  # a starting point, not yet measured against a labelled corpus


def _find_airline_anchor(word: str) -> str | None:
    """The ICAO code if `word` names a known airline (exactly, or a garbled long name), else None."""
    exact = AIRLINE_TELEPHONY.get(word)
    if exact:
        return exact
    if len(word) < _FUZZY_MIN_WORD_LEN:
        return None
    best_code, best_score = None, 0
    for spoken, code in AIRLINE_TELEPHONY.items():
        if len(spoken) < _FUZZY_MIN_WORD_LEN:
            continue
        score = rf_fuzz.ratio(word, spoken)
        if score > best_score:
            best_code, best_score = code, score
    return best_code if best_score >= _FUZZY_THRESHOLD else None


def extract_callsign_candidate(text: str) -> str | None:
    """A candidate flight designator (e.g. "TRA6N") for match_flight, or None.

    Deliberately permissive on the airline word -- an airline name can be misheard the same
    way a vessel name can. Deliberately strict on requiring digits to follow it: bare digits
    with no airline anchor are ambiguous (a heading, a QNH, a flight level) and must not be
    treated as a callsign.
    """
    words = re.findall(r"[A-Za-z]+", (text or "").lower())
    for i, word in enumerate(words):
        code = _find_airline_anchor(word)
        if code is None:
            continue
        digits = ""
        for follow in words[i + 1:i + 5]:
            # Not just spoken digits: a real callsign suffix mixes digits and a single
            # phonetic letter ("six november" -> "6N"), which _decode_spoken_word already
            # handles by checking both tables -- using _SPOKEN_DIGITS alone here was tried
            # and empirically failed ("Transavia six november" produced "TRA6", dropping the
            # N) before this plan was finalised.
            char = _decode_spoken_word(follow)
            if char is None:
                break
            digits += char
        if digits:
            return f"{code}{digits}"
        return code if code == word.upper() else None
    return None
```

Note the return-value nuance in the last branch: if the airline word itself decoded to a code
with no digits following (`"Delta, say again"`), that must be `None` — but `"Hello KLM"` (the
clean `test_extract_klm_said_plainly` case) must return `"KLM"` even with no digits, because
`match_flight` still has something to fuzzy-match against a bare "KLM..." flight number. The
distinguishing test is whether the matched word already looks like the bare code (`klm`) versus
a spoken company name (`delta`) that resolved to a *different* string (`DAL`) — a spoken name
alone, with nothing that could itself be a partial callsign, carries no more identifying power
than the airline general frequency traffic already has. (This whole implementation — every
function in this task — was run against every test case in this task by hand before the plan
was finalised, using a scratch copy of the real `corrections.py` helpers; the two bugs above
are exactly what that caught.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_flight_identify.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/flight_identify.py server/tests/test_flight_identify.py
git commit -m "Add callsign candidate extraction for airband flight identification"
```

- [ ] **Step 6: Write the failing tests for `match_flight`**

```python
@pytest.fixture(autouse=True)
def _clear_cache():
    adsb._aircraft_cache.clear()
    yield
    adsb._aircraft_cache.clear()


def _seed(hex_id: str, flight: str, t: str = "B738"):
    adsb._aircraft_cache[hex_id] = {
        "hex": hex_id, "flight": flight, "r": None, "t": t,
        "alt_baro": None, "gs": None, "track": None, "lat": None, "lon": None,
        "squawk": None, "last_seen": 0.0,
    }


def test_match_flight_exact_hit():
    _seed("484443", "KLM281")
    result = flight_identify.match_flight("KLM281")
    assert result["hex"] == "484443"
    assert result["flight"] == "KLM281"


def test_match_flight_resolves_a_garbled_candidate_to_the_real_flight():
    """extract_callsign_candidate already normalised KALM -> KLM, so the candidate reaching
    match_flight is clean; this test is the airline-code fuzzy path when the candidate
    itself still differs slightly from what's in the live cache (e.g. a flight number a
    digit short)."""
    _seed("484443", "KLM676")
    assert flight_identify.match_flight("KLM676")["hex"] == "484443"


def test_match_flight_no_match_returns_none():
    _seed("484443", "KLM281")
    assert flight_identify.match_flight("AAL999") is None


def test_match_flight_handles_empty_cache():
    assert flight_identify.match_flight("KLM281") is None


def test_match_flight_none_candidate_returns_none():
    """So callers can chain extract -> match without a None-check in between."""
    assert flight_identify.match_flight(None) is None
```

- [ ] **Step 7: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_flight_identify.py -v`
Expected: FAIL with `AttributeError: module 'stt_proxy.flight_identify' has no attribute
'match_flight'`

- [ ] **Step 8: Write `match_flight`**

Add to `server/stt_proxy/flight_identify.py`:

```python
def match_flight(candidate: str | None) -> dict | None:
    """The live aircraft `candidate` most likely refers to, or None.

    Exact match first (the common case once extraction has already normalised known garbled
    airline forms). Falls back to a fuzzy match against the whole candidate string -- digits
    are not fuzzed on their own; no evidence yet that they get misheard the way letters do,
    per the design spec's deferred-work note.
    """
    if not candidate:
        return None

    for ac in adsb.current_aircraft():
        if ac["flight"] == candidate:
            return ac

    best_ac, best_score = None, 0
    for ac in adsb.current_aircraft():
        if not ac["flight"]:
            continue
        score = rf_fuzz.ratio(candidate, ac["flight"])
        if score > best_score:
            best_ac, best_score = ac, score
    return best_ac if best_score >= _FUZZY_THRESHOLD else None
```

- [ ] **Step 9: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_flight_identify.py -v`
Expected: PASS (12 tests total)

- [ ] **Step 10: Commit**

```bash
git add server/stt_proxy/flight_identify.py server/tests/test_flight_identify.py
git commit -m "Add live-cache matching for airband flight identification"
```

- [ ] **Step 11: Write the failing tests for `format_flight_for_plugin` and `identify_flight`**

```python
def test_format_flight_for_plugin_with_type():
    result = {"flight": "KLM281", "t": "A332"}
    assert (flight_identify.format_flight_for_plugin(result, "Approach, good day.")
            == "[KLM281/A332] Approach, good day.")


def test_format_flight_for_plugin_without_type():
    result = {"flight": "KLM281", "t": None}
    assert (flight_identify.format_flight_for_plugin(result, "Approach, good day.")
            == "[KLM281] Approach, good day.")


def test_identify_flight_prefixes_on_a_match():
    _seed("484443", "KLM281", t="A332")
    text = flight_identify.identify_flight("Hello KLM two eight one, cleared ILS.")
    assert text == "[KLM281/A332] Hello KLM two eight one, cleared ILS."


def test_identify_flight_returns_text_unchanged_without_a_match():
    text = flight_identify.identify_flight("One eight zero, over.")
    assert text == "One eight zero, over."


def test_identify_flight_returns_text_unchanged_when_cache_is_empty():
    text = flight_identify.identify_flight("Hello KLM two eight one, cleared ILS.")
    assert text == "Hello KLM two eight one, cleared ILS."
```

- [ ] **Step 12: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_flight_identify.py -v`
Expected: FAIL with `AttributeError` on `format_flight_for_plugin` / `identify_flight`

- [ ] **Step 13: Write `format_flight_for_plugin` and `identify_flight`**

Add to `server/stt_proxy/flight_identify.py`:

```python
def format_flight_for_plugin(result: dict, text: str) -> str:
    flight = result.get("flight") or ""
    aircraft_type = result.get("t")
    tag = f"[{flight}/{aircraft_type}]" if aircraft_type else f"[{flight}]"
    return f"{tag} {text}"


def identify_flight(text: str) -> str:
    """The one call site Task 3 needs: identify and prefix, or return text unchanged."""
    candidate = extract_callsign_candidate(text)
    result = match_flight(candidate)
    if result is None:
        return text
    return format_flight_for_plugin(result, text)
```

- [ ] **Step 14: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_flight_identify.py -v`
Expected: PASS (17 tests total)

- [ ] **Step 15: Commit**

```bash
git add server/stt_proxy/flight_identify.py server/tests/test_flight_identify.py
git commit -m "Add plugin-display formatting and the identify_flight entry point"
```

---

### Task 3: Wire into the airband branch of `whisper-proxy.py`

**Files:**
- Modify: `server/whisper-proxy.py:234-263` (the `stt_proxy.backends` import block, add a
  neighbouring import for the two new modules) and `:459-464` (the `elif mode == "airband":`
  branch)
- Modify: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `flight_identify.identify_flight(text: str) -> str` (Task 2)
- Produces: nothing further downstream — this is the final call site.

- [ ] **Step 1: Write the failing tests for channel restriction and the wiring**

Add to `server/tests/test_whisper_proxy.py`, near the existing airband-mode tests (search the
file for `mode="airband"` to find the neighbouring block):

```python
def test_approach_tower_channels_includes_both_locale_separators():
    """The plugin sends the channel as a decimal string in the SDR#'s current culture, which
    on this deployment renders a comma, not a dot (see corrections.py's existing
    channel in ("160.650", "160,650") handling for the same quirk)."""
    assert "118.405" in proxy.APPROACH_TOWER_CHANNELS
    assert "118,405" in proxy.APPROACH_TOWER_CHANNELS


def test_airband_identify_flight_called_only_on_approach_tower_channels(monkeypatch):
    calls = []
    monkeypatch.setattr(proxy.flight_identify, "identify_flight",
                         lambda text: calls.append(text) or f"[TAGGED] {text}")

    tagged = proxy._maybe_identify_flight("Hello KLM two eight one.", channel="118,405")
    untagged = proxy._maybe_identify_flight("Wind two seven zero.", channel="122,205")

    assert tagged == "[TAGGED] Hello KLM two eight one."
    assert untagged == "Wind two seven zero."
    assert calls == ["Hello KLM two eight one."]


def test_airband_identify_flight_skipped_for_unknown_channel(monkeypatch):
    monkeypatch.setattr(proxy.flight_identify, "identify_flight",
                         lambda text: pytest.fail("should not be called"))
    result = proxy._maybe_identify_flight("Hello KLM two eight one.", channel="")
    assert result == "Hello KLM two eight one."
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_whisper_proxy.py -k "approach_tower or maybe_identify" -v`
Expected: FAIL with `AttributeError: module 'whisper_proxy' has no attribute
'APPROACH_TOWER_CHANNELS'` (or `_maybe_identify_flight`)

- [ ] **Step 3: Add the import and the wiring**

In `server/whisper-proxy.py`, right after the existing `stt_proxy.backends` import block
(after the `transcribe,` line, before the blank-line block that follows it — see the imports
already reviewed at lines 234-263):

```python
from stt_proxy import adsb, flight_identify  # noqa: E402
```

Then, near the top of the file among the other module-level constants (alongside where
`AIS_CACHE_FILE` / `_STARTED_AT` are defined), add:

```python
# Only channels where pilots actually state a callsign repeatedly. Ground sometimes has one;
# ATIS/Departure Information never do (a looping recorded broadcast has no one to identify).
# Both locale-formatted separators are listed for the same reason corrections.py's channel
# check already handles this: the plugin renders the channel string in SDR#'s current
# culture, which on this deployment is a comma decimal separator.
APPROACH_TOWER_CHANNELS = frozenset({
    "121.200", "121,200",   # Schiphol Approach 4
    "118.405", "118,405",   # Schiphol Approach 5 / Arrival (Main)
    "119.055", "119,055",   # Schiphol Approach
    "127.870", "127,870",   # Schiphol Area Control Centre 1
    "119.230", "119,230",   # Schiphol Tower 1 / Tower (Main)
})


def _maybe_identify_flight(text: str, channel: str) -> str:
    """identify_flight(text), but only on a channel known to carry callsigns."""
    if channel not in APPROACH_TOWER_CHANNELS:
        return text
    return flight_identify.identify_flight(text)
```

Then, in the existing `elif mode == "airband":` branch (currently reading, per the earlier
session's work):

```python
elif mode == "airband":
    corrected = _apply_sttt_corrections(raw_text, mode="airband")
    channel_label = f"[{channel} MHz]" if channel else "[airband]"
    print(f"[{ts}] {channel_label} {corrected}", flush=True)
    data["text"] = corrected
    resp_body = json.dumps(data).encode("utf-8")
```

change it to:

```python
elif mode == "airband":
    corrected = _apply_sttt_corrections(raw_text, mode="airband")
    corrected = _maybe_identify_flight(corrected, channel)
    channel_label = f"[{channel} MHz]" if channel else "[airband]"
    print(f"[{ts}] {channel_label} {corrected}", flush=True)
    data["text"] = corrected
    resp_body = json.dumps(data).encode("utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_whisper_proxy.py -k "approach_tower or maybe_identify" -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the full server test suite**

Run: `py -m pytest server/tests -v`
Expected: PASS, no regressions (should be the prior suite total plus the ~32 new tests
from Tasks 1-3)

- [ ] **Step 6: Commit**

```bash
git add server/whisper-proxy.py server/tests/test_whisper_proxy.py
git commit -m "Wire flight identification into the airband transcription path"
```

- [ ] **Step 7: Start the ADS-B poll loop when the proxy starts**

Find where the proxy currently starts the AIS feed thread at module scope near the bottom of
`whisper-proxy.py` (search for `ais._ais_thread` or `aishub.start`). Add, alongside it:

```python
adsb.start(adsb.POINT_LAT, adsb.POINT_LON, adsb.POINT_DIST_NM)
```

- [ ] **Step 8: Manual verification against live traffic**

There is no labelled airband corpus to benchmark against yet (same limitation the AIS feed
swap had — see the spec's Testing section). Verification is by observation:

1. Restart the proxy (`py server/whisper-proxy.py`), confirm the console prints
   `[adsb.fi] polling (52.15, 4.3) within 40nm every 15s` followed by
   `[adsb.fi] N aircraft` within 15 seconds.
2. With SDR# tuned to one of `APPROACH_TOWER_CHANNELS` (e.g. Schiphol Approach 4, 121.200),
   confirm a transmission containing a real airline callsign produces a `[FLIGHT/TYPE]`
   prefix in the transcript panel.
3. Confirm a transmission on the same channel with no extractable callsign (a bare heading
   readback) displays unprefixed, exactly as before this change.
4. Confirm a transmission on a non-Approach/Tower channel (e.g. ATIS) is never sent through
   identification at all — same output as before this whole plan.

- [ ] **Step 9: Commit the manual-verification note in the plan (this file) as done**

No code change — this step just confirms Step 8 was actually performed before calling the
plan complete, per the project's verification-before-completion practice.

---

## Self-Review Notes

- **Spec coverage:** data source (adsb.fi, Task 1) ✓; extraction with fuzzy airline matching
  restricted to 5+ character words, explicit KLM variants (Task 2) ✓; channel restriction to
  Approach/Tower (Task 3) ✓; `[FLIGHT/TYPE]` prefix format matching `identify.py`'s convention
  (Task 2, `format_flight_for_plugin`) ✓; full-replace cache model, not AIS-style accumulation
  (Task 1, documented in Global Constraints and `poll_once`) ✓; graceful degradation on no
  match / feed failure (Task 1 `poll_and_record` tests, Task 2 `identify_flight` tests,
  Task 3 channel-restriction test) ✓.
- **Placeholder scan:** no TBD/TODO; every step has real code; the one place a step is more
  explanatory than code (Task 2 Step 3's note on the extraction return-value nuance) still
  ends with the actual working implementation right above it.
- **Type consistency:** `current_aircraft()` returns `list[dict]` with the same field names
  throughout Tasks 1-3 (`hex`, `flight`, `r`, `t`, `alt_baro`, `gs`, `track`, `lat`, `lon`,
  `squawk`, `last_seen`); `match_flight`'s return type (`dict | None`, same shape) is used
  consistently by `format_flight_for_plugin` and the Task 2 tests.
- **Executed, not just read.** Every function in Task 1 and Task 2, and every test case listed
  for them, was run for real against a scratch copy of the actual `corrections.py` helpers
  before this plan was finalised (not against a labelled corpus — there isn't one yet — just
  the planned code against the planned tests). This caught two real bugs that a read-through
  missed: the digit-collection loop only checked `_SPOKEN_DIGITS`, so `"six november"` produced
  `"TRA6"` instead of `"TRA6N"` (fixed by using `_decode_spoken_word`, which checks phonetic
  letters too); and the bare-airline-word return condition was inverted (`code != word.upper()`
  instead of `==`), which would have made the clean `"Hello KLM"` case return `None`. The fuzzy
  threshold (70) was also checked against a list of common ATC words (`heading`, `runway`,
  `approach`, `descend`, etc.) against every 5+ character airline entry and produced zero
  false-positive collisions — reassuring, but still an untested-against-a-real-corpus starting
  point, as the Global Constraints section already says.
