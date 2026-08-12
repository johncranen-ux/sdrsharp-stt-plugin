# AISHub Vessel Source Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the dead aisstream feed with AISHub as the vessel source, make the cache hold ships that share a name, and surface contested identifications to the operator instead of guessing.

**Architecture:** A new `stt_proxy/aishub.py` polls AISHub's REST endpoint every 15 minutes and writes through the same `record()` merge point the aisstream adapter uses. The cache gains an MMSI index and a name→candidates index, so fourteen ships called ALBATROS stay fourteen ships. When a heard name matches more than one plausible vessel, `/conversations` renders the candidates with VesselFinder links rather than picking one.

**Tech Stack:** Python 3 (stdlib `urllib` for HTTP — no new dependencies), `rapidfuzz` for name matching, `pytest` for tests.

**Design spec:** `docs/superpowers/specs/2026-08-12-aishub-vessel-source-design.md`

## Global Constraints

- **Run all tests from the `server/` directory:** `python -m pytest tests/ -q`. Baseline is **654 passing** before any change; it must never drop.
- **No new runtime dependencies.** `server/requirements.txt` stays exactly as it is. Use `urllib.request` from the stdlib, not `requests`. Do **not** add `pyais`.
- **Never commit the AISHub credential.** No key in source, tests, fixtures, docs, or commit messages. This repo already required a `git filter-repo` history rewrite over committed data.
- **Configuration reaches the proxy through `server/start-all.bat`, not through a `.env` file.** Nothing in the Python loads dotenv — every setting is a plain `os.environ.get`. `AISHUB_USERNAME` is already set there alongside the other keys (the file is gitignored and untracked; verified with `git check-ignore`). For the manually-run scripts in Tasks 3 and 7, export it in the shell for that session — do **not** add a second copy of the secret to a `.env`, and do **not** add a dotenv loader, which would be a new dependency outside this plan's scope.
- **Never commit real AIS payloads as fixtures.** `ais_cache.json` and vessel data are gitignored under NL Telecommunicatiewet 18.13 / ITU RR 17.3. All fixtures are synthetic and hand-written.
- **AISHub rate limit is one request per minute**, enforced by them with silent data denial (HTTP 200, `ERROR: true`, no ships). Any code path that calls the API enforces a 60-second floor itself.
- **Work on branch `feat/aishub-vessel-source`**, which already exists and holds the design commit `67d0174`.
- Reach module state through the module (`ais._vessel_cache`), never `from ais import _vessel_cache` — the feed thread mutates it, and binding the name captures a snapshot.

---

## File Structure

| file | responsibility |
|---|---|
| `server/stt_proxy/ais.py` (modify) | Cache, indexes, `record()`/`_apply()` merge, matchers, ranking. Gains `_mmsi_index`, `_pending`, `_name_index`, `_in_scope`, candidate lookup. |
| `server/stt_proxy/aishub.py` (create) | AISHub HTTP client, response validation, field mapping, poll loop. Writes only via `ais.record()`. |
| `server/stt_proxy/markup.py` (modify) | VesselFinder URL: search → details page. |
| `server/stt_proxy/conversations.py` (modify) | Render the candidate block on `/conversations`. |
| `server/whisper-proxy.py` (modify) | `AIS_SOURCE` selector replacing the `AISSTREAM_API_KEY`-presence check. |
| `server/tests/test_aishub.py` (create) | Unit tests for the client: mapping, `ERROR: true`, rate floor, failures. |
| `server/tests/test_whisper_proxy.py` (modify) | Tests for `record()`, indexes, ranking, candidates, rendering. |
| `server/aishub_contract_check.py` (create) | Manually-run contract test against the live endpoint. Not in CI. |

## Two adaptations to the ported code — read before Task 1

The `record()` implementation being ported from `feat/local-ais-receiver` needs two changes. They are not optional and they are easy to miss.

**1. `_apply` must stamp the observation time, not the receipt time.** The branch writes `entry["last_seen"] = _now()`. AISHub's `TIME` field is the timestamp of the position report, and the whole point of adopting it is that `last_seen` becomes true. `_apply` receives `when` (a UNIX float) already, so it formats that instead. For aisstream nothing changes, because its adapter passes `when = time.time()`.

**Use `datetime.fromtimestamp(when)` — local, no timezone argument.** `_now()` is `datetime.datetime.now()` and `_is_fresh` compares against a naive local `cutoff`, so every `last_seen` already written is local wall-clock. Stamping UTC would put new entries two hours from the old ones (this machine is UTC+2) and quietly break freshness comparisons. Verified: `fromtimestamp(time.time())` and `_now()` produce identical strings, so the aisstream path is bit-for-bit unchanged.

**2. Drop the `AIS_LOCAL_MAX_KM` admission gate.** The branch refuses to admit a vessel more than 40 km from Maas Center. Our bounding box deliberately reaches ~140 km west, so that gate would reject most of the box and defeat the lead-time design. `ais_local.py` is not being ported, so the setting has no remaining consumer. Keep `_MAAS_CENTER` and `_km_from_maas` — the ranking needs them — and delete the gate and the env var.

---

### Task 1: Port the multi-source merge core

**Files:**
- Modify: `server/stt_proxy/ais.py` (add after `_fresh_snapshot`, ~line 396; rewrite `_process_ais` at 398-471)
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: nothing (first task)
- Produces:
  - `ais.record(fields: dict, *, source: str, observed_at: float | None = None) -> None`
  - `ais._apply(entry: dict, fields: dict, when: float, source: str) -> None`
  - `ais._mmsi_index: dict[str, dict]`, `ais._pending: dict[str, dict]`
  - `ais._km_from_maas(lat: float, lon: float) -> float`, `ais._MAAS_CENTER: tuple[float, float]`
  - `fields` keys recognised: `mmsi, name, callsign, type, imo, length, beam, draught, destination, latitude, longitude, sog, cog, heading`

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_whisper_proxy.py`:

```python
def test_record_admits_a_named_vessel_and_indexes_it_by_mmsi(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "244123456", "name": "ORASUND", "callsign": "PBZL",
                "latitude": 52.0, "longitude": 3.9}, source="test")

    assert "ORASUND" in ais._vessel_cache
    assert ais._mmsi_index["244123456"]["name"] == "ORASUND"
    assert ais._callsign_cache["PBZL"]["mmsi"] == "244123456"


def test_record_holds_a_position_until_a_name_arrives(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "244000111", "latitude": 51.9, "longitude": 4.0},
               source="test", observed_at=1000.0)
    assert ais._vessel_cache == {}
    assert "244000111" in ais._pending

    ais.record({"mmsi": "244000111", "name": "LATE NAME"},
               source="test", observed_at=1001.0)

    entry = ais._vessel_cache["LATE NAME"]
    assert entry["latitude"] == 51.9
    assert "244000111" not in ais._pending


def test_record_does_not_alias_two_ships_that_share_a_name(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "111111111", "name": "ALBATROS"}, source="test")
    ais.record({"mmsi": "222222222", "name": "ALBATROS"}, source="test")

    assert ais._mmsi_index["111111111"]["mmsi"] == "111111111"
    assert ais._mmsi_index["222222222"]["mmsi"] == "222222222"
    assert ais._mmsi_index["111111111"] is not ais._mmsi_index["222222222"]


def test_record_keeps_the_newer_position_when_an_older_one_arrives_late(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "244777888", "name": "NEWEST WINS",
                "latitude": 52.5, "longitude": 4.5}, source="a", observed_at=2000.0)
    ais.record({"mmsi": "244777888", "latitude": 51.0, "longitude": 3.0},
               source="b", observed_at=1000.0)

    assert ais._vessel_cache["NEWEST WINS"]["latitude"] == 52.5


def test_record_stamps_last_seen_from_the_observation_not_the_clock(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    import datetime as _dt
    observed = 1786528800.0        # 2026-08-12 10:00:00 UTC
    ais.record({"mmsi": "244999000", "name": "TIMESTAMPED",
                "latitude": 52.0, "longitude": 4.0},
               source="aishub", observed_at=observed)

    # Expectation computed, not hardcoded: last_seen is local wall-clock throughout this
    # codebase, so a literal would only pass in one timezone.
    expected = _dt.datetime.fromtimestamp(observed).strftime("%Y-%m-%d %H:%M:%S")
    assert ais._vessel_cache["TIMESTAMPED"]["last_seen"] == expected
    assert ais._vessel_cache["TIMESTAMPED"]["last_seen"] != ais._now()


def test_record_never_blanks_a_name_with_an_empty_string(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})

    ais.record({"mmsi": "244321000", "name": "KEEPS ITS NAME"}, source="test")
    ais.record({"mmsi": "244321000", "name": "", "latitude": 52.0,
                "longitude": 4.0}, source="test")

    assert ais._mmsi_index["244321000"]["name"] == "KEEPS ITS NAME"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_whisper_proxy.py -q -k record_` (from `server/`)
Expected: FAIL — `AttributeError: module 'stt_proxy.ais' has no attribute 'record'`

- [ ] **Step 3: Add `math` to the imports and the distance helper**

In `server/stt_proxy/ais.py`, add `import math` to the import block at the top (alphabetical, after `import json`). Then add above `_stale_filter_warned` (~line 247):

```python
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

- [ ] **Step 4: Add the indexes and the merge functions**

Insert after `_fresh_snapshot` (after line 395) in `server/stt_proxy/ais.py`:

```python
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

        if entry is not None:
            # An observation for this MMSI seen before it was admitted -- held in _pending
            # because nothing existed to attach it to -- must not be discarded now that
            # something does. Flushed BEFORE the current observation so the newest-wins
            # position logic still picks whichever is actually newer.
            pending = _pending.pop(mmsi, None)
            if pending is not None:
                _apply(entry, pending, pending.get("position_at", when),
                       pending.get("source", source))

            _apply(entry, fields, when, source)
            _mmsi_index[mmsi] = entry
            _index_name(entry)
            if entry.get("callsign"):
                _callsign_cache[entry["callsign"].upper()] = entry
            return

        # Not yet admitted: accumulate until there is a name.
        held = _pending.setdefault(mmsi, {"mmsi": mmsi})
        _apply(held, fields, when, source)
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
        if entry.get("callsign"):
            _callsign_cache[entry["callsign"].upper()] = entry


def _index_name(entry: dict) -> None:
    """Record this MMSI under its name. Caller holds _cache_lock.

    Filled in properly in Task 4; a no-op here keeps record() and its call sites final so
    Task 4 changes one function rather than five call sites.
    """
    return


def _apply(entry: dict, fields: dict, when: float, source: str) -> None:
    """Merge one observation's fields into `entry`, in place.

    Static fields fill or update. Position applies only if this observation is newer than the
    stored fix -- newest-wins, so a vessel heard two hours ago does not keep a stale fix over
    a fresh one.

    A static field of "" is treated the same as absent, never written: "" is exactly as
    uninformative as a missing key for every field record() recognises, and adapters routinely
    default absent strings to "".

    `last_seen` is stamped from `when` -- the OBSERVATION time -- not from the clock. AISHub's
    TIME field is when the position was reported, and making last_seen true is the whole
    reason for adopting it. For aisstream nothing changes: its adapter passes time.time(), and
    fromtimestamp(time.time()) is exactly what _now() produced.

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
        entry["last_seen"] = datetime.datetime.fromtimestamp(when).strftime(_LAST_SEEN_FMT)
```

- [ ] **Step 5: Rewrite `_process_ais` as a thin aisstream adapter**

Replace the whole body of `_process_ais` (lines 398-471) in `server/stt_proxy/ais.py` with:

```python
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
```

- [ ] **Step 6: Rebuild the MMSI index on cache load**

In `_load_cache` (line 87-91), inside the `with _cache_lock:` block, add index population. Replace the loop body with:

```python
            for entry in entries:
                _vessel_cache[entry["name"].upper()] = entry
                if entry.get("mmsi"):
                    _mmsi_index[str(entry["mmsi"])] = entry
                if entry.get("callsign"):
                    _callsign_cache[entry["callsign"].upper()] = entry
                _index_name(entry)
```

- [ ] **Step 7: Run the new tests and the full suite**

Run: `python -m pytest tests/test_whisper_proxy.py -q -k record_`
Expected: PASS (6 tests)

Run: `python -m pytest tests/ -q`
Expected: **654 passed** — the aisstream rewrite must not change any existing behaviour. If any pre-existing test fails, the adapter is wrong; fix the adapter, do not edit the test.

- [ ] **Step 8: Commit**

```bash
git add server/stt_proxy/ais.py server/tests/test_whisper_proxy.py
git commit -m "Give the vessel cache one merge point and an MMSI index

Ported from feat/local-ais-receiver, with two changes. _apply now stamps
last_seen from the observation time rather than the clock, so AISHub's TIME
field can make it true. The AIS_LOCAL_MAX_KM admission gate is dropped: the
bounding box reaches ~140 km west and a 40 km radius would reject most of it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: AISHub response parsing

**Files:**
- Create: `server/stt_proxy/aishub.py`
- Test: `server/tests/test_aishub.py` (create)

**Interfaces:**
- Consumes: `ais.record`, `ais._clean_destination` from Task 1
- Produces:
  - `aishub.AisHubError(Exception)`
  - `aishub.parse_response(payload: bytes) -> list[dict]` — raises `AisHubError` on any non-observation
  - `aishub.map_ship(ship: dict) -> dict | None` — AISHub record → `record()` fields, `None` if unusable
  - `aishub.parse_time(raw: str) -> float | None` — `"2026-08-12 10:02:58 GMT"` → UNIX float

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_aishub.py`:

```python
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from stt_proxy import aishub  # noqa: E402


def _envelope(records, error=False):
    return json.dumps([
        {"ERROR": error, "USERNAME": "X", "FORMAT": "HUMAN", "RECORDS": len(records)},
        records,
    ]).encode("utf-8")


SHIP = {
    "MMSI": 244123456, "TIME": "2026-08-12 10:02:58 GMT",
    "LONGITUDE": 3.95477, "LATITUDE": 52.06695, "COG": 51.3, "SOG": 4.9,
    "HEADING": 103, "IMO": 9406714, "NAME": "ORASUND", "CALLSIGN": "PBZL",
    "TYPE": 70, "A": 100, "B": 20, "C": 8, "D": 7, "DRAUGHT": 7.4,
    "DEST": "NLRTM",
}


def test_parse_response_returns_the_ships():
    ships = aishub.parse_response(_envelope([SHIP]))
    assert len(ships) == 1
    assert ships[0]["NAME"] == "ORASUND"


def test_parse_response_treats_the_error_flag_as_no_observation():
    # The rate-limit response: HTTP 200, valid JSON, ERROR true, no ships. Read as an
    # empty box it would mark every cached vessel out of scope.
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(_envelope([], error=True))


def test_parse_response_rejects_a_missing_ships_array():
    payload = json.dumps([{"ERROR": False, "RECORDS": 0}]).encode("utf-8")
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(payload)


def test_parse_response_rejects_malformed_json():
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(b"<html>502 Bad Gateway</html>")


def test_parse_response_allows_a_genuinely_empty_box():
    # Distinct from ERROR: a real observation that found nothing.
    assert aishub.parse_response(_envelope([])) == []


def test_map_ship_maps_every_field_record_understands():
    fields = aishub.map_ship(SHIP)
    assert fields["mmsi"] == "244123456"
    assert fields["name"] == "ORASUND"
    assert fields["callsign"] == "PBZL"
    assert fields["imo"] == 9406714
    assert fields["type"] == 70
    assert fields["length"] == 120      # A + B
    assert fields["beam"] == 15         # C + D
    assert fields["draught"] == 7.4
    assert fields["destination"] == "NLRTM"
    assert fields["latitude"] == 52.06695
    assert fields["longitude"] == 3.95477
    assert fields["sog"] == 4.9
    assert fields["cog"] == 51.3
    assert fields["heading"] == 103


def test_map_ship_drops_a_record_with_no_mmsi():
    assert aishub.map_ship({"NAME": "NO IDENTITY"}) is None


def test_map_ship_survives_absent_optional_fields():
    fields = aishub.map_ship({"MMSI": 1, "NAME": "SPARSE"})
    assert fields["mmsi"] == "1"
    assert fields["length"] is None
    assert fields["draught"] is None


def test_map_ship_strips_ais_destination_padding():
    fields = aishub.map_ship({**SHIP, "DEST": "ROTTERDAM@@@@@@@"})
    assert fields["destination"] == "ROTTERDAM"


def test_parse_time_reads_the_gmt_stamp():
    # Absolute epoch, so this assertion is timezone-independent.
    assert aishub.parse_time("2026-08-12 10:02:58 GMT") == 1786528978.0


def test_parse_time_returns_none_on_junk():
    assert aishub.parse_time("not a time") is None
    assert aishub.parse_time("") is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_aishub.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'stt_proxy.aishub'`

- [ ] **Step 3: Create the module with the parsing half**

Create `server/stt_proxy/aishub.py`:

```python
"""AISHub as the vessel source.

Polls AISHub's REST webservice for every vessel in a bounding box and writes each one into
the shared cache through `ais.record()`. Nothing here touches the cache directly -- the merge
lives in one place so two providers cannot get it wrong two different ways.

Replaces aisstream, which has delivered nothing since 2026-08-05. The aisstream path is still
live and still tested; `AIS_SOURCE` chooses between them.

The failure mode that shapes this module: AISHub answers a rate-limit violation with HTTP 200
and a valid-JSON body carrying `ERROR: true` and no ships. Read naively that is
indistinguishable from "the box is empty", which would mark every cached vessel out of scope
and silently destroy identification. Every non-observation raises AisHubError instead, and the
caller leaves the cache untouched.
"""

import datetime
import json

from stt_proxy.ais import _clean_destination

API_URL = "https://data.aishub.net/ws.php"

# AISHub's documented limit, verbatim: "Don't access the webservice more frequently than once
# per minute! The web service will return nothing if executed more frequently!" The penalty is
# silent data denial, so this is enforced in code and not left to configuration.
MIN_INTERVAL_SEC = 60

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class AisHubError(Exception):
    """The response was not an observation. The cache must not be updated from it."""


def parse_time(raw: str) -> float | None:
    """AISHub's "2026-08-12 10:02:58 GMT" as a UNIX timestamp, or None.

    Always UTC: the field is documented as a UTC datetime and carries a literal "GMT" suffix,
    so it is stripped rather than parsed as a zone name (%Z does not round-trip reliably).
    """
    text = (raw or "").strip()
    if text.endswith(" GMT"):
        text = text[:-4]
    try:
        naive = datetime.datetime.strptime(text, _TIME_FMT)
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=datetime.timezone.utc).timestamp()


def parse_response(payload: bytes) -> list[dict]:
    """The ships in an AISHub response, or raise AisHubError.

    An empty list is a real answer -- a box that genuinely holds nothing. ERROR, a missing
    ships array, and unparseable content are all "we learned nothing", which is a different
    fact and must never reach the cache as an emptiness claim.
    """
    try:
        body = json.loads(payload.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError) as exc:
        raise AisHubError(f"response was not JSON: {exc}") from exc

    if not isinstance(body, list) or not body:
        raise AisHubError(f"unexpected response shape: {type(body).__name__}")

    envelope = body[0] if isinstance(body[0], dict) else {}
    if envelope.get("ERROR"):
        detail = envelope.get("ERROR_MESSAGE") or "no message"
        raise AisHubError(f"server reported ERROR: {detail}")

    if len(body) < 2 or not isinstance(body[1], list):
        raise AisHubError("response carried no ships array")

    return body[1]


def _dimension(ship: dict, near: str, far: str):
    """Length or beam from the two half-dimensions, or None when neither was reported."""
    total = (ship.get(near) or 0) + (ship.get(far) or 0)
    return total or None


def map_ship(ship: dict) -> dict | None:
    """One AISHub record as the fields `ais.record()` understands, or None if unusable."""
    mmsi = str(ship.get("MMSI") or "").strip()
    if not mmsi:
        return None
    return {
        "mmsi": mmsi,
        "name": (ship.get("NAME") or "").strip(),
        "callsign": (ship.get("CALLSIGN") or "").strip(),
        "type": ship.get("TYPE"),
        "imo": ship.get("IMO"),
        "length": _dimension(ship, "A", "B"),
        "beam": _dimension(ship, "C", "D"),
        "draught": ship.get("DRAUGHT"),
        "destination": _clean_destination(ship.get("DEST") or ""),
        "latitude": ship.get("LATITUDE"),
        "longitude": ship.get("LONGITUDE"),
        "sog": ship.get("SOG"),
        "cog": ship.get("COG"),
        "heading": ship.get("HEADING"),
    }
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_aishub.py -q`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/aishub.py server/tests/test_aishub.py
git commit -m "Read an AISHub response without mistaking failure for an empty sea

AISHub answers a rate-limit violation with HTTP 200 and valid JSON carrying
ERROR true and no ships. Treated as an observation that would mark every
cached vessel out of scope, so every non-observation raises instead.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: The poll loop and source selection

**Files:**
- Modify: `server/stt_proxy/aishub.py`
- Modify: `server/whisper-proxy.py:507-514`
- Test: `server/tests/test_aishub.py`

**Interfaces:**
- Consumes: `aishub.parse_response`, `aishub.map_ship`, `aishub.parse_time`, `ais.record` from Tasks 1-2
- Produces:
  - `aishub.build_url(username: str, bbox: tuple[float, float, float, float]) -> str`
  - `aishub.poll_once(username: str, bbox, fetch=None) -> int` — vessels recorded; raises `AisHubError`
  - `aishub.poll_loop(username: str) -> None` — daemon-thread entry point
  - `aishub.BBOX: tuple[float, float, float, float]`, `aishub.POLL_SEC: int`
  - `ais.set_in_scope(mmsis: set[str]) -> None`, `ais.get_in_scope() -> set[str]`

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_aishub.py`:

```python
def test_build_url_carries_the_box_and_asks_for_json():
    url = aishub.build_url("USER", (51.0, 53.2, 2.0, 6.0))
    assert url.startswith(aishub.API_URL + "?")
    assert "username=USER" in url
    assert "output=json" in url
    assert "format=1" in url
    assert "latmin=51.0" in url and "latmax=53.2" in url
    assert "lonmin=2.0" in url and "lonmax=6.0" in url


def test_poll_once_records_every_named_vessel(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    payload = _envelope([SHIP, {**SHIP, "MMSI": 999, "NAME": "SECOND"}])
    count = aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: payload)

    assert count == 2
    assert ais._mmsi_index["244123456"]["name"] == "ORASUND"
    assert ais._mmsi_index["999"]["name"] == "SECOND"


def test_poll_once_uses_the_report_time_as_last_seen(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    import datetime as _dt
    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))

    # SHIP's TIME is 2026-08-12 10:02:58 GMT. Stored as local wall-clock, like every other
    # last_seen in this codebase, so compute rather than hardcode.
    expected = _dt.datetime.fromtimestamp(1786528978.0).strftime("%Y-%m-%d %H:%M:%S")
    assert ais._vessel_cache["ORASUND"]["last_seen"] == expected


def test_poll_once_publishes_the_in_scope_set(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    payload = _envelope([SHIP, {**SHIP, "MMSI": 999, "NAME": "SECOND"}])
    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: payload)

    assert ais.get_in_scope() == {"244123456", "999"}


def test_a_failed_poll_leaves_the_cache_and_the_scope_alone(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))
    before_cache = dict(ais._vessel_cache)
    before_scope = set(ais.get_in_scope())

    with pytest.raises(aishub.AisHubError):
        aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0),
                         fetch=lambda url: _envelope([], error=True))

    assert ais._vessel_cache == before_cache
    assert ais.get_in_scope() == before_scope


def test_the_poll_interval_cannot_be_configured_below_the_rate_limit(monkeypatch):
    monkeypatch.setenv("AISHUB_POLL_SEC", "5")
    assert aishub._resolve_poll_sec() == aishub.MIN_INTERVAL_SEC


def test_the_poll_interval_honours_a_legal_setting(monkeypatch):
    monkeypatch.setenv("AISHUB_POLL_SEC", "900")
    assert aishub._resolve_poll_sec() == 900
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_aishub.py -q -k "build_url or poll_ or interval or scope or failed_poll"`
Expected: FAIL — `AttributeError: module 'stt_proxy.aishub' has no attribute 'build_url'`

- [ ] **Step 3: Add the in-scope set to `ais.py`**

Add to `server/stt_proxy/ais.py`, immediately after the `_pending` definition from Task 1:

```python
# MMSIs returned by the most recent SUCCESSFUL poll. Empty means "no source has reported yet"
# and is treated as "everything is in scope", so aisstream and a cold start behave as before.
#
# Scope is defined against the last good poll rather than against wall-clock age deliberately.
# "last_seen within N minutes of now" would make a feed outage indistinguishable from every
# ship leaving the estuary -- and this project has already lost six days to a feed that failed
# quietly.
_in_scope: set[str] = set()


def set_in_scope(mmsis: set[str]) -> None:
    """Publish the vessels the latest good poll saw. Called only on success."""
    global _in_scope
    with _cache_lock:
        _in_scope = set(mmsis)


def get_in_scope() -> set[str]:
    with _cache_lock:
        return set(_in_scope)
```

- [ ] **Step 4: Add the polling half of `aishub.py`**

Append to `server/stt_proxy/aishub.py` (and add `import os`, `import threading`, `import time`, `import urllib.error`, `import urllib.request` to its imports, plus `from stt_proxy import ais`):

```python
def _resolve_bbox() -> tuple[float, float, float, float]:
    """(latmin, latmax, lonmin, lonmax) for the poll.

    Wide on purpose. The margin is what buys lead time: the western edge sits ~140 km from
    Maas Center, which is over two hours of steaming at 15 knots, so a vessel is cached long
    before it calls and the poll can be slow. The cost is that a wide box carries 777
    duplicate-name groups against the approach box's 17 -- which is why matching ranks
    candidates by proximity rather than trusting the box to disambiguate.
    """
    raw = os.environ.get("AISHUB_BBOX", "51.0,53.2,2.0,6.0")
    try:
        latmin, latmax, lonmin, lonmax = (float(p) for p in raw.split(","))
    except ValueError:
        print(f"[AISHub] bad AISHUB_BBOX {raw!r}, using the default", flush=True)
        return (51.0, 53.2, 2.0, 6.0)
    return (latmin, latmax, lonmin, lonmax)


def _resolve_poll_sec() -> int:
    """Seconds between polls, never below the rate limit whatever the environment says."""
    try:
        wanted = int(os.environ.get("AISHUB_POLL_SEC", "900"))
    except ValueError:
        wanted = 900
    return max(wanted, MIN_INTERVAL_SEC)


BBOX     = _resolve_bbox()
POLL_SEC = _resolve_poll_sec()


def build_url(username: str, bbox: tuple[float, float, float, float]) -> str:
    latmin, latmax, lonmin, lonmax = bbox
    query = urllib.parse.urlencode({
        "username": username,
        "format": 1,            # human-readable: degrees and knots, not raw AIS scaling
        "output": "json",
        "latmin": latmin, "latmax": latmax,
        "lonmin": lonmin, "lonmax": lonmax,
    })
    return f"{API_URL}?{query}"


def _fetch(url: str) -> bytes:
    """GET the URL with gzip. Uncompressed this box is 2.66 MB a poll."""
    request = urllib.request.Request(url, headers={
        "Accept-Encoding": "gzip",
        "User-Agent": "sdrsharp-stt-proxy/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw
    except (urllib.error.URLError, OSError) as exc:
        raise AisHubError(f"fetch failed: {exc}") from exc


def poll_once(username: str, bbox, fetch=None) -> int:
    """One poll. Returns vessels recorded, or raises AisHubError having changed nothing.

    The cache is only touched once the whole response has been validated and parsed, so a
    failure part-way through cannot leave a half-updated scope set.
    """
    ships = parse_response((fetch or _fetch)(build_url(username, bbox)))

    seen: set[str] = set()
    recorded = 0
    for ship in ships:
        fields = map_ship(ship)
        if fields is None:
            continue
        seen.add(fields["mmsi"])
        ais.record(fields, source="aishub", observed_at=parse_time(ship.get("TIME", "")))
        recorded += 1

    ais.set_in_scope(seen)
    return recorded


def poll_loop(username: str) -> None:
    """Poll forever. Daemon-thread entry point; never raises."""
    print(f"[AISHub] polling {BBOX} every {POLL_SEC}s", flush=True)
    failures = 0
    while True:
        try:
            count = poll_once(username, BBOX)
            if failures:
                print(f"[AISHub] recovered after {failures} failed poll(s)", flush=True)
            failures = 0
            print(f"[AISHub] {count} vessels", flush=True)
        except AisHubError as exc:
            failures += 1
            # Rate-limited every time would be a configuration bug, so say so early and then
            # stop repeating it; a long outage should not drown the console the way the
            # aisstream silence warning did.
            if failures <= 3 or failures % 20 == 0:
                print(f"[AISHub] poll failed ({failures}): {exc}. "
                      f"Cache and scope left untouched.", flush=True)
        except Exception as exc:
            failures += 1
            print(f"[AISHub] unexpected poll error: {exc}", flush=True)
        time.sleep(POLL_SEC)


def start(username: str) -> None:
    threading.Thread(target=poll_loop, args=(username,), daemon=True).start()
```

Also add `import gzip` and `import urllib.parse` to the module imports.

- [ ] **Step 5: Wire the source selector into the proxy**

Replace `server/whisper-proxy.py:507-514` with:

```python
    ais_source = os.environ.get("AIS_SOURCE", "aishub").strip().lower()
    if ais_source == "aishub":
        aishub_user = os.environ.get("AISHUB_USERNAME", "")
        if aishub_user:
            aishub.start(aishub_user)
            threading.Thread(target=_periodic_save, daemon=True).start()
            atexit.register(_save_cache)
            print(f"AIS feed: AISHub, box {aishub.BBOX}, every {aishub.POLL_SEC}s", flush=True)
        else:
            print("AIS feed: disabled (AIS_SOURCE=aishub but AISHUB_USERNAME is unset)",
                  flush=True)
    elif ais_source == "aisstream":
        # Kept live and tested rather than commented out. aisstream was a reliable free
        # source for a long time and may return; code that is not exercised does not work
        # when it is reverted to.
        ais_key = os.environ.get("AISSTREAM_API_KEY", "")
        if ais_key:
            threading.Thread(target=_ais_thread, args=(ais_key,), daemon=True).start()
            threading.Thread(target=_periodic_save, daemon=True).start()
            atexit.register(_save_cache)
            print("AIS feed: aisstream, starting...", flush=True)
        else:
            print("AIS feed: disabled (AIS_SOURCE=aisstream but AISSTREAM_API_KEY is unset)",
                  flush=True)
    else:
        print(f"AIS feed: disabled (AIS_SOURCE={ais_source})", flush=True)
```

Add `from stt_proxy import aishub  # noqa: E402` next to the existing `from stt_proxy import ais` at line 143.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_aishub.py -q`
Expected: PASS (18 tests)

Run: `python -m pytest tests/ -q`
Expected: **654 passed** plus the new `test_aishub.py` tests.

- [ ] **Step 7: Verify against the live endpoint by hand**

`AISHUB_USERNAME` is already set in `server/start-all.bat`, but that only applies to a proxy launched by it. For this one-off, export it into your shell first (PowerShell: `$env:AISHUB_USERNAME = "<the key from start-all.bat>"`). Then from `server/`:

```bash
python -c "
import os
from stt_proxy import aishub
n = aishub.poll_once(os.environ['AISHUB_USERNAME'], aishub.BBOX)
from stt_proxy import ais
print('recorded', n, 'in-scope', len(ais.get_in_scope()))
"
```

Expected: roughly 9,000 recorded, in-scope the same order. If it prints `ERROR`, wait 60 seconds — you are rate-limited.

- [ ] **Step 8: Commit**

```bash
git add server/stt_proxy/aishub.py server/stt_proxy/ais.py server/whisper-proxy.py server/tests/test_aishub.py
git commit -m "Poll AISHub for vessels, and choose the source with AIS_SOURCE

A failed poll leaves the cache and the in-scope set exactly as they were, so a
dead feed reads as a stale cache rather than an empty sea. The interval floor
is enforced in code because the documented penalty for exceeding it is silent
data denial.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Name index and candidate ranking

**Files:**
- Modify: `server/stt_proxy/ais.py`
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `ais.record`, `ais._index_name`, `ais.get_in_scope`, `ais._km_from_maas` from Tasks 1 and 3
- Produces:
  - `ais._name_index: dict[str, list[str]]` — NAME → MMSIs, insertion-ordered, no duplicates
  - `ais._type_plausibility(type_code) -> int` — 3 commercial, 2 working/unknown, 1 leisure
  - `ais._candidate_sort_key(entry: dict, in_scope: set[str]) -> tuple`
  - `ais.candidates_for_name(name: str) -> list[dict]` — every cached ship with that exact name, ranked

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_whisper_proxy.py`:

```python
def _fresh_caches(monkeypatch):
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_name_index", {})
    monkeypatch.setattr(ais, "_in_scope", set())


def test_the_name_index_holds_every_ship_that_shares_a_name(monkeypatch):
    _fresh_caches(monkeypatch)
    for mmsi in ("111", "222", "333"):
        ais.record({"mmsi": mmsi, "name": "ALBATROS"}, source="test")

    assert ais._name_index["ALBATROS"] == ["111", "222", "333"]


def test_the_name_index_does_not_repeat_an_mmsi(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "111", "name": "ALBATROS"}, source="test")
    ais.record({"mmsi": "111", "name": "ALBATROS", "latitude": 52.0,
                "longitude": 4.0}, source="test")

    assert ais._name_index["ALBATROS"] == ["111"]


def test_candidates_rank_an_in_scope_vessel_above_one_that_left(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "gone", "name": "FORTUNA", "latitude": 52.02,
                "longitude": 3.88, "type": 70}, source="test")
    ais.record({"mmsi": "here", "name": "FORTUNA", "latitude": 51.0,
                "longitude": 3.0, "type": 70}, source="test")
    ais.set_in_scope({"here"})

    assert [c["mmsi"] for c in ais.candidates_for_name("FORTUNA")] == ["here", "gone"]


def test_candidates_rank_the_nearer_vessel_first(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "far", "name": "DELTA", "latitude": 51.2,
                "longitude": 5.8, "type": 70}, source="test")
    ais.record({"mmsi": "near", "name": "DELTA", "latitude": 52.03,
                "longitude": 3.89, "type": 70}, source="test")
    ais.set_in_scope({"far", "near"})

    assert [c["mmsi"] for c in ais.candidates_for_name("DELTA")] == ["near", "far"]


def test_candidates_rank_a_tanker_above_a_yacht_at_the_same_place(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "yacht", "name": "ZEUS", "latitude": 52.02,
                "longitude": 3.88, "type": 36}, source="test")
    ais.record({"mmsi": "tanker", "name": "ZEUS", "latitude": 52.02,
                "longitude": 3.88, "type": 70}, source="test")
    ais.set_in_scope({"yacht", "tanker"})

    assert [c["mmsi"] for c in ais.candidates_for_name("ZEUS")] == ["tanker", "yacht"]


def test_candidates_put_a_vessel_with_no_position_last_but_keep_it(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "nopos", "name": "CONDOR", "type": 70}, source="test")
    ais.record({"mmsi": "haspos", "name": "CONDOR", "latitude": 52.0,
                "longitude": 3.9, "type": 70}, source="test")
    ais.set_in_scope({"nopos", "haspos"})

    assert [c["mmsi"] for c in ais.candidates_for_name("CONDOR")] == ["haspos", "nopos"]


def test_everything_is_in_scope_before_any_poll_has_succeeded(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "a", "name": "SOLO", "latitude": 52.0,
                "longitude": 3.9}, source="test")

    assert len(ais.candidates_for_name("SOLO")) == 1


def test_candidates_for_an_unknown_name_is_empty(monkeypatch):
    _fresh_caches(monkeypatch)
    assert ais.candidates_for_name("NO SUCH SHIP") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_whisper_proxy.py -q -k "name_index or candidates or in_scope_before"`
Expected: FAIL — `AttributeError: module 'stt_proxy.ais' has no attribute '_name_index'`

- [ ] **Step 3: Add the index, the plausibility table and the ranking**

In `server/stt_proxy/ais.py`, add `_name_index` next to `_mmsi_index`:

```python
# NAME -> the MMSIs of every ship carrying it. _vessel_cache can only hold one entry per name,
# so this is the only thing that keeps fourteen ALBATROS apart.
_name_index: dict[str, list[str]] = {}
```

Replace the placeholder `_index_name` from Task 1 with:

```python
def _index_name(entry: dict) -> None:
    """Record this MMSI under its name. Caller holds _cache_lock."""
    name = (entry.get("name") or "").strip().upper()
    mmsi = str(entry.get("mmsi") or "").strip()
    if not name or not mmsi:
        return
    holders = _name_index.setdefault(name, [])
    if mmsi not in holders:
        holders.append(mmsi)
```

Then add the ranking, after `candidates_for_name`'s dependencies:

```python
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
    """
    key = (name or "").strip().upper()
    if not key:
        return []
    in_scope = get_in_scope()
    with _cache_lock:
        entries = [_mmsi_index[m] for m in _name_index.get(key, []) if m in _mmsi_index]
    return sorted(entries, key=lambda e: _candidate_sort_key(e, in_scope))
```

- [ ] **Step 4: Point `_vessel_cache` at the best candidate**

`_vessel_cache[NAME]` must hold the top-ranked ship, not merely the last one seen. At the end of `record()`, replace both `_vessel_cache[...] = entry` assignments with a call to a new helper, and add it:

```python
def _refresh_name_view(name: str) -> None:
    """Point _vessel_cache at the best candidate for this name. Caller holds _cache_lock.

    _vessel_cache stays {NAME: entry} rather than becoming {NAME: [entry]}: twenty production
    call sites and a large number of test fixtures index it that way, and it holds references
    to the same dicts, so this is an ordering choice and not a second copy of the data.
    """
    key = (name or "").strip().upper()
    holders = _name_index.get(key, [])
    entries = [_mmsi_index[m] for m in holders if m in _mmsi_index]
    if not entries:
        return
    in_scope = set(_in_scope)
    _vessel_cache[key] = min(entries, key=lambda e: _candidate_sort_key(e, in_scope))
```

In `record()`, after each `_index_name(entry)` call, add `_refresh_name_view(entry.get("name", ""))`. In the admission branch the line `_vessel_cache[entry["name"].upper()] = entry` stays (it seeds the key), followed by `_index_name(entry)` then `_refresh_name_view(entry["name"])`.

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_whisper_proxy.py -q -k "name_index or candidates or in_scope_before"`
Expected: PASS (8 tests)

Run: `python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 6: Commit**

```bash
git add server/stt_proxy/ais.py server/tests/test_whisper_proxy.py
git commit -m "Keep every ship that shares a name, and rank them

A live snapshot of the Maas approach carries ALBATROS three times and the wider
box fourteen. Ranking is presence, then distance from Maas Center, then type
plausibility, then recency -- proximity above type because it still separates
candidates that are all equally live, which is the case that occurs.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Ambiguity detection in name matching

**Files:**
- Modify: `server/stt_proxy/ais.py` (`_best_name_match` ~537, `match_by_name` ~546)
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `ais.candidates_for_name`, `ais._candidate_sort_key` from Task 4
- Produces:
  - `ais.AIS_NAME_AMBIGUOUS_GAP: float` — env `AIS_NAME_AMBIGUOUS_GAP`, default `3.0`
  - `ais._scored_name_matches(query, keys, cutoff) -> list[tuple[str, float]]` — descending
  - `ais.match_by_name_candidates(extracted_name: str) -> list[dict]`
  - `ais.match_by_name` unchanged in signature and return type

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_whisper_proxy.py`:

```python
def test_a_dropped_token_yields_both_ships_rather_than_one(monkeypatch):
    # "Delta" scores 83.3 against both DELTA 3 and DELTA D. The old matcher returned
    # whichever came first in the list -- a confident identification decided by list order.
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "d3", "name": "DELTA 3", "latitude": 52.02,
                "longitude": 3.88, "type": 70}, source="test")
    ais.record({"mmsi": "dd", "name": "DELTA D", "latitude": 52.02,
                "longitude": 3.89, "type": 70}, source="test")
    ais.set_in_scope({"d3", "dd"})

    names = {c["name"] for c in ais.match_by_name_candidates("DELTA")}
    assert names == {"DELTA 3", "DELTA D"}


def test_a_clear_winner_yields_one_candidate(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "v", "name": "VOLGA MAERSK", "latitude": 52.0,
                "longitude": 3.9, "type": 70}, source="test")
    ais.record({"mmsi": "w", "name": "VAGA MAERSK", "latitude": 52.0,
                "longitude": 3.9, "type": 70}, source="test")
    ais.set_in_scope({"v", "w"})

    # 100.0 vs 87.0 -- a 13 point gap is not a close call.
    assert [c["name"] for c in ais.match_by_name_candidates("VOLGA MAERSK")] \
        == ["VOLGA MAERSK"]


def test_a_near_miss_within_the_gap_yields_both(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "v", "name": "VOLGA MAERSK", "latitude": 52.0,
                "longitude": 3.9, "type": 70}, source="test")
    ais.record({"mmsi": "w", "name": "VAGA MAERSK", "latitude": 52.0,
                "longitude": 3.9, "type": 70}, source="test")
    ais.set_in_scope({"v", "w"})

    # "VOGA MAERSK": 95.7 vs 90.9, a 4.8 point gap. Contested.
    monkeypatch.setattr(ais, "AIS_NAME_AMBIGUOUS_GAP", 5.0)
    names = {c["name"] for c in ais.match_by_name_candidates("VOGA MAERSK")}
    assert names == {"VOLGA MAERSK", "VAGA MAERSK"}


def test_two_ships_sharing_one_name_are_both_candidates(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "a", "name": "FORTUNA", "latitude": 52.02,
                "longitude": 3.88, "type": 70}, source="test")
    ais.record({"mmsi": "b", "name": "FORTUNA", "latitude": 52.05,
                "longitude": 3.90, "type": 70}, source="test")
    ais.set_in_scope({"a", "b"})

    assert {c["mmsi"] for c in ais.match_by_name_candidates("FORTUNA")} == {"a", "b"}


def test_match_by_name_still_returns_one_entry(monkeypatch):
    # The live path's contract is unchanged: one entry or None.
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "d3", "name": "DELTA 3", "latitude": 52.02,
                "longitude": 3.88, "type": 70}, source="test")
    ais.record({"mmsi": "dd", "name": "DELTA D", "latitude": 52.5,
                "longitude": 4.5, "type": 70}, source="test")
    ais.set_in_scope({"d3", "dd"})

    hit = ais.match_by_name("DELTA")
    assert isinstance(hit, dict)
    assert hit["mmsi"] == "d3"      # nearer Maas Center wins the tie


def test_match_by_name_candidates_is_empty_for_no_match(monkeypatch):
    _fresh_caches(monkeypatch)
    ais.record({"mmsi": "x", "name": "ORASUND"}, source="test")
    assert ais.match_by_name_candidates("ZZZZZZZZ") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_whisper_proxy.py -q -k "candidates or dropped_token or near_miss"`
Expected: FAIL — `AttributeError: module 'stt_proxy.ais' has no attribute 'match_by_name_candidates'`

- [ ] **Step 3: Add scored matching and the candidate lookup**

In `server/stt_proxy/ais.py`, add next to `AIS_NAME_MIN_SCORE`:

```python
# Two cache names within this many points of each other are a tie, not a winner and a loser.
# Measured: "Delta" scores 83.3 against both DELTA 3 and DELTA D, and one dropped letter puts
# VOLGA MAERSK and VAGA MAERSK 4.7 apart. 3.0 catches the exact ties and the tightest
# near-misses without flagging the ordinary 13-point gap of clean speech as contested.
AIS_NAME_AMBIGUOUS_GAP = float(os.environ.get("AIS_NAME_AMBIGUOUS_GAP", "3.0"))
```

Add above `_best_name_match`:

```python
def _scored_name_matches(query: str, keys: list[str], cutoff: int) -> list[tuple[str, float]]:
    """(name, score) for every cache name at or above `cutoff`, best first.

    _best_name_match keeps only the winner, which is what made a tie invisible: it used
    `score > best[1]`, so an exact draw was settled by list order and reported as an
    identification. This keeps the runners-up so the caller can see a close call.
    """
    hits = rf_process.extract(query, keys, scorer=rf_fuzz.ratio,
                              limit=None, score_cutoff=cutoff)
    kept = [(name, score) for name, score, _ in hits
            if len(name.replace(" ", "")) >= AIS_NAME_MIN_TOKEN or name == query]
    return sorted(kept, key=lambda pair: -pair[1])
```

Add after `match_by_name`:

```python
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
    keys, _ = _fresh_snapshot()
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
        for entry in candidates_for_name(name):
            mmsi = str(entry.get("mmsi") or "")
            if mmsi and mmsi in seen:
                continue
            seen.add(mmsi)
            out.append(entry)
    return sorted(out, key=lambda e: _candidate_sort_key(e, in_scope))
```

- [ ] **Step 4: Make `match_by_name` return the top candidate**

Replace the body of `match_by_name` with:

```python
def match_by_name(extracted_name: str) -> dict | None:
    """The single best vessel for a heard name, or None.

    Unchanged contract for the live path. It is now the head of the candidate ranking rather
    than the highest fuzzy score, so a tie is settled by presence and proximity instead of by
    list order.
    """
    candidates = match_by_name_candidates(extracted_name)
    return candidates[0] if candidates else None
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_whisper_proxy.py -q -k "candidates or dropped_token or near_miss or still_returns_one"`
Expected: PASS (6 tests)

Run: `python -m pytest tests/ -q`
Expected: all green. If a pre-existing name-matching test fails, read it before changing anything — the ranking now settles ties that the old code settled arbitrarily, so a test asserting the *arbitrary* outcome is asserting a bug. Confirm that is what it is, then update it and say so in the commit.

- [ ] **Step 6: Run the identification bench before and after**

```bash
git stash
python bench_identify.py --labels identification-labels.txt --resolve --repeats 3 > /tmp/bench-before.txt
git stash pop
python bench_identify.py --labels identification-labels.txt --resolve --repeats 3 > /tmp/bench-after.txt
diff /tmp/bench-before.txt /tmp/bench-after.txt
```

Baseline is 85.7% precision / 76.5% recall. A drop is a stop-and-investigate, not a "close enough" — record both numbers in the commit message.

- [ ] **Step 7: Commit**

```bash
git add server/stt_proxy/ais.py server/tests/test_whisper_proxy.py
git commit -m "Stop settling a tied vessel name by list order

_best_name_match kept only the top score with score > best[1], so 'Delta'
scoring 83.3 against both DELTA 3 and DELTA D returned whichever came first
and called it an identification. The same file already refuses to guess when a
callsign pattern fits several ships; names now get the same treatment.

bench_identify: <before> -> <after>

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

Replace `<before>` and `<after>` with the real figures from Step 6.

---

### Task 6: Candidates on the conversations page

**Files:**
- Modify: `server/stt_proxy/markup.py:24-38`
- Modify: `server/stt_proxy/conversations.py` (`resolve_conversation` output, `render_conversations_page` ~756)
- Test: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `ais.match_by_name_candidates` from Task 5, `markup._vessel_link`
- Produces:
  - `markup.VESSELFINDER_URL = "https://www.vesselfinder.com/vessels/details/{mmsi}"`
  - `conversations._format_candidates(row: dict) -> str` — HTML, `""` when not contested
  - stored exchange rows may carry `candidates: list[dict]` with keys `name, mmsi, type, km, destination, last_seen`

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_whisper_proxy.py`:

```python
def test_the_vesselfinder_link_points_at_the_ship_not_a_search():
    from stt_proxy import markup
    link = markup._vessel_link("ORASUND", "244123456")
    assert "vessels/details/244123456" in link
    assert "?name=" not in link


def test_a_contested_row_lists_its_candidates():
    from stt_proxy.conversations import render_conversations_page
    html = render_conversations_page([{
        "vessel": "DELTA 3", "mmsi": "d3", "confidence": "low",
        "start": "2026-08-12 10:00:00", "end": "2026-08-12 10:01:00",
        "channel": "01", "turns": [{"time": "10:00:00", "text": "Delta calling"}],
        "candidates": [
            {"name": "DELTA 3", "mmsi": "111", "type": "Tanker",
             "km": 4.2, "destination": "NLRTM", "last_seen": "2026-08-12 10:14:00"},
            {"name": "DELTA D", "mmsi": "222", "type": "General cargo",
             "km": 31.5, "destination": None, "last_seen": "2026-08-12 10:11:00"},
        ],
    }])
    assert "DELTA 3" in html and "DELTA D" in html
    assert "vessels/details/111" in html and "vessels/details/222" in html
    assert "4.2" in html and "31.5" in html


def test_an_uncontested_row_shows_no_candidate_block():
    from stt_proxy.conversations import render_conversations_page
    html = render_conversations_page([{
        "vessel": "ORASUND", "mmsi": "244123456", "confidence": "high",
        "start": "2026-08-12 10:00:00", "end": "2026-08-12 10:01:00",
        "channel": "01", "turns": [{"time": "10:00:00", "text": "Orasund"}],
    }])
    assert "candidates" not in html.lower()


def test_a_single_candidate_is_not_presented_as_a_choice():
    from stt_proxy.conversations import render_conversations_page
    html = render_conversations_page([{
        "vessel": "ORASUND", "mmsi": "111", "confidence": "high",
        "start": "2026-08-12 10:00:00", "end": "2026-08-12 10:01:00",
        "channel": "01", "turns": [{"time": "10:00:00", "text": "Orasund"}],
        "candidates": [{"name": "ORASUND", "mmsi": "111", "type": "Tanker",
                        "km": 4.2, "destination": "NLRTM",
                        "last_seen": "2026-08-12 10:14:00"}],
    }])
    assert "candidates" not in html.lower()


def test_candidate_names_are_escaped():
    from stt_proxy.conversations import render_conversations_page
    html = render_conversations_page([{
        "vessel": "X", "mmsi": "1", "confidence": "low",
        "start": "s", "end": "e", "channel": "01", "turns": [],
        "candidates": [
            {"name": "<script>alert(1)</script>", "mmsi": "1", "type": "Tanker",
             "km": 1.0, "destination": None, "last_seen": "t"},
            {"name": "OTHER", "mmsi": "2", "type": "Tanker",
             "km": 2.0, "destination": None, "last_seen": "t"},
        ],
    }])
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_rows_stored_before_candidates_existed_still_render():
    from stt_proxy.conversations import render_conversations_page
    html = render_conversations_page([{
        "vessel": "OLD ROW", "mmsi": "9", "confidence": "high",
        "start": "s", "end": "e", "channel": "01",
        "turns": [{"time": "10:00:00", "text": "hello"}],
    }])
    assert "OLD ROW" in html
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_whisper_proxy.py -q -k "vesselfinder_link or contested or candidate"`
Expected: FAIL — the link assertion fails on the search URL, and no candidate block is rendered.

- [ ] **Step 3: Point the link at the ship**

In `server/stt_proxy/markup.py`, replace lines 21-24:

```python
# Looked up by MMSI rather than by name, even though a name would read better: vessel names
# are not unique -- a live snapshot of the Maas approach carries ALBATROS three times -- and
# the ones here have been through STT. The MMSI is the thing the AIS match actually
# established, so it is what resolves to the right ship.
#
# The details path rather than the search path: this is used where the reader is choosing
# between candidates, and a search result page makes them choose twice.
VESSELFINDER_URL = "https://www.vesselfinder.com/vessels/details/{mmsi}"
```

- [ ] **Step 4: Render the candidate block**

Add to `server/stt_proxy/conversations.py`, next to `_format_particulars`:

```python
def _format_candidates(row: dict) -> str:
    """The candidate list for a contested identification, or "" when there is nothing to choose.

    Rendered only for two or more: a single candidate is an answer, and presenting it as a
    choice would train the reader to ignore the block that matters.

    This is a display, not a feedback loop -- clicking records nothing. Deliberate: a click
    that recorded "this was the right ship" is free labelled ground truth for the bench, but
    it needs a store, a schema and a correction path, none of which this needs.
    """
    candidates = row.get("candidates") or []
    if len(candidates) < 2:
        return ""

    items = []
    for c in candidates:
        bits = []
        if c.get("type"):
            bits.append(_html_escape(c["type"]))
        if c.get("km") is not None:
            bits.append(f"{float(c['km']):.1f} km from Maas Center")
        if c.get("destination"):
            bits.append(f"dest {_html_escape(c['destination'])}")
        if c.get("last_seen"):
            bits.append(f"seen {_html_escape(c['last_seen'])}")
        items.append(
            f'<li>{_vessel_link(c.get("name", "?"), c.get("mmsi"))} '
            f'<span class="cmeta">{" &middot; ".join(bits)}</span></li>')

    return (f'<div class="cands"><span class="clabel">{len(candidates)} candidates '
            f'&mdash; pick the one that fits what was said:</span>'
            f'<ul>{"".join(items)}</ul></div>')
```

Import `_vessel_link` is already present at `conversations.py:32`.

In `render_conversations_page`, after the `ais_line` assignment (~line 757), add:

```python
        cand_block = _format_candidates(row)
```

and insert `{cand_block}` into the block template immediately after `{ais_line}`:

```python
      </div>{ais_line}{cand_block}
```

Add to the page's `<style>` block (inside the existing stylesheet string):

```css
.cands{margin:.4em 0 .2em 0;padding:.4em .6em;border-left:3px solid #b58900;background:#fbf6e6}
.cands .clabel{font-size:.85em;color:#8a6d00}
.cands ul{margin:.3em 0 0 0;padding-left:1.2em}
.cands .cmeta{color:#666;font-size:.85em}
```

- [ ] **Step 5: Populate `candidates` when resolving**

In `conversations.py`, where an exchange's vessel is finalised in `resolve_conversation`, attach the candidate list. After the exchange dict has its `vessel` and `mmsi` set, add:

```python
        # Attached to the exchange so _store_resolved's `**ex` spread carries it through to
        # the page with no schema change. Rows stored before this existed simply lack the key.
        named = ex.get("vessel")
        if named:
            found = ais.match_by_name_candidates(named)
            if len(found) > 1:
                ex["candidates"] = [{
                    "name": c.get("name"),
                    "mmsi": c.get("mmsi"),
                    "type": ais._get_ship_type_name(c.get("type")),
                    "km": (ais._km_from_maas(c["latitude"], c["longitude"])
                           if c.get("latitude") is not None
                           and c.get("longitude") is not None else None),
                    "destination": c.get("destination"),
                    "last_seen": c.get("last_seen"),
                } for c in found]
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_whisper_proxy.py -q -k "vesselfinder_link or contested or candidate or old_row"`
Expected: PASS (6 tests)

Run: `python -m pytest tests/ -q`
Expected: all green.

- [ ] **Step 7: Commit**

```bash
git add server/stt_proxy/markup.py server/stt_proxy/conversations.py server/tests/test_whisper_proxy.py
git commit -m "Show the candidates when a name fits more than one ship

A contested identification now lists every plausible vessel with its type,
distance from Maas Center, destination and a VesselFinder link, so the reader
can apply what the exchange actually said -- a ship already inside the harbour
is not dropping anchor at Echo 3. The link moves from a search to the details
page, because choosing twice is not choosing.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Contract check against the live endpoint, and the cutover

**Files:**
- Create: `server/aishub_contract_check.py`
- Modify: `docs/user-manual.md`
- Test: manual

**Interfaces:**
- Consumes: everything above
- Produces: `server/aishub_contract_check.py` — a script, not a pytest test

- [ ] **Step 1: Write the contract check**

Create `server/aishub_contract_check.py`:

```python
"""Assert AISHub's response still has the shape stt_proxy/aishub.py assumes.

Run by hand, never in CI: it needs the credential and burns one of a rate-limited budget of
sixty requests an hour.

    python aishub_contract_check.py

This exists because of a failure this project has already had. The local-AIS work shipped with
a wrong assumption about transport shape and no test caught it, because -- as its design note
records -- "all fixtures were synthetic JSON in the assumed shape". Synthetic fixtures check
code against an assumption. Only a real call checks the assumption against the server.
"""

import os
import sys

from stt_proxy import aishub

REQUIRED = ["MMSI", "TIME", "LATITUDE", "LONGITUDE", "NAME", "CALLSIGN",
            "IMO", "TYPE", "A", "B", "C", "D", "DRAUGHT", "DEST",
            "COG", "SOG", "HEADING"]


def main() -> int:
    username = os.environ.get("AISHUB_USERNAME", "")
    if not username:
        print("AISHUB_USERNAME is not set", file=sys.stderr)
        return 2

    try:
        ships = aishub.parse_response(aishub._fetch(
            aishub.build_url(username, aishub.BBOX)))
    except aishub.AisHubError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print("If this says ERROR, wait 60s -- one request per minute.", file=sys.stderr)
        return 1

    if not ships:
        print("FAIL: no ships returned; cannot check the contract", file=sys.stderr)
        return 1

    print(f"{len(ships)} ships returned")

    sample = ships[0]
    missing = [f for f in REQUIRED if f not in sample]
    if missing:
        print(f"FAIL: fields absent from the response: {missing}", file=sys.stderr)
        return 1
    print(f"all {len(REQUIRED)} expected fields present")

    stamped = sum(1 for s in ships[:200] if aishub.parse_time(s.get("TIME", "")) is not None)
    if stamped < 190:
        print(f"FAIL: only {stamped}/200 TIME values parsed", file=sys.stderr)
        return 1
    print(f"TIME parsed on {stamped}/200 sampled ships")

    mapped = sum(1 for s in ships if aishub.map_ship(s) is not None)
    print(f"map_ship accepted {mapped}/{len(ships)}")

    named = [s for s in ships if (s.get("NAME") or "").strip()]
    seen: dict[str, int] = {}
    for s in named:
        key = s["NAME"].strip().upper()
        seen[key] = seen.get(key, 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    print(f"{len(named)} named, {len(dupes)} duplicate-name groups covering "
          f"{sum(dupes.values())} vessels")

    print("\nCONTRACT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run it**

From `server/`: `python aishub_contract_check.py`

Expected: `CONTRACT OK`, ~9,000 ships, ~700+ duplicate-name groups. If it reports missing fields, AISHub changed their response and `map_ship` needs updating before the cutover.

- [ ] **Step 3: Run the proxy end to end**

Start the proxy with `AIS_SOURCE=aishub` and `AISHUB_USERNAME` set. Confirm on the console:

```
AIS feed: AISHub, box (51.0, 53.2, 2.0, 6.0), every 900s
[AISHub] polling (51.0, 53.2, 2.0, 6.0) every 900s
[AISHub] 9xxx vessels
```

Then fetch `http://localhost:<port>/api/vessels` and confirm entries carry `last_seen` values matching AISHub report times rather than the moment the proxy started.

- [ ] **Step 4: Document the switch**

Add to `docs/user-manual.md`, in the configuration section:

```markdown
### AIS vessel source

`AIS_SOURCE` selects where vessel data comes from:

| value | meaning |
|---|---|
| `aishub` (default) | Poll AISHub every 15 minutes. Needs `AISHUB_USERNAME`. |
| `aisstream` | The original aisstream.io websocket. Needs `AISSTREAM_API_KEY`. Dead since 2026-08-05; kept because it was reliable for a long time and may return. |
| `off` | No vessel enrichment. |

`AISHUB_USERNAME` is the key from AISHub's welcome mail. **It goes in `server/start-all.bat`
alongside the other API keys — that file is gitignored. Never put it in a tracked file.** There
is no `.env` loader in this project; every setting is read straight from the environment.

Without `AISHUB_USERNAME` the proxy still starts and transcribes; it prints `AIS feed: disabled`
and runs without vessel enrichment.

Other settings: `AISHUB_BBOX` (`latmin,latmax,lonmin,lonmax`, default `51.0,53.2,2.0,6.0`) and
`AISHUB_POLL_SEC` (default 900; values under 60 are raised to 60, because AISHub answers a
faster caller with no data at all).

When a heard name fits more than one ship, `/conversations` lists the candidates with
VesselFinder links instead of choosing. Pick the one that fits what was said — a vessel already
inside the harbour is not dropping anchor at Echo 3.
```

- [ ] **Step 5: Commit**

```bash
git add server/aishub_contract_check.py docs/user-manual.md
git commit -m "Check AISHub's shape against AISHub, not against our assumption

Synthetic fixtures validate code against an assumption; the local-AIS work
shipped a wrong transport assumption that no synthetic fixture could catch.
This makes one real call and asserts the fields exist. Not in CI: it needs the
credential and burns a rate-limited request.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

- [ ] **Step 6: Merge**

```bash
python -m pytest tests/ -q          # from server/, must be fully green
git checkout master
git merge --no-ff feat/aishub-vessel-source
```

---

## Self-review

**Spec coverage:**

| spec requirement | task |
|---|---|
| AISHub primary, aisstream preserved live behind `AIS_SOURCE` | 3 |
| Cherry-pick `record()` core, leave `ais_local.py`/`pyais` | 1 |
| `last_seen` from AISHub `TIME` | 1 (`_apply`), 2 (`parse_time`), 3 (verified) |
| MMSI-keyed cache, name → candidates | 1, 4 |
| `_vessel_cache` stays `{NAME: entry}` as a view | 4 (`_refresh_name_view`) |
| Ranking: scope, proximity, type, recency | 4 |
| Out-of-box excluded from matching, not deleted | 3 (`set_in_scope`), 4 (sort key) |
| In-scope against last good poll, not wall clock | 3 |
| `ERROR: true` never read as an empty box | 2, 3 |
| 60 s floor enforced in code | 2 (`MIN_INTERVAL_SEC`), 3 (`_resolve_poll_sec`) |
| gzip | 3 (`_fetch`) |
| Wide bbox | 3 (`_resolve_bbox`) |
| Ambiguity surfaced, not resolved | 5, 6 |
| VesselFinder details URL | 6 |
| Candidates additive to stored rows | 6 |
| Synthetic fixtures + separate contract test | 2, 7 |
| Bench before/after | 5 |
| No new dependencies | all — stdlib `urllib`, `gzip`, `json` only |

Deferred items from the spec (spoken digits, click-to-confirm, `AIS_SILENCE_WARN_SEC` restore, `AIS_MAX_AGE_MIN` calibration) are intentionally absent.

**Placeholder scan:** the only intentional placeholders are `<before>`/`<after>` in Task 5's commit message, which Step 6 produces. `_index_name` is a deliberate no-op in Task 1 and is implemented in Task 4 — flagged in its docstring.

**Type consistency:** `record(fields, *, source, observed_at)` is used identically in Tasks 1, 2 and 3. `candidates_for_name` (exact name, Task 4) and `match_by_name_candidates` (fuzzy, Task 5) are distinct and both return `list[dict]`. `_candidate_sort_key(entry, in_scope)` takes the same two arguments in Tasks 4 and 5. `set_in_scope`/`get_in_scope` are defined in Task 3 and consumed in Task 4. `VESSELFINDER_URL` keeps its `{mmsi}` placeholder, so `_vessel_link` needs no change.
