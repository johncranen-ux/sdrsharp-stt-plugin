# Local AIS Receiver Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate the vessel cache from a locally-received AIS feed, replacing an aisstream feed that has delivered nothing since 2026-08-05.

**Architecture:** AIS-catcher runs as an external process on a second RTL-SDR dongle and pushes fully-decoded JSON over UDP. A new listener parses it and calls a provider-neutral recorder extracted from `ais.py`, so aisstream and the local feed share one merge implementation. Raw AIS position reports carry no vessel name, so the recorder gains an MMSI index and a pending-position area.

**Tech Stack:** Python 3.10+, stdlib `socket`/`json`/`threading`. No new dependencies. AIS-catcher v0.66 (external, already installed at `D:\SDR\AIS\AIS-catcher.exe`).

**Design spec:** `docs/superpowers/specs/2026-08-09-local-ais-receiver-design.md`

## Global Constraints

- **All tests run offline.** No network, no dongle, no AIS-catcher process in the suite. UDP tests bind loopback on an ephemeral port.
- **Run the suite with `py -m pytest server/tests -q` from the repo root.** 554 tests currently pass plus 1 deliberate xfail; that must not regress.
- **The existing `ais_caches` fixture** (`server/tests/test_whisper_proxy.py:132`) monkeypatches `ais._vessel_cache`, `ais._callsign_cache`, `ais._unknown_frames_logged` and `ais._last_message_at`. Any new module-global cache state must be added to it, or tests leak state between runs.
- **Module-owned state rule:** modules own mutable state written by background threads. Read it through the module (`ais._vessel_cache`), never via an imported name, and patch the *owner* in tests. Breaking this gives wrong results, not errors.
- **Never commit received traffic.** `.gitignore` and the CI gate in `.github/workflows/ci.yml` both list transcript-bearing filenames. Captured AIS JSON samples used as test fixtures are synthetic or from the standard test sentences in this plan — never a live capture of real vessels.
- **Comment style:** explain *why*, and cite the measurement or bug that motivated the code.
- `_MAAS_CENTER = (52.02, 3.88)` lives in `server/bench_identify.py:155`. The recorder needs its own copy in `ais.py`; do not import bench_identify from the proxy.

---

## File Structure

| File | Responsibility |
|---|---|
| `server/stt_proxy/ais.py` (modify) | Gains the provider-neutral recorder, MMSI index, pending positions, radius filter. `_process_ais` becomes a thin aisstream adapter over it. |
| `server/stt_proxy/ais_local.py` (create) | AIS-catcher JSON → recorder. Two halves: a pure `parse_message()` and a UDP listener thread. No cache state of its own. |
| `server/tests/test_ais_local.py` (create) | Adapter and listener tests. |
| `server/tests/test_whisper_proxy.py` (modify) | Recorder tests alongside the existing AIS tests; extend the `ais_caches` fixture. |
| `server/start-all.bat` (modify) | Launch AIS-catcher. **Gitignored** — the implementer edits it locally and reports the exact lines added; it is never committed. |

---

### Task 1: The provider-neutral recorder

**Files:**
- Modify: `server/stt_proxy/ais.py` (add near `_process_ais`, line 388)
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `ais._vessel_cache`, `ais._callsign_cache`, `ais._cache_lock`, `ais._now()`, `ais._clean_destination()`
- Produces:
  - `ais.record(fields: dict, *, source: str) -> None`
  - `ais._mmsi_index: dict[str, dict]`
  - `ais._pending_positions: dict[str, dict]`

`fields` is a partial observation. Recognised keys: `mmsi` (str, required), `name`, `callsign`, `type`, `imo`, `length`, `beam`, `draught`, `destination`, `latitude`, `longitude`, `sog`, `cog`, `heading`. Any subset may be present.

- [ ] **Step 1: Extend the `ais_caches` fixture**

In `server/tests/test_whisper_proxy.py`, replace the fixture body at line 133:

```python
@pytest.fixture
def ais_caches(monkeypatch):
    vessels, callsigns = {}, {}
    monkeypatch.setattr(ais, "_vessel_cache", vessels)
    monkeypatch.setattr(ais, "_callsign_cache", callsigns)
    # New in the local-AIS work: the MMSI index and the pending-position area are module
    # globals too, so without resetting them a test's result depends on what ran before it.
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending_positions", {})
    monkeypatch.setattr(ais, "_unknown_frames_logged", 0)
    monkeypatch.setattr(ais, "_last_message_at", None)
    return vessels, callsigns
```

- [ ] **Step 2: Write the failing tests**

Append to `server/tests/test_whisper_proxy.py`, after the existing AIS parsing tests:

```python
# ---------------------------------------------------------------------------
# The provider-neutral recorder
#
# aisstream enriched every position report with MetaData.ShipName. Raw AIS does not:
# types 1/2/3 carry MMSI and position only, and the name arrives separately in type 5
# roughly every 6 minutes. The cache is keyed by name, so a nameless position report has
# nowhere to go until the vessel is named -- hence the MMSI index and the pending area.
# ---------------------------------------------------------------------------

def test_a_named_static_observation_creates_a_vessel(ais_caches):
    vessels, callsigns = ais_caches
    ais.record({"mmsi": "244010000", "name": "VARNEBANK", "callsign": "PBUX"},
               source="local")
    assert vessels["VARNEBANK"]["mmsi"] == "244010000"
    assert callsigns["PBUX"] is vessels["VARNEBANK"]
    assert ais._mmsi_index["244010000"] is vessels["VARNEBANK"]


def test_a_nameless_position_is_held_until_the_vessel_is_named(ais_caches):
    """THE case local AIS creates. Dropping it, as the aisstream path does, would discard
    most local position data -- the name only arrives every ~6 minutes."""
    vessels, _ = ais_caches
    ais.record({"mmsi": "244010000", "latitude": 52.0, "longitude": 3.9}, source="local")
    assert vessels == {}
    assert "244010000" in ais._pending_positions

    ais.record({"mmsi": "244010000", "name": "VARNEBANK"}, source="local")
    assert vessels["VARNEBANK"]["latitude"] == 52.0
    assert ais._pending_positions == {}


def test_a_position_for_a_known_mmsi_lands_without_a_name(ais_caches):
    vessels, _ = ais_caches
    ais.record({"mmsi": "244010000", "name": "VARNEBANK"}, source="local")
    ais.record({"mmsi": "244010000", "latitude": 52.0, "longitude": 3.9}, source="local")
    assert vessels["VARNEBANK"]["latitude"] == 52.0


def test_static_never_erases_a_known_position(ais_caches):
    """The 2026-08-06 bug, re-asserted through the new entry point: static messages carry
    no position, and assigning wholesale deleted what PositionReport had recorded. It
    repeated every ~6 minutes, leaving 25% of labelled vessels with no position at all."""
    vessels, _ = ais_caches
    ais.record({"mmsi": "1", "name": "SHIP", "latitude": 52.0, "longitude": 3.9},
               source="local")
    ais.record({"mmsi": "1", "name": "SHIP", "callsign": "PBAA"}, source="local")
    assert vessels["SHIP"]["latitude"] == 52.0
    assert vessels["SHIP"]["callsign"] == "PBAA"


def test_a_stale_position_does_not_overwrite_a_fresher_one(ais_caches):
    """Why the rule is newest-wins rather than a blanket 'local wins': a vessel heard
    locally two hours ago and now out of range must not keep its stale fix over a fresh
    remote one."""
    vessels, _ = ais_caches
    ais.record({"mmsi": "1", "name": "SHIP", "latitude": 52.0, "longitude": 3.9},
               source="local")
    fresh = vessels["SHIP"]["position_at"]
    ais.record({"mmsi": "1", "name": "SHIP", "latitude": 10.0, "longitude": 10.0},
               source="aisstream", observed_at=fresh - 3600)
    assert vessels["SHIP"]["latitude"] == 52.0


def test_the_newest_position_wins_whatever_the_source(ais_caches):
    vessels, _ = ais_caches
    ais.record({"mmsi": "1", "name": "SHIP", "latitude": 52.0, "longitude": 3.9},
               source="aisstream")
    ais.record({"mmsi": "1", "name": "SHIP", "latitude": 51.5, "longitude": 4.0},
               source="local")
    assert vessels["SHIP"]["latitude"] == 51.5
    assert vessels["SHIP"]["source"] == "local"


def test_an_observation_with_no_mmsi_is_ignored(ais_caches):
    vessels, _ = ais_caches
    ais.record({"name": "SHIP"}, source="local")
    assert vessels == {}
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_whisper_proxy.py -q -k "recorder or nameless or stale or newest or no_mmsi or named_static or known_mmsi or never_erases"`
Expected: FAIL with `AttributeError: module 'stt_proxy.ais' has no attribute 'record'`

- [ ] **Step 4: Implement the recorder**

Add to `server/stt_proxy/ais.py`, immediately before `_process_ais` (line 388):

```python
# MMSI -> the SAME entry object held in _vessel_cache. Raw AIS position reports (types
# 1/2/3) carry no vessel name, where aisstream enriched every one with MetaData.ShipName,
# so without this index a local position report has no way to find its vessel. It also
# retires match_by_mmsi's linear scan over ~8,600 entries.
_mmsi_index: dict[str, dict] = {}

# Positions for MMSIs not yet named, flushed into the entry when a static message names
# the vessel. Deliberately NOT stored in _vessel_cache under a synthetic "MMSI:244..."
# key: the fuzzy name matcher iterates those keys, and junk keys would become candidates
# for name matching.
_pending_positions: dict[str, dict] = {}

_STATIC_FIELDS   = ("name", "callsign", "type", "imo", "length", "beam",
                    "draught", "destination")
_POSITION_FIELDS = ("latitude", "longitude", "sog", "cog", "heading")


def record(fields: dict, *, source: str, observed_at: float | None = None) -> None:
    """Merge one observation into the vessel cache, whatever provider saw it.

    One implementation on purpose. The merge is where the subtle bugs lived: static
    messages wholesale-replacing position data left 25% of the vessels in the labelled
    conversations with no position at all until the MERGE-never-replace fix. Two providers
    writing the cache through two code paths would be two chances to get that wrong, with
    only one of them covered by these tests.

    `observed_at` is a UNIX timestamp for the observation; it defaults to now. Position
    writes apply only if newer than the stored fix -- see the stale-position test.
    """
    mmsi = str(fields.get("mmsi") or "").strip()
    if not mmsi:
        return
    when = time.time() if observed_at is None else observed_at
    name = (fields.get("name") or "").strip()

    with _cache_lock:
        entry = _mmsi_index.get(mmsi)
        if entry is None and name:
            entry = _vessel_cache.get(name.upper())

        if entry is None:
            if not name:
                # Nameless and unknown: hold the position until a static message names it.
                pending = _pending_positions.setdefault(mmsi, {"mmsi": mmsi})
                _merge_position(pending, fields, when, source)
                return
            entry = {"name": name, "callsign": "", "mmsi": mmsi, "type": None,
                     "imo": None, "length": None, "beam": None, "last_seen": _now()}
            _vessel_cache[name.upper()] = entry
            _mmsi_index[mmsi] = entry
            held = _pending_positions.pop(mmsi, None)
            if held:
                _merge_position(entry, held, held.get("position_at", when),
                                held.get("source", source))

        for key in _STATIC_FIELDS:
            if key in fields and fields[key] is not None:
                entry[key] = fields[key]
        _merge_position(entry, fields, when, source)

        entry["mmsi"] = mmsi
        entry["last_seen"] = _now()
        entry["source"] = source
        _mmsi_index[mmsi] = entry
        if entry.get("callsign"):
            _callsign_cache[entry["callsign"].upper()] = entry


def _merge_position(entry: dict, fields: dict, when: float, source: str) -> None:
    """Apply position fields only if this observation is newer than the stored fix.

    Newest-wins rather than a blanket 'local always overwrites': a vessel heard locally
    two hours ago and now out of VHF range must not keep a stale fix over a fresh remote
    one. In practice local AIS is real-time and wins essentially always, so this delivers
    the intent without its pathological case.
    """
    if fields.get("latitude") is None or fields.get("longitude") is None:
        return
    if when < entry.get("position_at", float("-inf")):
        return
    for key in _POSITION_FIELDS:
        if key in fields:
            entry[key] = fields[key]
    entry["position_at"] = when
    entry["source"] = source
```

Add `import time` to the imports at the top of `ais.py` if it is not already there (it is used by `_last_message_at`, so it should be).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_whisper_proxy.py -q`
Expected: PASS, all existing tests still green.

- [ ] **Step 6: Commit**

```bash
git add server/stt_proxy/ais.py server/tests/test_whisper_proxy.py
git commit -m "Record vessel observations through one provider-neutral path"
```

---

### Task 2: The radius filter

**Files:**
- Modify: `server/stt_proxy/ais.py`
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `ais.record` from Task 1
- Produces: `ais.AIS_LOCAL_MAX_KM` (float), `ais._km_from_maas(lat, lon) -> float`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_vessel_outside_the_radius_never_enters_the_cache(ais_caches, monkeypatch):
    """The candidate pool is where wrong-match risk lives: the documented NORDIC SIRA /
    NORDIC SAGA failure came from too many candidates. Measured over the 7,205 cached
    vessels carrying a position, a 40 km radius admits 1,116 of them -- an 85% cut."""
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_LOCAL_MAX_KM", 40.0)
    # Off Norway, far outside any plausible Maas approach.
    ais.record({"mmsi": "1", "name": "FARAWAY", "latitude": 60.0, "longitude": 5.0},
               source="local")
    assert vessels == {}


def test_a_vessel_inside_the_radius_is_admitted(ais_caches, monkeypatch):
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_LOCAL_MAX_KM", 40.0)
    ais.record({"mmsi": "1", "name": "NEARBY", "latitude": 52.02, "longitude": 3.88},
               source="local")
    assert "NEARBY" in vessels


def test_scheveningen_is_inside_forty_km_and_the_filter_does_not_pretend_otherwise(
        ais_caches, monkeypatch):
    """Recorded because an earlier draft of the spec claimed a 40 km radius would exclude
    Scheveningen. It does not: measured against _MAAS_CENTER it is 27.7 km away, so no
    radius separates it from inbound traffic. The filter buys pool reduction, not port
    exclusion, and this test stops that claim being reintroduced."""
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_LOCAL_MAX_KM", 40.0)
    ais.record({"mmsi": "1", "name": "SCH123 ZEELAND",
                "latitude": 52.0992, "longitude": 4.2659}, source="local")
    assert "SCH123 ZEELAND" in vessels
    assert ais._km_from_maas(52.0992, 4.2659) == pytest.approx(27.8, abs=1.0)


def test_a_static_only_vessel_waits_rather_than_being_admitted(ais_caches, monkeypatch):
    """Static messages carry no position, so the radius cannot judge them yet. Admitting
    them regardless would let the whole world in through type 5 alone."""
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_LOCAL_MAX_KM", 40.0)
    ais.record({"mmsi": "1", "name": "UNPLACED", "callsign": "PBAA"}, source="local")
    assert vessels == {}
    ais.record({"mmsi": "1", "latitude": 52.02, "longitude": 3.88}, source="local")
    assert vessels["UNPLACED"]["callsign"] == "PBAA"


def test_the_filter_is_off_when_the_radius_is_zero(ais_caches, monkeypatch):
    vessels, _ = ais_caches
    monkeypatch.setattr(ais, "AIS_LOCAL_MAX_KM", 0.0)
    ais.record({"mmsi": "1", "name": "FARAWAY", "latitude": 60.0, "longitude": 5.0},
               source="local")
    assert "FARAWAY" in vessels
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_whisper_proxy.py -q -k "radius or scheveningen or static_only or filter_is_off"`
Expected: FAIL — vessels are admitted regardless of position.

- [ ] **Step 3: Implement the filter**

Add near the other config constants in `ais.py` (beside `AIS_MAX_AGE_MIN`, line 245):

```python
# Radius in km from Maas Center for admission to the cache; 0 disables the filter.
#
# The purpose is pool reduction, not excluding any particular port. Measured over the
# 7,205 cached vessels that carry a position: 20 km admits 349, 30 km admits 654, 40 km
# admits 1,116 (15.5%), 100 km admits 5,878. Cutting the pool by 85% cuts the wrong-match
# surface, which is where the documented NORDIC SIRA / NORDIC SAGA failure came from.
#
# 40 is a starting point, not a finding. Too tight loses recall, too wide loses precision.
# Tune it against `bench_identify.py --labels ... --resolve --repeats 3`, which reports
# both with a spread. And note Scheveningen sits at 27.7 km, so NO radius separates it
# from inbound traffic -- do not expect this to do that.
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
```

Add `import math` to the imports at the top of `ais.py`.

Then in `record()`, immediately after `when = ...` and before `with _cache_lock:`:

```python
    lat, lon = fields.get("latitude"), fields.get("longitude")
    if AIS_LOCAL_MAX_KM > 0 and lat is not None and lon is not None:
        if _km_from_maas(lat, lon) > AIS_LOCAL_MAX_KM:
            return
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/ais.py server/tests/test_whisper_proxy.py
git commit -m "Admit vessels to the cache only within a radius of Maas Center"
```

---

### Task 3: Move aisstream onto the recorder

**Files:**
- Modify: `server/stt_proxy/ais.py:388-460` (`_process_ais`)
- Test: `server/tests/test_whisper_proxy.py` (existing tests, unchanged)

**Interfaces:**
- Consumes: `ais.record` from Task 1
- Produces: no new public names. `_process_ais(msg: dict) -> None` keeps its signature.

The existing tests at `test_whisper_proxy.py:144-283` already cover the aisstream merge behaviours. **They are the proof of no behaviour change and must not be edited in this task.**

- [ ] **Step 1: Rewrite `_process_ais` as a thin adapter**

Replace the body of `_process_ais` (keeping the function name and signature) with:

```python
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
```

- [ ] **Step 2: Run the existing tests unchanged**

Run: `py -m pytest server/tests/test_whisper_proxy.py -q`
Expected: PASS. **If any of `test_ship_static_data_is_parsed`, `test_position_report_is_parsed`, `test_static_data_does_not_erase_a_known_position` or `test_both_message_types_stamp_last_seen` fail, the refactor changed behaviour — fix the recorder, not the test.**

Note the radius filter from Task 2 now applies to aisstream too. Existing tests use positions inside the radius or none at all; if one fails on the filter, monkeypatch `AIS_LOCAL_MAX_KM` to `0` **in that test only** and note why.

- [ ] **Step 3: Run the full suite**

Run: `py -m pytest server/tests -q`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add server/stt_proxy/ais.py
git commit -m "Route aisstream through the shared recorder"
```

---

### Task 4: The AIS-catcher JSON adapter

**Files:**
- Create: `server/stt_proxy/ais_local.py`
- Test: `server/tests/test_ais_local.py`

**Interfaces:**
- Consumes: nothing from earlier tasks (pure function)
- Produces: `ais_local.parse_message(msg: dict) -> dict | None` — returns recorder `fields`, or `None` if the message should be ignored.

Real AIS-catcher v0.66 `-o 5` output, captured 2026-08-09 by feeding standard test sentences over UDP:

```
type 1: {"class":"AIS","type":1,"mmsi":366053209,"status":3,"speed":0.0,
         "lon":-122.341614,"lat":37.802120,"course":219.3,"heading":1,...}
type 5: {"class":"AIS","type":5,"mmsi":369190000,"imo":6710932,
         "callsign":"WDA9674","shipname":"MT.MITCHELL","shiptype":99,
         "to_bow":90,"to_stern":90,"to_port":10,"to_starboard":10,
         "draught":6.0,"destination":"SEATTLE",...}
```

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_ais_local.py`:

```python
"""Tests for ais_local.py -- AIS-catcher JSON into the shared recorder.

Field names below are from real AIS-catcher v0.66 `-o 5` output, captured 2026-08-09 by
feeding the standard AIVDM test sentences over UDP. They are not guessed.
"""

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import ais_local  # noqa: E402

POSITION = {"class": "AIS", "type": 1, "mmsi": 366053209, "status": 3, "speed": 0.0,
            "lon": -122.341614, "lat": 37.802120, "course": 219.3, "heading": 1,
            "channel": "B"}

STATIC = {"class": "AIS", "type": 5, "mmsi": 369190000, "imo": 6710932,
          "callsign": "WDA9674", "shipname": "MT.MITCHELL", "shiptype": 99,
          "to_bow": 90, "to_stern": 90, "to_port": 10, "to_starboard": 10,
          "draught": 6.0, "destination": "SEATTLE", "channel": "A"}


def test_a_position_report_maps_to_recorder_fields():
    f = ais_local.parse_message(POSITION)
    assert f["mmsi"] == "366053209"
    assert f["latitude"] == pytest.approx(37.80212)
    assert f["longitude"] == pytest.approx(-122.341614)
    assert f["sog"] == 0.0 and f["cog"] == pytest.approx(219.3) and f["heading"] == 1
    assert "name" not in f


def test_a_static_report_maps_name_callsign_and_dimensions():
    f = ais_local.parse_message(STATIC)
    assert f["name"] == "MT.MITCHELL"
    assert f["callsign"] == "WDA9674"
    assert f["imo"] == 6710932
    assert f["type"] == 99
    assert f["length"] == 180      # to_bow + to_stern
    assert f["beam"] == 20         # to_port + to_starboard
    assert f["draught"] == 6.0
    assert f["destination"] == "SEATTLE"


def test_the_mmsi_is_a_string_because_the_cache_stores_strings():
    """AIS-catcher emits mmsi as an integer; every cache lookup compares strings."""
    assert ais_local.parse_message(POSITION)["mmsi"] == "366053209"


def test_a_message_flagged_with_an_error_is_rejected():
    """AIS-catcher still decodes a sentence whose checksum failed, and flags it with an
    `error` field -- observed 2026-08-09, where a corrupted checksum produced a full and
    entirely plausible decode. A wrong vessel name from a corrupt payload is the failure
    that costs most here, so suspect messages are dropped rather than trusted."""
    assert ais_local.parse_message({**STATIC, "error": 2}) is None


def test_a_base_station_report_is_ignored():
    """Type 4 is a shore station, not a vessel. It carries an MMSI and would otherwise
    create a cache entry that no transmission can ever refer to."""
    assert ais_local.parse_message({"class": "AIS", "type": 4, "mmsi": 2442006}) is None


def test_an_aid_to_navigation_is_ignored():
    """Type 21 is a buoy, and it carries a `name` -- so without this it would enter the
    name-keyed cache and become a candidate for vessel name matching."""
    assert ais_local.parse_message(
        {"class": "AIS", "type": 21, "mmsi": 992441000, "name": "MAAS CENTER"}) is None


def test_a_message_with_no_mmsi_is_ignored():
    assert ais_local.parse_message({"class": "AIS", "type": 1}) is None


def test_an_empty_shipname_is_not_recorded_as_a_name():
    """AIS pads unset strings; an empty name must not create a vessel called ''."""
    f = ais_local.parse_message({**STATIC, "shipname": "   "})
    assert "name" not in f


def test_a_class_b_position_is_accepted():
    """Type 18 is Class B -- smaller vessels, common in the approach."""
    f = ais_local.parse_message({"class": "AIS", "type": 18, "mmsi": 244010000,
                                 "lat": 52.0, "lon": 3.9, "speed": 4.2, "course": 90.0})
    assert f["mmsi"] == "244010000" and f["latitude"] == 52.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_ais_local.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'stt_proxy.ais_local'`

- [ ] **Step 3: Implement the adapter**

Create `server/stt_proxy/ais_local.py`:

```python
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_ais_local.py -q`
Expected: PASS (9 tests).

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/ais_local.py server/tests/test_ais_local.py
git commit -m "Map AIS-catcher JSON onto the recorder's fields"
```

---

### Task 5: The UDP listener

**Files:**
- Modify: `server/stt_proxy/ais_local.py`
- Test: `server/tests/test_ais_local.py`

**Interfaces:**
- Consumes: `ais_local.parse_message` (Task 4), `ais.record` (Task 1)
- Produces:
  - `ais_local.bind(port: int) -> socket.socket`
  - `ais_local.handle_datagram(raw: bytes) -> bool` — True if it produced a recorder call
  - `ais_local.stats() -> dict` with keys `messages`, `last_message_at`, `rejected`, `errors`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_ais_local.py`:

```python
import json
import socket

from stt_proxy import ais  # noqa: E402


@pytest.fixture
def local_state(monkeypatch):
    vessels = {}
    monkeypatch.setattr(ais, "_vessel_cache", vessels)
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending_positions", {})
    monkeypatch.setattr(ais, "AIS_LOCAL_MAX_KM", 0.0)   # filter off for transport tests
    monkeypatch.setattr(ais_local, "_stats",
                        {"messages": 0, "last_message_at": None,
                         "rejected": 0, "errors": 0})
    return vessels


def test_a_datagram_reaches_the_cache(local_state):
    ais_local.handle_datagram(json.dumps(STATIC).encode())
    assert "MT.MITCHELL" in local_state


def test_malformed_json_is_counted_and_survived(local_state):
    """A garbled datagram must never kill the listener thread."""
    assert ais_local.handle_datagram(b"{not json") is False
    assert ais_local.stats()["errors"] == 1
    assert local_state == {}


def test_an_ignored_message_type_is_counted_as_rejected(local_state):
    assert ais_local.handle_datagram(
        json.dumps({"class": "AIS", "type": 4, "mmsi": 2442006}).encode()) is False
    assert ais_local.stats()["rejected"] == 1


def test_stats_track_messages_and_the_last_message_time(local_state):
    assert ais_local.stats()["last_message_at"] is None
    ais_local.handle_datagram(json.dumps(POSITION).encode())
    assert ais_local.stats()["messages"] == 1
    assert ais_local.stats()["last_message_at"] is not None


def test_binding_a_port_someone_else_owns_fails_loudly():
    """SO_REUSEADDR is deliberately NOT set. ThreadingHTTPServer sets it, and a second
    proxy once bound alongside the first, silently took the port, and left the original
    running as a zombie -- so 'restart it' quietly did nothing. A listener that quietly
    binds a port someone else owns is that bug in a new place."""
    first = ais_local.bind(0)
    port = first.getsockname()[1]
    try:
        with pytest.raises(OSError):
            ais_local.bind(port)
    finally:
        first.close()


def test_a_bound_socket_receives_over_loopback(local_state):
    sock = ais_local.bind(0)
    try:
        port = sock.getsockname()[1]
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(json.dumps(STATIC).encode(), ("127.0.0.1", port))
        sender.close()
        sock.settimeout(2.0)
        raw, _ = sock.recvfrom(65535)
        assert ais_local.handle_datagram(raw) is True
        assert "MT.MITCHELL" in local_state
    finally:
        sock.close()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_ais_local.py -q -k "datagram or malformed or rejected or stats or binding or loopback"`
Expected: FAIL with `AttributeError: module 'stt_proxy.ais_local' has no attribute 'bind'`

- [ ] **Step 3: Implement the transport**

Append to `server/stt_proxy/ais_local.py`:

```python
import json
import os
import socket
import threading
import time

from . import ais

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
```

Move the existing `from __future__ import annotations` line to remain first in the file, and consolidate the imports at the top.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_ais_local.py -q`
Expected: PASS (15 tests).

- [ ] **Step 5: Run the full suite**

Run: `py -m pytest server/tests -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/stt_proxy/ais_local.py server/tests/test_ais_local.py
git commit -m "Receive AIS-catcher datagrams on a port that cannot be silently stolen"
```

---

### Task 6: Wiring, silence watchdog, and observability

**Files:**
- Modify: `server/whisper-proxy.py` (startup, and the `/api/ais-cache` route at line 334)
- Modify: `server/stt_proxy/ais_local.py`
- Test: `server/tests/test_ais_local.py`
- Modify (local only, **never committed**): `server/start-all.bat`

**Interfaces:**
- Consumes: `ais_local.start`, `ais_local.stats` (Task 5)
- Produces: `ais_local.silence_report(now: float) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
def test_silence_is_reported_when_nothing_has_ever_arrived(local_state, monkeypatch):
    """AIS-catcher not running looks exactly like a quiet channel. Distinguishing 'never
    received anything' from 'went quiet mid-stream' is what made the aisstream fault
    diagnosable at all."""
    monkeypatch.setattr(ais_local, "AIS_SILENCE_WARN_SEC", 60)
    monkeypatch.setattr(ais_local, "_started_at", 1000.0)
    msg = ais_local.silence_report(now=1100.0)
    assert msg is not None and "never" in msg.lower()


def test_silence_is_reported_when_the_feed_stops_mid_stream(local_state, monkeypatch):
    monkeypatch.setattr(ais_local, "AIS_SILENCE_WARN_SEC", 60)
    monkeypatch.setattr(ais_local, "_started_at", 1000.0)
    ais_local.handle_datagram(json.dumps(POSITION).encode())
    with ais_local._stats_lock:
        ais_local._stats["last_message_at"] = 1010.0
    msg = ais_local.silence_report(now=1100.0)
    assert msg is not None and "never" not in msg.lower()


def test_a_healthy_feed_reports_no_silence(local_state, monkeypatch):
    monkeypatch.setattr(ais_local, "AIS_SILENCE_WARN_SEC", 60)
    monkeypatch.setattr(ais_local, "_started_at", 1000.0)
    ais_local.handle_datagram(json.dumps(POSITION).encode())
    with ais_local._stats_lock:
        ais_local._stats["last_message_at"] = 1090.0
    assert ais_local.silence_report(now=1100.0) is None


def test_the_watchdog_is_disabled_at_zero(local_state, monkeypatch):
    monkeypatch.setattr(ais_local, "AIS_SILENCE_WARN_SEC", 0)
    monkeypatch.setattr(ais_local, "_started_at", 1000.0)
    assert ais_local.silence_report(now=99999.0) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_ais_local.py -q -k silence`
Expected: FAIL with `AttributeError: ... has no attribute 'silence_report'`

- [ ] **Step 3: Implement the watchdog**

Append to `server/stt_proxy/ais_local.py`:

```python
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
            return (f"no local AIS message has EVER arrived in {quiet:.0f}s "
                    f"— is AIS-catcher running and pointed at "
                    f"127.0.0.1:{AIS_LOCAL_UDP_PORT}?")
        return None
    quiet = now - last
    if quiet >= AIS_SILENCE_WARN_SEC:
        return f"local AIS went quiet {quiet:.0f}s ago after {stats()['messages']} messages"
    return None
```

In `start()`, set `_started_at` after a successful bind:

```python
    global _started_at
    _started_at = time.time()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_ais_local.py -q`
Expected: PASS.

- [ ] **Step 5: Wire into the proxy**

In `server/whisper-proxy.py`, alongside the existing AIS thread startup, add:

```python
from stt_proxy import ais_local
ais_local.start()
```

In the `/api/ais-cache` route (`whisper-proxy.py:334`), add a `providers` key to the JSON response:

```python
            "providers": {
                "aisstream": {"last_message_at": ais._last_message_at},
                "local": ais_local.stats(),
            },
```

- [ ] **Step 6: Run the full suite**

Run: `py -m pytest server/tests -q`
Expected: PASS.

- [ ] **Step 7: Update `start-all.bat` locally — do NOT commit it**

`server/start-all.bat` is gitignored because it holds API keys. Add, before the proxy starts:

```bat
:: -- Local AIS receiver (second dongle) -----------------------
:: Selected by SERIAL, not index: index ordering is not stable across reboots and
:: AIS-catcher would otherwise be able to seize SDR#'s dongle.
start "AIS-catcher" cmd /k D:\SDR\AIS\AIS-catcher.exe -d <SERIAL-B> -o 5 -u 127.0.0.1 10110
```

Set unique serials first, one dongle plugged in at a time:

```
rtl_eeprom -s SDRSHARP     (with only the SDR# dongle connected)
rtl_eeprom -s AISCATCHER   (with only the AIS dongle connected)
```

Verify with `D:\SDR\AIS\AIS-catcher.exe -l`.

- [ ] **Step 8: Manual end-to-end check**

Start AIS-catcher and the proxy, then:

```
curl http://localhost:9000/api/ais-cache
```

Expect `providers.local.messages` climbing and `providers.local.last_message_at` recent. Stop AIS-catcher; within 60 s the proxy should print a "went quiet" line naming the message count.

- [ ] **Step 9: Commit**

```bash
git add server/whisper-proxy.py server/stt_proxy/ais_local.py server/tests/test_ais_local.py
git commit -m "Start the local AIS listener and report per-provider health"
```

---

## Self-Review

**Spec coverage:** external AIS-catcher process (Task 6 step 7) · UDP JSON consumption (Tasks 4, 5) · provider-neutral recorder (Task 1) · MMSI index and pending positions (Task 1) · merge rules incl. newest-wins position (Task 1) · radius filter (Task 2) · aisstream onto the recorder (Task 3) · silence watchdog (Task 6) · no-`SO_REUSEADDR` bind (Task 5) · malformed JSON rate-limited (Task 5) · per-source counters on `/api/ais-cache` (Task 6) · offline tests throughout. **All spec sections have a task.**

**Out of scope, as specified:** AISHub, proxy supervision of AIS-catcher, the webapp, changes to the name matcher or resolver.

**Type consistency:** `record(fields, *, source, observed_at=None)` is used with that exact signature in Tasks 1, 2, 3, 5. `parse_message(msg) -> dict | None` matches its use in Task 5. `stats()` returns the four keys asserted in Tasks 5 and 6. `mmsi` is a `str` everywhere — Task 4 converts AIS-catcher's integer explicitly, with a test pinning it.

**Known follow-ups, deliberately not tasks:** `match_by_mmsi` still linear-scans and could now use `_mmsi_index` — a separate optimisation with its own tests. `AIS_LOCAL_MAX_KM` wants tuning against `bench_identify --repeats 3` once the cache is locally populated; that is a measurement session, not code.
