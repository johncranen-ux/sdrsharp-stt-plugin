# Control Panel — Phase 3: Conversations, Vessels, Settings and the manual

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the panel the three screens it still lacks — Conversations, Vessels and Settings — reading the proxy's live state through a bounded, cached server-side layer, and rewrite the user manual so the panel is the documented way to run the station.

**Architecture:** The panel never lets a browser talk to the proxy directly. A new `webapp/proxy_data.py` fetches `/api/conversations` and `/api/ais-cache` server-side, holds them behind a short TTL, and serves the last good snapshot (flagged stale) when a fetch fails — so a stalled proxy degrades the screen instead of hanging it. Two thin query modules filter, sort and page over those snapshots so the browser is never sent 1.8 MB. Settings reuse the phase 1 `settings_schema` and `config_store` untouched; the API only adds masking and validation on top.

**Tech Stack:** FastAPI, pydantic, pytest + `TestClient`, and the same no-build browser JavaScript as phases 1–2 (no framework, no CDN, system fonts).

**Spec:** `docs/superpowers/specs/2026-08-18-control-panel-webapp-design.md` — Section 4 (the UI), Section 5 (data views), Section 6 (error handling), Section 7 (testing), Build order item 3.

## Global Constraints

- **Python 3.14**, Windows 11. `py -m pytest` from `server/`. The suite is **984 tests** at `d5d48ac` and must stay green.
- **Secrets never appear in an API response, a log line, or an error.** Six settings are `SettingType.SECRET`. This is Section 3 of the spec and is load-bearing.
- **Every mutating route is CSRF-protected and session-guarded**, and every new route gets a test asserting an unauthenticated request is rejected. That test is what keeps Section 3 true as routes are added.
- **`server/tests/conftest.py` must not be weakened.** It refuses to build a `Supervisor` over the real `server/logs`. On 2026-08-18 a test without that guard killed a live capture.
- **No browser-level UI tests** (spec Section 7, explicit). UI is verified by hand against the running panel.
- **No new runtime dependencies.** fastapi, uvicorn, psutil, argon2-cffi, httpx and pydantic are already in use.
- **Night-bridge UI language**: `--sea-*` ground, `--chart` text, `--dial` amber for live readings, red/green only where they carry their real meaning. System fonts only — the miniPC may have no internet.
- **Build DOM once and update in place.** Rebuilding on a poll destroys scroll position and text selections; this has already been fixed twice.
- **Commit after every task**, on a branch off `master`, message in the house style: an imperative sentence saying what changed and why, plus `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>`.

## Deviations from the spec, and why

1. **The Vessels tab does not read the separate identified-vessels log file.** Section 4 says "the identified-vessels log plus a searchable AIS cache". Every identification already travels in the conversation records, which this phase reads anyway, so the log would be a second reader over duplicate data. The Vessels tab therefore searches the AIS cache and drills into the conversations a vessel appears in. **Flag this at review** — if the log carries anything conversations do not, add a task.
2. **Task 1 is not in the spec at all.** `/api/ais-cache` reproducibly stalls ~19 s and resets before serving. The Vessels view reads it, so it is fixed first.

## File Structure

**Create**
- `server/webapp/proxy_data.py` — cached, bounded, server-side reader for the proxy's data endpoints. One responsibility: get a recent copy of a proxy collection, or say why not.
- `server/webapp/conversations_view.py` — filter, sort, page and project conversation records. Pure functions over a list; no I/O.
- `server/webapp/vessels_view.py` — search, sort, page and project AIS cache entries, and join a vessel to the conversations it appears in. Pure functions; no I/O.
- `server/webapp/settings_api.py` — schema + values for the form, masking on read, validation and change-reporting on write.
- `server/tests/test_proxy_data.py`, `test_conversations_view.py`, `test_vessels_view.py`, `test_settings_api.py`

**Modify**
- `server/stt_proxy/ais.py` — Task 1, the stall.
- `server/webapp/app.py` — five new routes.
- `server/webapp/static/index.html`, `app.css`, `app.js` — three new tabs.
- `server/tests/test_app_routes.py` — route and auth coverage for the new endpoints.
- `docs/user-manual.md` — Task 9.

---

### Task 1: Find out why the AIS cache read stalls for exactly 19 seconds

**Files:**
- Modify: `server/stt_proxy/ais.py`
- Test: `server/tests/test_ais_cache_read.py` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: `/api/ais-cache` answers within a bounded time whatever the feed thread is doing. No signature changes.

**Background — this is an INVESTIGATION task. The cause is not yet known, and the obvious hypothesis has already been measured and refuted. Do not skip to a fix.**

What is established, measured against the live proxy on 2026-08-19:

- `/api/ais-cache` returns 1.8 MB for 6046 vessels. Serialising that list takes **0.01 s** and a raw-socket read at a quiet moment delivered the whole body in **0.0 s**. Neither payload size nor JSON encoding is the cause.
- The proxy runs `ThreadingHTTPServer`, so one slow handler does not block others — and indeed `/api/conversations` answered normally throughout.
- Failures are **exactly ~18.97 s** every time (13 samples, 6 failures: 18.96, 18.96, 18.96, 18.97, 18.97, 18.98). Successes are **0.03 s**. There is no middle. A constant duration like that is a **fixed timeout somewhere**, not variable lock contention — contention would scatter.
- Failures arrive in **runs of three, separated by runs of four successes**. Two failure runs began ~127 s apart.
- **The AISHub poll is NOT the cause.** The probe recorded the proxy log's poll count beside every sample; it stayed at 11 across all 13 samples, successes and failures alike. `AIS_SAVE_INTERVAL` is 300 s and does not fit the ~127 s spacing either.

Raw measurement: `scratchpad/probe_cache.py` and `probe_cache.jsonl`.

- [ ] **Step 1: Find where the 19 seconds is spent, before theorising further**

Run the probe again with the proxy started under `py -X faulthandler`, and when a request is hanging, dump every thread's stack:

```python
# scratch: fire this from a second console while a request is stuck
import faulthandler, sys
faulthandler.dump_traceback(file=open("stacks.txt", "w"), all_threads=True)
```

Alternatively wrap the handler to log entry, post-lock, post-dumps and post-write timestamps, and read which span holds the 19 s. **The deliverable of this step is knowing which line blocks** — a stack, not a guess.

- [ ] **Step 2: Only once the blocking line is known, write the failing test and the fix**

The test must reproduce the mechanism found in Step 1, not the one guessed here. If — and only if — Step 1 shows a reader waiting on `_cache_lock`, the sketch in Steps 3-6 applies; if it shows something else (a socket write blocking, a timeout in an unrelated thread starving the GIL, a handler-level timeout), discard that sketch and design against what was measured. **Report the finding before implementing.**

- [ ] **Step 3: Write the failing test**

```python
# server/tests/test_ais_cache_read.py
"""A reader must never wait on the whole of a poll.

Measured 2026-08-19: two consecutive /api/ais-cache requests failed after ~19s while later
ones took 0.03s. A dashboard that hangs for 19 seconds whenever the feed happens to be
writing is not a dashboard.
"""
import threading
import time

from stt_proxy import ais


def test_a_snapshot_is_available_while_a_long_write_is_in_progress(monkeypatch):
    ais.reset_for_test()
    for i in range(200):
        ais.record({"mmsi": str(300000000 + i), "name": f"SHIP {i}"}, source="test")

    stop = threading.Event()

    def writer():
        # Simulates the poll: many records, back to back, as poll_once does.
        while not stop.is_set():
            for i in range(200):
                ais.record({"mmsi": str(300000000 + i), "name": f"SHIP {i}"}, source="test")

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    try:
        worst = 0.0
        for _ in range(20):
            started = time.time()
            entries = ais.snapshot()
            worst = max(worst, time.time() - started)
            assert len(entries) >= 200
    finally:
        stop.set()
        thread.join(timeout=5)

    assert worst < 1.0, f"a read waited {worst:.1f}s behind the writer"
```

- [ ] **Step 4: Run it and watch it fail**

Run: `py -m pytest tests/test_ais_cache_read.py -v`
Expected: FAIL — `ais.snapshot` and `ais.reset_for_test` do not exist yet.

- [ ] **Step 5: Add a bounded snapshot accessor — ONLY if Step 1 showed lock starvation**

The sketch below is a *candidate* fix for one specific cause: a reader competing for the write lock. It is written out because it is cheap and defensible on its own merits — a 1.8 MB read should never contend with the feed regardless — but it is **not yet known to fix the measured symptom**. If Step 1 found something else, this belongs in a separate piece of work, not here.

`ais` keeps a published copy that the writer swaps in, so a read is a single attribute fetch and never waits for a poll.

```python
# server/stt_proxy/ais.py

# The reader's copy. Rebound (never mutated) by _publish under the lock, so a reader takes
# the reference and is done -- it cannot be starved by a poll writing 1500 vessels, which is
# what made /api/ais-cache stall for ~19s at a time.
_published: tuple[dict, ...] = ()


def snapshot() -> list[dict]:
    """Every cached vessel, as of the last completed write. Never blocks on the feed."""
    return list(_published)


def _publish() -> None:
    """Republish the reader's copy. MUST be called with _cache_lock held."""
    global _published
    _published = tuple(_vessel_cache.values())
```

Call `_publish()` at the end of `record()` and `set_in_scope()`, inside the existing `_cache_lock` block. Add `reset_for_test()` clearing `_vessel_cache`, `_published` and the name index.

- [ ] **Step 6: Point the endpoint at it**

```python
# server/whisper-proxy.py, replacing the _cache_lock block in the /api/ais-cache handler
        if self.path == "/api/ais-cache":
            try:
                # snapshot() reads a published copy and never waits for the feed thread.
                data = json.dumps(ais.snapshot()).encode("utf-8")
```

- [ ] **Step 7: Run the tests**

Run: `py -m pytest tests/test_ais_cache_read.py tests/test_aishub.py -v`
Expected: PASS.

- [ ] **Step 8: Verify against the running proxy**

Restart the proxy from the panel, then run the probe for one full poll interval:

```bash
py -c "
import urllib.request, time
for i in range(30):
    t=time.time()
    with urllib.request.urlopen('http://127.0.0.1:9000/api/ais-cache', timeout=40) as r:
        n=len(r.read())
    print(f'{i}: {n} bytes in {time.time()-t:.2f}s'); time.sleep(10)"
```

Expected: every request under 1 s, including across an `[AISHub] N vessels` log line. Record the worst time in the commit message.

- [ ] **Step 9: Commit**

```bash
git add server/stt_proxy/ais.py server/whisper-proxy.py server/tests/test_ais_cache_read.py
git commit -m "Publish the vessel cache for readers, so a poll cannot stall the dashboard

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: A bounded, cached view of the proxy's data

**Files:**
- Create: `server/webapp/proxy_data.py`
- Test: `server/tests/test_proxy_data.py`

**Interfaces:**
- Consumes: `webapp.health.proxy_status`'s convention — loopback by address, `PROXY_PORT` from values.
- Produces:
  - `class Snapshot(BaseModel): fetched_at: float; age_sec: float; stale: bool; error: str | None; count: int`
  - `class ProxyData: __init__(self, load_values, fetch=None, clock=time.time)`
  - `ProxyData.conversations() -> tuple[list[dict], Snapshot]`
  - `ProxyData.vessels() -> tuple[list[dict], Snapshot]`
  - `CONVERSATIONS_TTL_SEC = 15`, `VESSELS_TTL_SEC = 60`, `FETCH_TIMEOUT_SEC = 6.0`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_proxy_data.py
"""Getting the proxy's collections without letting the proxy's problems become ours.

Two rules shape this: a browser must never be handed 1.8 MB, and a proxy that is slow or gone
must degrade the screen rather than hang it. The panel therefore holds the last good copy and
says how old it is.
"""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import proxy_data  # noqa: E402


class _Clock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now


def _data(answers, clock=None):
    """answers: dict of url-substring -> payload or Exception, consulted per call."""
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        for key, answer in answers.items():
            if key in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer(url) if callable(answer) else answer
        raise AssertionError(f"unexpected url {url}")

    data = proxy_data.ProxyData(lambda: {"PROXY_PORT": "9000"}, fetch=fetch,
                                clock=clock or _Clock())
    return data, calls


def test_a_first_call_fetches_and_reports_a_fresh_snapshot():
    data, calls = _data({"conversations": [{"vessel": "PASHA"}]})
    records, snap = data.conversations()
    assert records == [{"vessel": "PASHA"}]
    assert snap.stale is False and snap.error is None and snap.count == 1
    assert len(calls) == 1


def test_a_second_call_inside_the_ttl_does_not_hit_the_proxy_again():
    clock = _Clock()
    data, calls = _data({"conversations": [{"vessel": "PASHA"}]}, clock)
    data.conversations()
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC - 1
    data.conversations()
    assert len(calls) == 1


def test_the_ttl_expiring_refetches():
    clock = _Clock()
    data, calls = _data({"conversations": [{"vessel": "PASHA"}]}, clock)
    data.conversations()
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC + 1
    data.conversations()
    assert len(calls) == 2


def test_a_failed_fetch_serves_the_last_good_copy_marked_stale():
    """The screen keeps showing what it last knew, labelled. An empty table would read as
    'there are no conversations', which is a different and false claim."""
    clock = _Clock()
    answers = {"conversations": [{"vessel": "PASHA"}]}
    data, _ = _data(answers, clock)
    data.conversations()

    answers["conversations"] = ConnectionRefusedError("nobody home")
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC + 1
    records, snap = data.conversations()

    assert records == [{"vessel": "PASHA"}]
    assert snap.stale is True
    assert "ConnectionRefusedError" in snap.error
    assert snap.age_sec == pytest.approx(proxy_data.CONVERSATIONS_TTL_SEC + 1)


def test_a_failure_with_nothing_cached_yet_is_an_empty_result_that_says_why():
    data, _ = _data({"conversations": ConnectionRefusedError("nobody home")})
    records, snap = data.conversations()
    assert records == []
    assert snap.stale is True and snap.error is not None and snap.count == 0


def test_a_payload_that_is_not_a_list_is_refused_rather_than_rendered():
    data, _ = _data({"conversations": {"unexpected": "shape"}})
    records, snap = data.conversations()
    assert records == []
    assert "shape" in snap.error


def test_conversations_and_vessels_have_independent_caches():
    clock = _Clock()
    data, calls = _data({"conversations": [{"a": 1}], "ais-cache": [{"b": 2}]}, clock)
    data.conversations()
    data.vessels()
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC + 1     # conversations stale, vessels not
    data.conversations()
    data.vessels()
    assert sum("conversations" in c for c in calls) == 2
    assert sum("ais-cache" in c for c in calls) == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `py -m pytest tests/test_proxy_data.py -v`
Expected: FAIL — `No module named 'webapp.proxy_data'`.

- [ ] **Step 3: Implement**

```python
# server/webapp/proxy_data.py
"""Recent copies of the proxy's collections, fetched server-side.

Two things shape this module. The browser must never be handed the raw collections -- the AIS
cache alone is 1.8 MB for 6046 vessels -- so everything is fetched here and paged before it
leaves. And the proxy is a separate process that can be restarted, stalled or gone, so a fetch
failure must degrade a screen rather than hang it: the last good copy is kept and served with
its age, and a screen showing stale data says so.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable

from pydantic import BaseModel

CONVERSATIONS_TTL_SEC = 15
# The AIS cache changes only when the feed polls, which is every 900s by default. A minute of
# staleness costs nothing and a 1.8 MB fetch is not free.
VESSELS_TTL_SEC = 60
FETCH_TIMEOUT_SEC = 6.0


class Snapshot(BaseModel):
    fetched_at: float
    age_sec: float
    stale: bool
    error: str | None = None
    count: int = 0


def _fetch_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class _Cell:
    __slots__ = ("records", "fetched_at", "error", "lock")

    def __init__(self):
        self.records: list[dict] | None = None
        self.fetched_at = 0.0
        self.error: str | None = None
        self.lock = threading.Lock()


class ProxyData:
    def __init__(self, load_values: Callable[[], dict],
                 fetch: Callable[[str, float], object] | None = None,
                 clock: Callable[[], float] = time.time):
        self._load_values = load_values
        self._fetch = fetch or _fetch_json
        self._clock = clock
        self._cells = {"conversations": _Cell(), "ais-cache": _Cell()}

    def conversations(self):
        return self._get("conversations", CONVERSATIONS_TTL_SEC)

    def vessels(self):
        return self._get("ais-cache", VESSELS_TTL_SEC)

    def _url(self, path: str) -> str:
        port = (self._load_values().get("PROXY_PORT") or "9000").strip()
        return f"http://127.0.0.1:{port}/api/{path}"

    def _get(self, path: str, ttl: float) -> tuple[list[dict], Snapshot]:
        cell = self._cells[path]
        now = self._clock()
        # One fetch at a time per collection: without this, three browser tabs refreshing
        # together would pull 1.8 MB three times over.
        with cell.lock:
            if cell.records is None or now - cell.fetched_at >= ttl:
                self._refresh(cell, path)
            now = self._clock()
            records = cell.records or []
            return records, Snapshot(
                fetched_at=cell.fetched_at,
                age_sec=max(0.0, now - cell.fetched_at) if cell.records is not None else 0.0,
                stale=cell.error is not None,
                error=cell.error,
                count=len(records))

    def _refresh(self, cell: _Cell, path: str) -> None:
        try:
            payload = self._fetch(self._url(path), FETCH_TIMEOUT_SEC)
        except Exception as exc:
            # Keep whatever we had. An empty table would say "there is nothing", which is a
            # different claim from "we could not ask".
            cell.error = f"the proxy did not answer ({type(exc).__name__})"
            return
        if not isinstance(payload, list):
            cell.error = f"unexpected response shape: {type(payload).__name__}"
            return
        cell.records = payload
        cell.fetched_at = self._clock()
        cell.error = None
```

- [ ] **Step 4: Run the tests**

Run: `py -m pytest tests/test_proxy_data.py -v`
Expected: PASS, 7 tests.

- [ ] **Step 5: Commit**

```bash
git add server/webapp/proxy_data.py server/tests/test_proxy_data.py
git commit -m "Hold a recent copy of the proxy's collections, so a slow proxy cannot hang a screen

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Querying conversations

**Files:**
- Create: `server/webapp/conversations_view.py`
- Test: `server/tests/test_conversations_view.py`

**Interfaces:**
- Consumes: raw records from `ProxyData.conversations()`. A record carries `vessel, mmsi, callsign, type, via_callsign, evidence, confidence, imo, length, beam, draught, destination, latitude, longitude, sog, cog, heading, candidates, resolver_candidates, channel, start, end, turns`. A turn carries `time, text, raw, live_vessel, live_mmsi` and **optionally `conv`** — absent means the correction pass changed nothing OR failed, and the two are indistinguishable by design.
- Produces:
  - `conversation_id(record) -> str`
  - `summarise(record) -> dict`
  - `detail(record) -> dict`
  - `query(records, *, identified=None, channel=None, text=None, limit=50, offset=0) -> Page`
  - `class Page(BaseModel): rows: list[dict]; total: int; offset: int; limit: int`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_conversations_view.py
"""Turning 300 conversation records into something a phone can be sent.

The projection is deliberately lossy for the list and complete for one record: the list is
polled, the detail is opened once.
"""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import conversations_view as view  # noqa: E402


def _record(**over):
    record = {
        "vessel": "PASHA", "mmsi": "244123456", "callsign": "PBZL", "type": "tanker",
        "via_callsign": None, "evidence": "name heard in turn 1", "confidence": "high",
        "channel": "CH01", "start": "2026-08-19T10:00:00", "end": "2026-08-19T10:01:00",
        "destination": "NLRTM", "draught": 7.4, "latitude": 52.0, "longitude": 4.0,
        "resolver_candidates": [], "candidates": [],
        "turns": [{"time": "10:00:00", "text": "Pasha, Maas Approach", "raw": "Pasha, Mass Approach",
                   "live_vessel": "PASHA", "live_mmsi": "244123456"}],
    }
    record.update(over)
    return record


def test_a_summary_carries_what_the_list_shows_and_no_transcript():
    row = view.summarise(_record())
    assert row["vessel"] == "PASHA"
    assert row["turn_count"] == 1
    assert row["identified"] is True
    assert "turns" not in row and "resolver_candidates" not in row


def test_an_unidentified_row_is_marked_and_keeps_no_confidence():
    """Spec Section 5: "high confidence" on an unidentified row reads as a contradiction.
    The confidence describes the reasoning, not an identification that was not made."""
    row = view.summarise(_record(vessel=None, mmsi=None, confidence="high"))
    assert row["identified"] is False
    assert row["confidence"] is None


def test_the_id_is_stable_across_two_reads_of_the_same_record():
    assert view.conversation_id(_record()) == view.conversation_id(_record())


def test_two_conversations_on_different_channels_at_one_instant_get_different_ids():
    a = view.conversation_id(_record(channel="CH01"))
    b = view.conversation_id(_record(channel="CH16"))
    assert a != b


def test_detail_exposes_the_three_layer_text_chain():
    """raw -> text -> conv: what the regex pass and the LLM pass each changed. Only visible
    through the API until now."""
    turn = {"time": "10:00", "raw": "Mass Aproach", "text": "Maas Approach",
            "conv": "Maas Approach, over", "live_vessel": None, "live_mmsi": None}
    chain = view.detail(_record(turns=[turn]))["turns"][0]
    assert chain["raw"] == "Mass Aproach"
    assert chain["text"] == "Maas Approach"
    assert chain["conv"] == "Maas Approach, over"
    assert chain["changed_by_regex"] is True
    assert chain["changed_by_llm"] is True


def test_a_turn_the_correction_pass_left_alone_says_so_rather_than_inventing_a_layer():
    turn = {"time": "10:00", "raw": "Maas Approach", "text": "Maas Approach",
            "live_vessel": None, "live_mmsi": None}
    chain = view.detail(_record(turns=[turn]))["turns"][0]
    assert chain["conv"] is None
    assert chain["changed_by_regex"] is False
    assert chain["changed_by_llm"] is False


def test_a_heard_name_with_no_ais_match_is_distinguished_from_a_confirmed_one():
    """live_vessel set with live_mmsi null means the name was heard and AIS had no such ship."""
    turns = [{"time": "10:00", "raw": "x", "text": "x", "live_vessel": "GHOST", "live_mmsi": None},
             {"time": "10:01", "raw": "y", "text": "y", "live_vessel": "PASHA",
              "live_mmsi": "244123456"}]
    out = view.detail(_record(turns=turns))["turns"]
    assert out[0]["live_match"] == "heard-only"
    assert out[1]["live_match"] == "ais-confirmed"


def test_filtering_by_identified_and_by_channel():
    records = [_record(), _record(vessel=None, mmsi=None), _record(channel="CH16")]
    assert view.query(records, identified=True).total == 2
    assert view.query(records, identified=False).total == 1
    assert view.query(records, channel="CH16").total == 1


def test_free_text_search_covers_the_transcript_and_the_vessel():
    records = [_record(), _record(vessel="CONDOR", turns=[
        {"time": "1", "text": "buoy one six", "raw": "buoy 16",
         "live_vessel": None, "live_mmsi": None}])]
    assert view.query(records, text="condor").total == 1
    assert view.query(records, text="BUOY").total == 1      # case-insensitive, transcript too
    assert view.query(records, text="nothing here").total == 0


def test_rows_are_newest_first_and_paged():
    records = [_record(start=f"2026-08-19T10:{n:02d}:00") for n in range(5)]
    page = view.query(records, limit=2, offset=0)
    assert [r["start"] for r in page.rows] == ["2026-08-19T10:04:00", "2026-08-19T10:03:00"]
    assert page.total == 5
    assert view.query(records, limit=2, offset=4).rows[0]["start"] == "2026-08-19T10:00:00"


def test_a_shared_name_is_reported_with_its_mmsi_rather_than_by_name_alone():
    """Spec Section 5: seven labelled conversations were distorted by a name collision."""
    row = view.summarise(_record(vessel="SEA STAR", mmsi="311000111"))
    assert row["label"] == "SEA STAR (311000111)"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `py -m pytest tests/test_conversations_view.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# server/webapp/conversations_view.py
"""Filtering, paging and projecting conversation records. Pure functions, no I/O.

The list projection drops transcripts and candidate lists on purpose: the list is polled every
few seconds and the store is 613 KB. Detail is complete, and is fetched once when a row is
opened.
"""
from __future__ import annotations

from pydantic import BaseModel


class Page(BaseModel):
    rows: list[dict]
    total: int
    offset: int
    limit: int


def conversation_id(record: dict) -> str:
    """Stable within one proxy run. Start instant plus channel: the store has no id of its own,
    and index position shifts as conversations are added."""
    return f"{record.get('start') or ''}|{record.get('channel') or ''}"


def _identified(record: dict) -> bool:
    return bool(record.get("mmsi") or record.get("vessel"))


def summarise(record: dict) -> dict:
    identified = _identified(record)
    vessel, mmsi = record.get("vessel"), record.get("mmsi")
    return {
        "id": conversation_id(record),
        "start": record.get("start"),
        "end": record.get("end"),
        "channel": record.get("channel"),
        "vessel": vessel,
        "mmsi": mmsi,
        # Never the bare name: where two cached vessels share one, the name is not an
        # identification and reading it as one distorted seven labelled conversations.
        "label": f"{vessel} ({mmsi})" if identified and vessel and mmsi
                 else (vessel or mmsi or "unidentified"),
        "type": record.get("type"),
        "destination": record.get("destination"),
        "identified": identified,
        # Dropped on unidentified rows: the confidence describes the reasoning, and printed
        # beside "unidentified" it reads as a contradiction.
        "confidence": record.get("confidence") if identified else None,
        "turn_count": len(record.get("turns") or []),
        "candidate_count": len(record.get("resolver_candidates") or []),
    }


def _turn(turn: dict) -> dict:
    raw, text = turn.get("raw"), turn.get("text")
    conv = turn.get("conv")
    live_vessel, live_mmsi = turn.get("live_vessel"), turn.get("live_mmsi")
    return {
        "time": turn.get("time"),
        "raw": raw,
        "text": text,
        # Absent means the correction pass changed nothing OR failed; the store cannot tell
        # them apart, so this says only what is known.
        "conv": conv,
        "changed_by_regex": bool(raw is not None and text is not None and raw != text),
        "changed_by_llm": bool(conv is not None and conv != text),
        "live_vessel": live_vessel,
        "live_mmsi": live_mmsi,
        "live_match": None if not live_vessel
                      else ("ais-confirmed" if live_mmsi else "heard-only"),
    }


def detail(record: dict) -> dict:
    out = dict(record)
    out["id"] = conversation_id(record)
    out["identified"] = _identified(record)
    out["turns"] = [_turn(t) for t in (record.get("turns") or [])]
    if not out["identified"]:
        out["confidence"] = None
    return out


def _haystack(record: dict) -> str:
    parts = [str(record.get(k) or "") for k in ("vessel", "mmsi", "callsign", "destination",
                                                "channel", "evidence")]
    for turn in record.get("turns") or []:
        parts += [str(turn.get(k) or "") for k in ("raw", "text", "conv")]
    return " ".join(parts).lower()


def query(records: list[dict], *, identified: bool | None = None, channel: str | None = None,
          text: str | None = None, limit: int = 50, offset: int = 0) -> Page:
    found = list(records)
    if identified is not None:
        found = [r for r in found if _identified(r) is identified]
    if channel:
        found = [r for r in found if (r.get("channel") or "") == channel]
    if text:
        needle = text.strip().lower()
        found = [r for r in found if needle in _haystack(r)]

    found.sort(key=lambda r: str(r.get("start") or ""), reverse=True)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return Page(rows=[summarise(r) for r in found[offset:offset + limit]],
                total=len(found), offset=offset, limit=limit)
```

- [ ] **Step 4: Run the tests**

Run: `py -m pytest tests/test_conversations_view.py -v`
Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add server/webapp/conversations_view.py server/tests/test_conversations_view.py
git commit -m "Project conversations into rows a phone can hold, and expose the text chain

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Querying vessels

**Files:**
- Create: `server/webapp/vessels_view.py`
- Test: `server/tests/test_vessels_view.py`

**Interfaces:**
- Consumes: AIS cache entries from `ProxyData.vessels()`, keys `mmsi, name, callsign, type, imo, length, beam, draught, destination, latitude, longitude, sog, cog, heading, position_at, source, last_seen`. Conversation records for the drill-down.
- Produces:
  - `search(entries, *, text=None, limit=50, offset=0) -> Page` (reuses `conversations_view.Page`)
  - `detail(entries, mmsi) -> dict | None`
  - `conversations_for(records, mmsi) -> list[dict]`
  - `duplicate_names(entries) -> dict[str, list[str]]`

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_vessels_view.py
"""Searching the AIS cache.

"Is this vessel in the cache and when was it last seen" came up repeatedly through August and
currently takes a Python one-liner. That is the whole reason this screen exists.
"""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import vessels_view as view  # noqa: E402


def _vessel(**over):
    entry = {"mmsi": "244123456", "name": "PASHA", "callsign": "PBZL", "type": "tanker",
             "destination": "NLRTM", "draught": 7.4, "latitude": 52.0, "longitude": 4.0,
             "last_seen": "2026-08-19T10:00:00+00:00", "source": "aishub"}
    entry.update(over)
    return entry


def test_search_matches_name_mmsi_and_callsign_case_insensitively():
    entries = [_vessel(), _vessel(mmsi="311000111", name="CONDOR", callsign="PA2864")]
    assert view.search(entries, text="pasha").total == 1
    assert view.search(entries, text="311000").total == 1
    assert view.search(entries, text="pa2864").total == 1


def test_an_empty_search_returns_everything_newest_first():
    entries = [_vessel(mmsi="1", last_seen="2026-08-19T09:00:00+00:00"),
               _vessel(mmsi="2", last_seen="2026-08-19T11:00:00+00:00")]
    page = view.search(entries)
    assert [r["mmsi"] for r in page.rows] == ["2", "1"]


def test_a_vessel_with_no_last_seen_sorts_last_rather_than_crashing():
    entries = [_vessel(mmsi="1", last_seen=None),
               _vessel(mmsi="2", last_seen="2026-08-19T11:00:00+00:00")]
    assert [r["mmsi"] for r in view.search(entries).rows] == ["2", "1"]


def test_detail_returns_the_whole_entry_for_one_mmsi():
    entries = [_vessel(), _vessel(mmsi="311000111", name="CONDOR")]
    assert view.detail(entries, "311000111")["name"] == "CONDOR"
    assert view.detail(entries, "999") is None


def test_a_name_shared_by_two_mmsis_is_reported_as_shared():
    """A shared name is not an identification. The Vessels screen must show which names
    cannot be trusted on their own."""
    entries = [_vessel(mmsi="1", name="SEA STAR"), _vessel(mmsi="2", name="SEA STAR"),
               _vessel(mmsi="3", name="CONDOR")]
    shared = view.duplicate_names(entries)
    assert shared == {"SEA STAR": ["1", "2"]}
    assert view.search(entries, text="sea star").rows[0]["name_shared"] is True
    assert view.search(entries, text="condor").rows[0]["name_shared"] is False


def test_conversations_for_a_vessel_are_found_by_mmsi_not_by_name():
    records = [{"mmsi": "244123456", "vessel": "PASHA", "start": "2026-08-19T10:00:00",
                "channel": "CH01", "turns": []},
               {"mmsi": "311000111", "vessel": "CONDOR", "start": "2026-08-19T09:00:00",
                "channel": "CH01", "turns": []}]
    found = view.conversations_for(records, "244123456")
    assert len(found) == 1 and found[0]["vessel"] == "PASHA"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `py -m pytest tests/test_vessels_view.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# server/webapp/vessels_view.py
"""Searching and projecting the AIS cache. Pure functions, no I/O."""
from __future__ import annotations

from collections import defaultdict

from webapp.conversations_view import Page, summarise

_FIELDS = ("mmsi", "name", "callsign", "destination")


def duplicate_names(entries: list[dict]) -> dict[str, list[str]]:
    """Names carried by more than one MMSI. A shared name is not an identification."""
    by_name: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        name = (entry.get("name") or "").strip().upper()
        if name:
            by_name[name].append(str(entry.get("mmsi") or ""))
    return {name: mmsis for name, mmsis in by_name.items() if len(mmsis) > 1}


def _row(entry: dict, shared: dict[str, list[str]]) -> dict:
    name = (entry.get("name") or "").strip().upper()
    return {
        "mmsi": str(entry.get("mmsi") or ""),
        "name": entry.get("name"),
        "callsign": entry.get("callsign"),
        "type": entry.get("type"),
        "destination": entry.get("destination"),
        "draught": entry.get("draught"),
        "last_seen": entry.get("last_seen"),
        "source": entry.get("source"),
        "name_shared": name in shared,
    }


def search(entries: list[dict], *, text: str | None = None,
           limit: int = 50, offset: int = 0) -> Page:
    found = list(entries)
    if text:
        needle = text.strip().lower()
        found = [e for e in found
                 if any(needle in str(e.get(f) or "").lower() for f in _FIELDS)]
    # "" sorts before any timestamp, so a vessel never heard sorts last under reverse.
    found.sort(key=lambda e: str(e.get("last_seen") or ""), reverse=True)

    shared = duplicate_names(entries)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return Page(rows=[_row(e, shared) for e in found[offset:offset + limit]],
                total=len(found), offset=offset, limit=limit)


def detail(entries: list[dict], mmsi: str) -> dict | None:
    for entry in entries:
        if str(entry.get("mmsi") or "") == str(mmsi):
            return dict(entry)
    return None


def conversations_for(records: list[dict], mmsi: str) -> list[dict]:
    """By MMSI, never by name -- the name is exactly what cannot be trusted here."""
    found = [r for r in records if str(r.get("mmsi") or "") == str(mmsi)]
    found.sort(key=lambda r: str(r.get("start") or ""), reverse=True)
    return [summarise(r) for r in found]
```

- [ ] **Step 4: Run the tests**

Run: `py -m pytest tests/test_vessels_view.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add server/webapp/vessels_view.py server/tests/test_vessels_view.py
git commit -m "Make the AIS cache searchable, and mark the names two ships share

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: The data routes

**Files:**
- Modify: `server/webapp/app.py`
- Test: `server/tests/test_app_routes.py`

**Interfaces:**
- Consumes: `ProxyData`, `conversations_view`, `vessels_view`.
- Produces: `GET /api/conversations`, `GET /api/conversations/{id}`, `GET /api/vessels`, `GET /api/vessels/{mmsi}` — all on the `guarded` router. Every response carries a `snapshot` object so the browser can label stale data.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_app_routes.py

def test_the_conversations_route_pages_and_reports_its_snapshot(client):
    body = client.get("/api/conversations", params={"limit": 1}).json()
    assert body["limit"] == 1
    assert "stale" in body["snapshot"] and "age_sec" in body["snapshot"]


def test_the_conversations_route_never_ships_transcripts_in_the_list(client):
    """The list is polled; 613 KB per poll over Tailscale is not acceptable."""
    for row in client.get("/api/conversations").json()["rows"]:
        assert "turns" not in row


def test_an_unknown_conversation_id_is_a_404_not_an_empty_object(client):
    assert client.get("/api/conversations/nope").status_code == 404


def test_the_vessels_route_searches(client):
    body = client.get("/api/vessels", params={"text": "pasha"}).json()
    assert "rows" in body and "total" in body


def test_an_unknown_mmsi_is_a_404(client):
    assert client.get("/api/vessels/999999999").status_code == 404


@pytest.mark.parametrize("path", ["/api/conversations", "/api/conversations/x",
                                  "/api/vessels", "/api/vessels/1"])
def test_the_data_routes_reject_an_unauthenticated_request(unauthenticated_client, path):
    assert unauthenticated_client.get(path).status_code == 401
```

Note: the fixture must supply a `ProxyData` whose `fetch` returns fixed records — the tests must not reach a real proxy. Add to the fixture module:

```python
def _fake_proxy_data(conversations, vessels):
    from webapp.proxy_data import ProxyData

    def fetch(url, timeout):
        return conversations if "conversations" in url else vessels
    return ProxyData(lambda: {"PROXY_PORT": "9000"}, fetch=fetch)
```

and pass it into `create_app(..., proxy_data=...)`.

- [ ] **Step 2: Run them and watch them fail**

Run: `py -m pytest tests/test_app_routes.py -v`
Expected: FAIL — routes missing, `create_app` has no `proxy_data` parameter.

- [ ] **Step 3: Implement**

```python
# server/webapp/app.py -- inside create_app, alongside the existing guarded routes

    data = proxy_data or proxy_data_module.ProxyData(values)

    def _envelope(page, snap):
        body = page.model_dump()
        body["snapshot"] = snap.model_dump()
        return body

    @guarded.get("/api/conversations")
    def read_conversations(identified: bool | None = None, channel: str | None = None,
                           text: str | None = None, limit: int = 50, offset: int = 0):
        records, snap = data.conversations()
        return _envelope(conversations_view.query(
            records, identified=identified, channel=channel, text=text,
            limit=limit, offset=offset), snap)

    @guarded.get("/api/conversations/{conversation_id:path}")
    def read_conversation(conversation_id: str):
        records, _ = data.conversations()
        for record in records:
            if conversations_view.conversation_id(record) == conversation_id:
                return conversations_view.detail(record)
        raise HTTPException(status_code=404, detail="no such conversation")

    @guarded.get("/api/vessels")
    def read_vessels(text: str | None = None, limit: int = 50, offset: int = 0):
        entries, snap = data.vessels()
        return _envelope(vessels_view.search(entries, text=text, limit=limit, offset=offset),
                         snap)

    @guarded.get("/api/vessels/{mmsi}")
    def read_vessel(mmsi: str):
        entries, _ = data.vessels()
        entry = vessels_view.detail(entries, mmsi)
        if entry is None:
            raise HTTPException(status_code=404, detail="no such vessel in the cache")
        records, _ = data.conversations()
        entry["conversations"] = vessels_view.conversations_for(records, mmsi)
        return entry
```

`conversation_id` contains a `|` and a `:` from the ISO timestamp, so the path converter is `:path` and the browser must `encodeURIComponent` it.

- [ ] **Step 4: Run the tests**

Run: `py -m pytest tests/test_app_routes.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/webapp/app.py server/tests/test_app_routes.py
git commit -m "Serve conversations and vessels through the panel, paged and snapshot-labelled

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: The Conversations screen

**Files:**
- Modify: `server/webapp/static/index.html`, `app.css`, `app.js`

**Interfaces:**
- Consumes: `GET /api/conversations`, `GET /api/conversations/{id}`.
- Produces: a `conversations` tab; no exports.

- [ ] **Step 1: Add the tab and its markup**

A filter bar (identified / unidentified / all, channel, free text), a table of rows, and a detail panel that opens beside or below the selected row. Reuse `.legend`, `.card`, `.button` and the mono/tabular-numeral treatment. Add `<button class="tab" data-tab="conversations">` to the existing nav and a `<main id="conversations" class="view" hidden>`.

- [ ] **Step 2: Render the list, built once and updated in place**

Rows keyed by `id` in a `Map`, exactly as `cardViews` and `feedViews` already are. A poll updates text; it never calls `replaceChildren` on the table.

- [ ] **Step 3: Show staleness rather than hiding it**

When `snapshot.stale` is true, show a line above the table: `showing the last copy, {elapsed(age_sec)} old — {error}`. Never render an empty table on a failed fetch: "there are no conversations" and "we could not ask" are different claims.

- [ ] **Step 4: The detail view**

On selecting a row, fetch the detail and show, per turn: the time, the three-layer chain `raw → text → conv` with the layers that changed marked, and the live-match state (`heard-only` vs `ais-confirmed`) rendered as words, not as a colour alone. Below the turns, the resolver candidate list with position, draught, destination, age and which pass supplied it, and — where present — the sub-cutoff shortlist under the heading **"scored below the identification cutoff"**. Without that framing a suggestion reads as an identification.

- [ ] **Step 5: Verify by hand against the running panel**

Check: an identified row and an unidentified one; a conversation whose correction pass changed a turn; filters combining; paging past 50; and the stale banner (stop the proxy and confirm the table keeps its rows and gains the banner).

- [ ] **Step 6: Commit**

```bash
git add server/webapp/static
git commit -m "Add the Conversations screen, showing what each pass changed and what was offered

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: The Vessels screen

**Files:**
- Modify: `server/webapp/static/index.html`, `app.css`, `app.js`

- [ ] **Step 1: Search box and result table**

Columns: name, MMSI, callsign, type, destination, draught, last seen. A name shared by two MMSIs is marked in the row — the mark is the point of the screen as much as the search is.

- [ ] **Step 2: Debounce the search by 250 ms**

Each keystroke is a request against 6046 entries. Debounce, and drop an answer whose query is no longer current — the same generation-tagging already used by `logStream`.

- [ ] **Step 3: Vessel detail**

Full cached entry plus the conversations that vessel appears in, found by MMSI. Each links into the Conversations screen.

- [ ] **Step 4: Verify by hand**

Search by name, by partial MMSI, by callsign. Confirm a shared name is marked. Confirm a vessel with no conversations says so rather than showing an empty area.

- [ ] **Step 5: Commit**

```bash
git add server/webapp/static
git commit -m "Add the Vessels screen, answering 'is it cached and when was it last seen'

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Settings — API and screen

**Files:**
- Create: `server/webapp/settings_api.py`, `server/tests/test_settings_api.py`
- Modify: `server/webapp/app.py`, `server/webapp/static/*`

**Interfaces:**
- Consumes: `settings_schema.SETTINGS` (39 settings, 9 groups, 6 `SECRET`), `config_store.load/save`.
- Produces: `form(values) -> dict`, `apply(values, submitted) -> Applied`, `GET /api/settings`, `POST /api/settings`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_settings_api.py
"""The settings form, and the one rule that matters: a secret leaves this process never."""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store, settings_api  # noqa: E402
from webapp.settings_schema import SETTINGS, SettingType  # noqa: E402

_SECRETS = [s.key for s in SETTINGS if s.type is SettingType.SECRET]


def _values(**over):
    values = config_store.load(Path("does-not-exist.json"))
    values.update(over)
    return values


def test_no_secret_value_appears_anywhere_in_the_form():
    values = _values(**{key: "super-secret-value" for key in _SECRETS})
    body = settings_api.form(values)
    assert "super-secret-value" not in str(body)


def test_a_secret_reports_only_whether_it_is_set():
    key = _SECRETS[0]
    assert settings_api.form(_values(**{key: "x"}))["fields"][key]["set"] is True
    assert settings_api.form(_values(**{key: ""}))["fields"][key]["set"] is False


def test_the_form_is_grouped_in_schema_order():
    groups = [g["name"] for g in settings_api.form(_values())["groups"]]
    assert groups == list(dict.fromkeys(s.group for s in SETTINGS))


def test_submitting_an_empty_secret_leaves_the_stored_one_alone():
    """The form cannot show a secret, so an empty box means "unchanged", not "clear it"."""
    key = _SECRETS[0]
    applied = settings_api.apply(_values(**{key: "kept"}), {key: ""})
    assert applied.values[key] == "kept"
    assert key not in applied.changed


def test_a_secret_can_be_cleared_explicitly():
    key = _SECRETS[0]
    applied = settings_api.apply(_values(**{key: "kept"}), {key: settings_api.CLEAR})
    assert applied.values[key] == ""
    assert key in applied.changed


def test_an_invalid_value_is_rejected_with_the_key_named():
    port = next(s.key for s in SETTINGS if s.type is SettingType.INT)
    with pytest.raises(settings_api.Invalid) as caught:
        settings_api.apply(_values(), {port: "not a number"})
    assert port in str(caught.value)


def test_an_unknown_key_is_refused_rather_than_stored():
    with pytest.raises(settings_api.Invalid):
        settings_api.apply(_values(), {"NOT_A_SETTING": "x"})


def test_the_response_says_which_processes_must_be_restarted():
    """A setting the proxy reads at startup is not live until it restarts, and a form that
    does not say so silently lies about what is in effect."""
    exported = next(s.key for s in SETTINGS if s.exported and s.type is SettingType.BOOL)
    applied = settings_api.apply(_values(), {exported: "off"})
    assert "proxy" in applied.restart_needed
```

- [ ] **Step 2: Run and watch them fail**

Run: `py -m pytest tests/test_settings_api.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement `settings_api.py`**

`CLEAR` is a sentinel string (`"__CLEAR__"`). `apply` validates through the existing `settings_schema` coercion, returns `Applied(values, changed, restart_needed)`, and raises `Invalid(key, message)` naming the key. `restart_needed` maps a changed exported key to the processes whose env carries it — `PROXY_*`/STT/AIS keys to `proxy`, `AIS_STATION_*` to `counter`, `WEBAPP_*` to the panel itself.

- [ ] **Step 4: Add the routes**

`GET /api/settings` on `guarded`; `POST /api/settings` on `mutating` (CSRF), writing through `config_store.save` — which is already atomic and permission-restricted — and returning `changed` plus `restart_needed`. Add the unauthenticated-rejection test for both.

- [ ] **Step 5: Build the screen**

Grouped form in schema order. Secrets render as an empty password field with a "set" / "not set" marker beside it and a "Clear" control that submits the sentinel. Booleans as checkboxes, enums as selects, paths as text with the resolve mark the Dashboard already computes. A save reports what changed and, if anything needs it, an explicit "restart the proxy for this to take effect" line with the restart control right there.

- [ ] **Step 6: Verify by hand**

Change a non-secret and confirm `config.json` updates and the panel reports the restart need. Confirm a secret's value never appears in the page source, in a response body, or in the log. Confirm an invalid port is rejected with a message naming the field and nothing is written.

- [ ] **Step 7: Commit**

```bash
git add server/webapp/settings_api.py server/tests/test_settings_api.py server/webapp/app.py server/webapp/static
git commit -m "Add the Settings screen, with secrets that can be set but never read back

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 9: Rewrite the user manual around the panel

**Files:**
- Modify: `docs/user-manual.md`

The manual currently presents `start-all.bat` as the only way to run the station (lines 176–211) and never mentions the panel or port 8787. The panel is how the station is actually run, and after the miniPC move it will be the only practical way.

- [ ] **Step 1: Rewrite "Run" as "Starting the station"**

Panel first: `cd server && py -m webapp`, port **8787**, `http://127.0.0.1:8787`. Setting the password with `py -m webapp.set_password` and why the panel refuses to bind wide without one. Starting the proxy and counter from the Dashboard. Then a clearly-labelled subsection **"Running without the panel"** keeping `start-all.bat` for a headless or scripted start.

- [ ] **Step 2: Add "Reading the Dashboard"**

The watch (time since the last transmission, and why it distinguishes "SDR# receiving" from "SDR# open with play unpressed"), the feed lamps and what each colour means including the lamp test, the process cards, and the Logs popup.

- [ ] **Step 3: Add "Conversations and Vessels"**

What the three-layer text chain shows, what the candidate list is for, why a sub-cutoff suggestion is not an identification, and how to search the cache for a vessel.

- [ ] **Step 4: Add "Settings"**

That `config.json` is now the source of truth, that it was imported from `start-all.bat`, that secrets can be set but never read back, and which changes need a restart. Keep the existing settings reference — the key names are unchanged.

- [ ] **Step 5: Update Troubleshooting**

Add: the panel will not bind to a non-loopback address without a password; a port held by another process and what the panel says; a stale browser tab after an upgrade (hard reload — `/static` sends `no-cache` but an old tab keeps its script); and a feed lamp red versus its process stopped.

- [ ] **Step 6: Check every command in the document actually runs**

Walk the manual top to bottom on this machine and run each command as written. This is the step that catches a manual describing a flag that was renamed.

- [ ] **Step 7: Commit**

```bash
git add docs/user-manual.md
git commit -m "Document the panel as the way to run the station, with the batch file as fallback

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Done when

- `py -m pytest` green from `server/`, ≥ 984 tests plus the new ones.
- Conversations, Vessels and Settings all reachable and usable from a phone-width window.
- Stopping the proxy leaves every data screen showing its last copy with a stale banner, never an empty table and never a hang.
- No secret value appears in any response body, page source, or log.
- `/api/ais-cache` stays under 1 s across an AISHub poll.
- The manual's commands have each been run as written.
