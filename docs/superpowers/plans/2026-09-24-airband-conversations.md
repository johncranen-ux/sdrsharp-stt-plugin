# Airband Conversations Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Group Schiphol Approach 4 transmissions per flight in a new "Airband" tab of the control panel, using the callsign tag plus an autopilot-change clue. The autopilot clue is recorded in shadow mode until it passes a measurement gate.

**Architecture:** The proxy stores every Approach 4 transmission in new tables in the existing SQLite archive (`conversations.db`). Stage 1 stores it immediately with its callsign clue. A background loop runs stage 2 about 60 s later and adds the autopilot clue from ADS-B snapshots. The web app reads those tables directly (as it already does for comments), groups the rows into flight "strips" and serves a conversation per flight with audio and a "move to…" control. There is no LLM anywhere in the path, and no logic is shared with the CH01 `conversations.py` resolver.

**Tech Stack:** Python 3.14, stdlib `sqlite3`, rapidfuzz (already present), FastAPI (existing panel), plain browser JavaScript with no build step, pytest.

**Spec:** `docs/superpowers/specs/2026-09-24-airband-conversations-design.md`

## Global Constraints

- Scope: channels `121.200` / `121,200` / `121.205` / `121,205` only. Both decimal separators, because the plugin formats the channel in SDR#'s culture.
- `AIR_ECHO_ENABLED` default **`off`**. The echo clue is **computed and stored regardless**; the setting only decides whether it counts towards the displayed outcome.
- Echo window: from **t−10 s to t+60 s**. Tolerance: **±100 ft** altitude, **±5°** heading. **Only a change counts**: the aircraft must have been present in the last snapshot at or before t−10 s with a different value. **A unique match only.**
- Numbers extracted: **altitudes and headings only**. QNH, speed, frequencies and runways are never extracted.
- Transmission ids are SQLite `INTEGER PRIMARY KEY AUTOINCREMENT`. Never reuse `conversations._chunk_seq`.
- Timestamps are stored as full local ISO-8601 **with offset** (`datetime.now().astimezone().isoformat(timespec="seconds")`), plus an `epoch` REAL for range queries.
- All SQL lives in the top-level archive modules (`conversation_archive.py`, the new `air_archive.py`). Every connection goes through `conversation_archive.connect`, so the conftest guard against the real archive keeps working.
- `webapp` must never import `stt_proxy`, and `stt_proxy` must never import `webapp`. Shared code sits at top level (same rule as `ship_types.py`).
- Nothing in the attribution path may raise into the transcription path. The plugin must always get its text back.
- Tests must never touch `server/logs`, `server/stt_proxy/conversations.db` or the real captures directory.
- The proxy does **not** hot-reload. Deploying a change means restarting it through the control panel's Proxy card (Stop/Start), never by killing the PID by hand.
- Every commit ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Replays and tests writing into the real ADS-B snapshot log.** `bench_flight_identify._load_cache` and several tests call `adsb.poll_once`. Once polling writes a daily log, every replay would append the 09-10 aircraft to *today's* log. Expected: only the live poll loop writes, and tests can't reach `server/logs` (pinned in Task 1).
2. **Frequencies, runways and bare readbacks read as altitudes or headings.** Examples: "one eight four zero five", "one two three seven zero five", "ILS runway one eight right", "flight level seven zero eight zero". Expected: no number (pinned in Task 2).
3. **A proxy restart between stage 1 and stage 2.** Expected: the pending transmission is re-checked from the daily snapshot log, or else marked `no_snapshots`, never `none`. `none` would count as a measured failure of the clue (pinned in Task 4).
4. **Moving a transmission whose id doesn't exist, or moving to a malformed key.** Expected: 404 / 400, and nothing written (pinned in Task 7).
5. **A transmission just after midnight, with a proxy running in UTC while captures are in local time.** Expected: ▶ looks in the capture directory for the transmission's own local date (pinned in Task 7).

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `server/stt_proxy/adsb.py` | modify | keep autopilot fields; snapshot ring; daily snapshot log; `snapshots_between()` |
| `server/stt_proxy/air_numbers.py` | create | pure extraction of altitudes/headings from transcript text |
| `server/stt_proxy/flight_identify.py` | modify | expose `callsign_clue()` (structured); `identify_flight` unchanged in behaviour |
| `server/stt_proxy/flight_attribution.py` | create | `echo_clue()`, `combine()`, `record_transmission()` (stage 1), `recheck_pending()` + loop (stage 2) |
| `server/air_archive.py` | create | schema + all SQL for `air_transmissions` / `air_moves` |
| `server/whisper-proxy.py` | modify | call stage 1 in the airband branch; start the stage-2 loop |
| `server/webapp/settings_schema.py` | modify | `AIR_ECHO_ENABLED` |
| `server/bench_flight_identify.py` | modify | `--via-attribution` regression path; `poll_once(record=False)` |
| `server/bench_air_echo.py` | create | the switch-on gate measurement |
| `server/webapp/air_view.py` | create | pure grouping: strips, thread, move targets |
| `server/webapp/app.py` | modify | `/api/air/flights`, `/api/air/thread`, `POST /api/air/moves` |
| `server/webapp/static/air.js` | create | the Airband tab |
| `server/webapp/static/index.html`, `app.js`, `app.css` | modify | tab button, `<main id="airband">`, polling hook, styles |
| `server/tests/conftest.py` | modify | guard: ADS-B snapshot log under `tmp_path` |
| `server/tests/test_adsb.py`, `test_air_numbers.py`, `test_flight_identify.py`, `test_flight_attribution.py`, `test_air_archive.py`, `test_air_view.py`, `test_app_routes.py`, `test_bench_flight_identify.py`, `test_bench_air_echo.py` | create/modify | tests per task |
| `docs/user-manual.md` | modify | an "Airband tab" section |

Run all tests from `server/`: `py -m pytest tests -q`. The suite is about 1,250 tests; each task runs its own file first and then the whole suite once before committing.

---

### Task 1: ADS-B keeps autopilot fields and records snapshots

This task gets deployed on its own as soon as it's done. Every day without these fields is data the measurement can't use.

**Files:**
- Modify: `server/stt_proxy/adsb.py`
- Modify: `server/tests/conftest.py`
- Modify: `server/bench_flight_identify.py:210-218` (`_load_cache`)
- Test: `server/tests/test_adsb.py`

**Interfaces:**
- Produces:
  - `adsb.map_aircraft(ac) -> dict | None`, now also with keys `nav_altitude_mcp`, `nav_heading`, `nav_qnh`, `baro_rate`.
  - `adsb.poll_once(lat, lon, dist_nm, fetch=None, record=True, now=None) -> int`. With `record=True` it appends to the ring and the daily log.
  - `adsb.snapshots_between(t0: float, t1: float) -> list[dict]`: each item is `{"t": <epoch float>, "aircraft": [mapped dicts]}`, oldest first, covering `t0 <= t <= t1`. It reads the in-memory ring and falls back to the daily log file(s) when the ring doesn't reach back to `t0`.
  - `adsb.SNAPSHOT_LOG_DIR: Path` (module variable; tests patch it).
  - `adsb.snapshot_log_path(day: str) -> Path`: returns `SNAPSHOT_LOG_DIR / f"adsb-{day}.jsonl"`.

- [ ] **Step 1: Add the conftest guard first** (so the new tests below can't touch `server/logs`)

Append to `server/tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _adsb_snapshot_log_under_tmp_path(monkeypatch, tmp_path):
    """Every adsb.poll_once in a test writes its snapshot log under tmp_path.

    Once polling records a daily log, any test (or replay) that loads a cache through
    poll_once would otherwise append made-up aircraft to the operator's real
    logs/adsb-<today>.jsonl -- the very file the autopilot-clue measurement is scored from.
    """
    from stt_proxy import adsb as adsb_module

    monkeypatch.setattr(adsb_module, "SNAPSHOT_LOG_DIR", tmp_path / "adsb-logs")
    adsb_module.reset_snapshot_ring()
```

- [ ] **Step 2: Write the failing tests**

Append to `server/tests/test_adsb.py` (check the existing imports at the top of the file; it already imports `json` and `adsb`, so add whatever is missing):

```python
import datetime
import json

from stt_proxy import adsb


def _payload(*aircraft):
    return lambda _url: json.dumps({"aircraft": list(aircraft)}).encode("utf-8")


# A real 2026 instant: on Windows, astimezone() raises OSError for epochs near 1970.
B = 1_790_000_000.0

KLM12B = {"hex": "484161", "flight": "KLM12B  ", "t": "B738", "r": "PH-BXH",
          "alt_baro": 9725, "nav_altitude_mcp": 7008, "nav_heading": 51.33,
          "nav_qnh": 1013.6, "baro_rate": -1024}


def test_map_aircraft_keeps_the_autopilot_selected_values():
    mapped = adsb.map_aircraft(KLM12B)
    assert mapped["nav_altitude_mcp"] == 7008
    assert mapped["nav_heading"] == 51.33
    assert mapped["nav_qnh"] == 1013.6
    assert mapped["baro_rate"] == -1024


def test_map_aircraft_leaves_absent_autopilot_values_as_none():
    mapped = adsb.map_aircraft({"hex": "abc123", "flight": "PHVSY"})
    assert mapped["nav_altitude_mcp"] is None and mapped["nav_heading"] is None


def test_a_recorded_poll_is_appended_to_the_daily_log(tmp_path):
    now = datetime.datetime(2026, 9, 24, 11, 14, 2).astimezone().timestamp()
    adsb.poll_once(0, 0, 0, fetch=_payload(KLM12B), now=now)
    path = adsb.snapshot_log_path("2026-09-24")
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1
    assert rows[0]["t"].startswith("2026-09-24T11:14:02")
    assert rows[0]["aircraft"][0]["nav_altitude_mcp"] == 7008
    assert "last_seen" not in rows[0]["aircraft"][0]


def test_an_unrecorded_poll_writes_nothing_and_keeps_no_snapshot():
    adsb.poll_once(0, 0, 0, fetch=_payload(KLM12B), record=False, now=B)
    assert not adsb.SNAPSHOT_LOG_DIR.exists() or not any(adsb.SNAPSHOT_LOG_DIR.iterdir())
    assert adsb.snapshots_between(B - 100, B + 100) == []


def test_snapshots_between_reads_the_ring_oldest_first():
    for t in (B, B + 15, B + 30):
        adsb.poll_once(0, 0, 0, fetch=_payload(KLM12B), now=t)
    got = adsb.snapshots_between(B + 10, B + 30)
    assert [s["t"] for s in got] == [B + 15, B + 30]


def test_snapshots_between_falls_back_to_the_log_after_a_restart():
    now = datetime.datetime(2026, 9, 24, 11, 0, 0).astimezone().timestamp()
    adsb.poll_once(0, 0, 0, fetch=_payload(KLM12B), now=now)
    adsb.reset_snapshot_ring()          # what a proxy restart does to memory
    got = adsb.snapshots_between(now - 5, now + 5)
    assert len(got) == 1 and got[0]["aircraft"][0]["hex"] == "484161"
    assert abs(got[0]["t"] - now) < 1.0


def test_a_log_write_failure_does_not_fail_the_poll(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(adsb, "_append_snapshot_log", boom)
    assert adsb.poll_once(0, 0, 0, fetch=_payload(KLM12B), now=B) == 1
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `py -m pytest tests/test_adsb.py -q`
Expected: FAIL. `reset_snapshot_ring` / `snapshot_log_path` don't exist yet, and `map_aircraft` has no `nav_*` keys.

- [ ] **Step 4: Implement**

In `server/stt_proxy/adsb.py`:

1. Add imports: `import collections`, `import datetime`, `from pathlib import Path`.
2. In `map_aircraft`, add these four keys to the returned dict after `"squawk"`:

```python
        # The autopilot's SELECTED values (adsb.fi carries them for roughly half the traffic,
        # measured 2026-09-24: 39/81 had nav_altitude_mcp). A controller's "descend seven
        # thousand" shows up here seconds later -- the evidence flight_attribution's echo clue
        # is built on. Dropped until 2026-09-24, which is why the 09-10 log cannot replay it.
        "nav_altitude_mcp": ac.get("nav_altitude_mcp"),
        "nav_heading": ac.get("nav_heading"),
        "nav_qnh": ac.get("nav_qnh"),
        "baro_rate": ac.get("baro_rate"),
```

3. Add below `current_aircraft()`:

```python
# -- snapshot history: what flight_attribution's stage 2 looks back over ---------------

SNAPSHOT_LOG_DIR = Path(os.environ.get("ADSB_SNAPSHOT_DIR", "").strip()
                        or Path(__file__).resolve().parent.parent / "logs")

# 5 minutes at the 15 s default -- stage 2 needs t-10 s .. t+60 s, plus slack for a slow loop.
_RING_LEN = 20
_ring_lock = threading.Lock()
_ring: collections.deque = collections.deque(maxlen=_RING_LEN)
_log_warned = False


def reset_snapshot_ring() -> None:
    """Forget every in-memory snapshot. What a restart does; tests call it for isolation."""
    with _ring_lock:
        _ring.clear()


def snapshot_log_path(day: str) -> Path:
    return Path(SNAPSHOT_LOG_DIR) / f"adsb-{day}.jsonl"


def _append_snapshot_log(snapshot: dict) -> None:
    """One line per poll, the same shape as the 09-10 side-car bench_flight_identify reads."""
    stamp = datetime.datetime.fromtimestamp(snapshot["t"]).astimezone()
    path = snapshot_log_path(stamp.date().isoformat())
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps({"t": stamp.isoformat(), "aircraft": snapshot["aircraft"]},
                      ensure_ascii=False)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def _record_snapshot(aircraft: list[dict], now: float) -> None:
    """Ring first, log second; a log failure is reported once and never fails the poll."""
    global _log_warned
    snapshot = {"t": now, "aircraft": aircraft}
    with _ring_lock:
        _ring.append(snapshot)
    try:
        _append_snapshot_log(snapshot)
    except Exception as exc:
        if not _log_warned:
            print(f"[adsb.fi] could not write the snapshot log: {exc}", flush=True)
            _log_warned = True


def _snapshots_from_log(t0: float, t1: float) -> list[dict]:
    days = {datetime.datetime.fromtimestamp(t).astimezone().date().isoformat()
            for t in (t0, t1)}
    out = []
    for day in sorted(days):
        path = snapshot_log_path(day)
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                t = datetime.datetime.fromisoformat(row["t"]).timestamp()
            except (ValueError, KeyError, TypeError):
                continue
            if t0 <= t <= t1:
                out.append({"t": t, "aircraft": row.get("aircraft") or []})
    return sorted(out, key=lambda s: s["t"])


def snapshots_between(t0: float, t1: float) -> list[dict]:
    """Every recorded snapshot with t0 <= t <= t1, oldest first.

    The ring answers when it reaches back to t0; otherwise (a restart emptied it, or t0 is
    older than five minutes) the daily log is read instead, so a restart between stage 1 and
    stage 2 costs evidence only if the log is missing too.
    """
    with _ring_lock:
        ring = list(_ring)
    if ring and ring[0]["t"] <= t0:
        return [s for s in ring if t0 <= s["t"] <= t1]
    return _snapshots_from_log(t0, t1)
```

4. Change `poll_once`'s signature and body:

```python
def poll_once(lat: float, lon: float, dist_nm: float, fetch=None,
              record: bool = True, now: float | None = None) -> int:
```

After building `mapped` and replacing the cache, add:

```python
    if record:
        stamp = time.time() if now is None else now
        _record_snapshot([{k: v for k, v in f.items() if k != "last_seen"}
                          for f in mapped.values()], stamp)
```

Also replace `fields["last_seen"] = time.time()` with `fields["last_seen"] = time.time() if now is None else now`.

5. In `server/bench_flight_identify.py`'s `_load_cache`, pass `record=False`:

```python
    adsb.poll_once(0.0, 0.0, 0.0, fetch=lambda _url: payload, record=False)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -m pytest tests/test_adsb.py tests/test_bench_flight_identify.py tests/test_flight_identify.py -q`
Expected: PASS.

- [ ] **Step 6: Run the whole suite**

Run: `py -m pytest tests -q`
Expected: all pass. Then confirm nothing was written under `server/logs` during the run: `git status --short server/logs` should show no new `adsb-2026-*.jsonl` created by tests. The `logs` directory is gitignored, so check with `ls -t server/logs | head` instead.

- [ ] **Step 7: Commit**

```bash
git add server/stt_proxy/adsb.py server/tests/conftest.py server/tests/test_adsb.py server/bench_flight_identify.py
git commit -m "Keep ADS-B autopilot fields and record every poll to a daily log"
```

- [ ] **Step 8: Deploy (operator confirmation required)**

Ask the operator before restarting. Restart the proxy through the control panel (Proxy card → Stop, then Start). Wait 30 s, then check `server/logs/adsb-<today>.jsonl` exists and that its latest line contains `nav_altitude_mcp` values:

```bash
py -c "import json,pathlib,datetime;p=pathlib.Path('logs')/f'adsb-{datetime.date.today()}.jsonl';r=json.loads(p.read_text().splitlines()[-1]);print(len(r['aircraft']),sum(1 for a in r['aircraft'] if a.get('nav_altitude_mcp')))"
```

Expected: two numbers, the second roughly half of the first.

---

### Task 2: Extract altitudes and headings from a transmission

**Files:**
- Create: `server/stt_proxy/air_numbers.py`
- Test: `server/tests/test_air_numbers.py`

**Interfaces:**
- Produces:
  - `air_numbers.Number`: a frozen dataclass with `kind: str` (`"altitude"` or `"heading"`), `value: int` (feet or degrees) and `text: str` (the words it came from).
  - `air_numbers.extract_numbers(text: str) -> list[Number]`, in spoken order, deduplicated.
  - `air_numbers.to_dict(n: Number) -> dict` and `from_dict(d) -> Number`.

The rule that makes the negatives work: a number is only read **directly after a trigger word**. Frequencies, runways and QNH have no trigger, so they are never consumed. A flight-level run must be exactly 2–3 digits, and a heading run exactly 3 digits with a value from 1 to 360.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_air_numbers.py`. All strings are real 2026-09-10 transcripts:

```python
import pytest

from stt_proxy.air_numbers import Number, extract_numbers, from_dict, to_dict


def alt(v):
    return ("altitude", v)


def hdg(v):
    return ("heading", v)


def kinds(text):
    return [(n.kind, n.value) for n in extract_numbers(text)]


@pytest.mark.parametrize("text, expected", [
    ("Descend flight level seven zero, QNH one two bravo.", [alt(7000)]),
    ("Maintain flight level six zero, Delta seven three.", [alt(6000)]),
    ("Climb one three zero, delta seven three.", [alt(13000)]),
    ("Flight level one three zero, American two zero two.", [alt(13000)]),
    ("Level four zero, Foxtrot ILS runway right, QNH one six.", [alt(4000)]),
    ("Maintain level six zero, United nine four seven.", [alt(6000)]),
    ("Descending zero two zero, nine eight two.", [alt(2000)]),
    ("Coteo, present heading, descend flight level seven zero, QNH one nine zero four four.",
     [alt(7000)]),
    ("Target two zero three, and two thousand feet, one airport, ILS six zero.", [alt(2000)]),
    ("One two three seven zero five, uh, nine thousand feet, take a break.", [alt(9000)]),
    ("Delta five seven with you, twenty five hundred, six.", [alt(2500)]),
    ("ILS, heading zero seven zero, ILS one eight three four five, QNH one eight two", [hdg(70)]),
    ("Turn left heading zero six zero, contact, drive on one one eight four zero five",
     [hdg(60)]),
    ("Over, right heading three six zero, KLM six two seven.", [hdg(360)]),
    ("Right turn three six zero, ship blue three two.", [hdg(360)]),
    ("Right turn three six zero, up to level one three zero, United nine four seven.",
     [hdg(360), alt(13000)]),
    ("Heading two seven zero, NRA one one eight four zero five, QNH one three zero.",
     [hdg(270)]),
    ("Two seven zero degrees, two four three zero alpha.", []),   # no trigger word
])
def test_positive_and_mixed(text, expected):
    assert kinds(text) == expected


@pytest.mark.parametrize("text", [
    # frequencies -- 118.405 and 123.705 spoken in full or abbreviated
    "One eight four zero five, this is the inbound aircraft.",
    "One two three seven zero five for Delta, seven three, good day.",
    "Contact arrival one one eight, decimal four zero five, QNH one one eight, bye.",
    "Tower one eight four zero five, six one three zero, goodbye.",
    # runways
    "ILS approach runway one eight center, over.",
    "Center for four zero, for the ILS runway right.",
    "ILS runway one eight right, roger",
    # QNH is never a trigger
    "Pulse, good morning, QNH one nine four four.",
    "Cleared runway one three zero, QNH one six two seven.",
    # a flight-level run that is not 2-3 digits is ambiguous, not guessed
    "Super four three zero, Alpha reducing two fifty, flight level seven zero eight zero",
    # current altitude, not a clearance
    "Port Cremoros three six seven heavy, passing two thousand six hundred, focus NISO.",
    # heading out of range
    "heading four five zero",
    "",
])
def test_negatives_yield_nothing(text):
    assert extract_numbers(text) == []


def test_numerals_are_read_like_spoken_digits():
    assert kinds("descend flight level 70") == [alt(7000)]
    assert kinds("heading 270") == [hdg(270)]


def test_a_number_repeated_in_one_transmission_is_reported_once():
    assert kinds("descend flight level seven zero, flight level seven zero") == [alt(7000)]


def test_round_trip_through_a_dict():
    n = Number("altitude", 7000, "flight level seven zero")
    assert from_dict(to_dict(n)) == n
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_air_numbers.py -q`
Expected: FAIL with `ModuleNotFoundError: stt_proxy.air_numbers`.

- [ ] **Step 3: Implement**

Create `server/stt_proxy/air_numbers.py`:

```python
"""Altitudes and headings spoken in an airband transmission.

Only numbers that directly follow a trigger word are read. That single rule is what keeps
frequencies ("one eight four zero five" = 118.405), runways ("runway one eight right") and QNH
out: none of them has a trigger, so none is ever consumed. QNH is excluded on purpose -- the
decoder writes "QNH" where "KLM" was spoken (2026-09-11 finding) -- and speed is excluded
because ADS-B carries no selected speed to compare it with.

See docs/superpowers/specs/2026-09-24-airband-conversations-design.md.
"""
from __future__ import annotations

import dataclasses
import re

_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "tree": "3",
    "four": "4", "five": "5", "fife": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "niner": "9",
}
_SMALL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19, "twenty": 20, "thirty": 30}

_ALT_VERBS = {"climb", "climbing", "descend", "descending", "maintain", "maintaining"}
_CURRENT = {"passing", "leaving"}          # a report of where they are, not a clearance
_FILLER = {"to", "and", "flight", "level", "altitude", "now", "further"}


@dataclasses.dataclass(frozen=True)
class Number:
    kind: str       # "altitude" (feet) or "heading" (degrees)
    value: int
    text: str


def to_dict(n: Number) -> dict:
    return dataclasses.asdict(n)


def from_dict(d: dict) -> Number:
    return Number(kind=d["kind"], value=int(d["value"]), text=d.get("text", ""))


def _tokens(text: str) -> list[str]:
    """Words, digits split one per token, and punctuation kept as a boundary.

    The comma matters: "descending zero two zero, nine eight two" is FL020 followed by a
    callsign, and without the boundary the two runs merge into one six-digit run.
    """
    out = []
    for raw in re.findall(r"[a-z]+|\d+|[,.;:!?]", (text or "").lower()):
        if raw.isdigit():
            out.extend(raw)             # "70" is read as "seven zero"
        else:
            out.append(raw)
    return out


def _digit(tok: str) -> str | None:
    if tok.isdigit():
        return tok
    return _DIGITS.get(tok)


def _digit_run(toks: list[str], i: int) -> tuple[str, int]:
    """The maximal run of digits starting at i, and the index after it."""
    run = ""
    while i < len(toks):
        d = _digit(toks[i])
        if d is None:
            break
        run += d
        i += 1
    return run, i


_TENS = {"twenty": 20, "thirty": 30}


def _thousands(toks: list[str], i: int) -> tuple[int, int] | None:
    """'two thousand [five hundred]' / 'twenty five hundred' starting at i -> (feet, end).

    Only the word(s) directly before "thousand"/"hundred" count, so "Delta seven three two
    thousand" is 2,000 ft after the callsign, not 732,000 or 12,000.
    """
    def unit(k):
        return _SMALL.get(toks[k]) if k < len(toks) else None

    if toks[i] in _TENS and unit(i + 1) and unit(i + 1) < 10 and             i + 2 < len(toks) and toks[i + 2] in {"thousand", "hundred"}:
        n, j = _TENS[toks[i]] + unit(i + 1), i + 2
    elif unit(i) and i + 1 < len(toks) and toks[i + 1] in {"thousand", "hundred"}:
        n, j = unit(i), i + 1
    else:
        return None
    if toks[j] == "thousand":
        feet = n * 1000
        j += 1
        if j + 1 < len(toks) and unit(j) and toks[j + 1] == "hundred":
            feet += unit(j) * 100
            j += 2
        return feet, j
    if n >= 10:                              # "twenty five hundred"; "five hundred" is not
        return n * 100, j + 1
    return None


def extract_numbers(text: str) -> list[Number]:
    toks = _tokens(text)
    found: list[Number] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        prev = toks[i - 1] if i else ""

        if tok == "heading" or (tok == "turn" and prev in {"left", "right"}) or (
                tok in {"left", "right"} and i + 1 < len(toks) and toks[i + 1] == "turn"):
            j = i + 1
            while j < len(toks) and toks[j] in {"turn", "heading", "left", "right"}:
                j += 1
            run, end = _digit_run(toks, j)
            if len(run) == 3 and 1 <= int(run) <= 360:
                found.append(Number("heading", int(run), " ".join(toks[i:end])))
                i = end
                continue

        if tok == "level" or tok in _ALT_VERBS:
            j = i + 1
            while j < len(toks) and toks[j] in _FILLER:
                j += 1
            run, end = _digit_run(toks, j)
            if 2 <= len(run) <= 3 and end < len(toks) + 1 and (
                    end == len(toks) or toks[end] not in {"thousand", "hundred"}):
                found.append(Number("altitude", int(run) * 100, " ".join(toks[i:end])))
                i = end
                continue

        if tok in _SMALL:
            if not any(t in _CURRENT for t in toks[max(0, i - 2):i]) and prev not in {"of"}:
                got = _thousands(toks, i)
                if got is not None:
                    feet, end = got
                    found.append(Number("altitude", feet, " ".join(toks[i:end])))
                    i = end
                    continue
        i += 1

    unique: list[Number] = []
    seen = set()
    for n in found:
        if (n.kind, n.value) not in seen:
            seen.add((n.kind, n.value))
            unique.append(n)
    return unique
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_air_numbers.py -q`
Expected: PASS. If a real-transcript case fails, fix the extractor rather than the test. These expectations are the spec. The exceptions are `"New York Delta seven three two thousand climbing flight level zero six zero"`-style lines, which aren't in the test list on purpose: they are ambiguous, and yielding both `2000` and `6000` for them is acceptable.

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/air_numbers.py server/tests/test_air_numbers.py
git commit -m "Extract spoken altitudes and headings from airband transmissions"
```

---

### Task 3: A structured callsign clue

**Files:**
- Modify: `server/stt_proxy/flight_identify.py:297-311`
- Test: `server/tests/test_flight_identify.py`

**Interfaces:**
- Produces: `flight_identify.callsign_clue(text: str) -> dict`, returning exactly `{"candidate": str | None, "hex": str | None, "flight": str | None, "type": str | None, "reg": str | None}`. No logging.
- `identify_flight(text) -> str` keeps its current output and logging, now built on `callsign_clue`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_flight_identify.py`. It already has an autouse `_clear_cache` fixture and a `_seed(hex_id, flight, t="B738")` helper that writes straight into `adsb._aircraft_cache` with `"r": None`:

```python
def test_callsign_clue_names_the_matched_aircraft():
    _seed("484161", "KLM12B")
    clue = flight_identify.callsign_clue("Descend flight level seven zero, KLM one two bravo.")
    assert clue == {"candidate": "KLM12B", "hex": "484161", "flight": "KLM12B",
                    "type": "B738", "reg": None}


def test_callsign_clue_keeps_a_heard_callsign_that_matched_nothing():
    _seed("484161", "KLM12B")
    clue = flight_identify.callsign_clue("KLM one four zero six, good day")
    assert clue["candidate"] == "KLM1406" and clue["hex"] is None


def test_callsign_clue_is_empty_for_chatter():
    assert flight_identify.callsign_clue("ILS approach runway one eight center") == {
        "candidate": None, "hex": None, "flight": None, "type": None, "reg": None}


def test_identify_flight_output_is_unchanged():
    _seed("484161", "KLM12B")
    assert flight_identify.identify_flight("KLM one two bravo, good morning") ==         "[KLM12B/B738] KLM one two bravo, good morning"
```

(Checked 2026-09-24: the extractor returns `KLM12B`, `KLM1406`, `KLM12B` and `None` for these four phrases.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_flight_identify.py -q`
Expected: FAIL with `AttributeError: ... callsign_clue`.

- [ ] **Step 3: Implement**

Replace `identify_flight` at the bottom of `flight_identify.py` with:

```python
def callsign_clue(text: str) -> dict:
    """What the callsign path found, as data rather than a prefixed string.

    flight_attribution stores this per transmission. `candidate` without `hex` is a callsign
    that was heard but is not in ADS-B -- a different fact from "no callsign was heard".
    Never logs: identify_flight owns the unmatched-candidate line, and the same transmission
    goes through both.
    """
    candidate = extract_callsign_candidate(text)
    result = match_flight(candidate) if candidate else None
    return {
        "candidate": candidate,
        "hex": result.get("hex") if result else None,
        "flight": result.get("flight") if result else None,
        "type": result.get("t") if result else None,
        "reg": result.get("r") if result else None,
    }


def identify_flight(text: str) -> str:
    """The one call site Task 3 needs: identify and prefix, or return text unchanged.

    Only logs on a genuine extraction-but-no-match -- ordinary chatter (headings, QNH) never
    yields a candidate at all and must not print anything, or the console would drown in
    noise the way over-eager logging elsewhere in this project has before.
    """
    clue = callsign_clue(text)
    if clue["candidate"] is None:
        return text
    if clue["hex"] is None:
        _log_unmatched(clue["candidate"])
        return text
    return format_flight_for_plugin({"flight": clue["flight"], "t": clue["type"]}, text)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_flight_identify.py tests/test_bench_flight_identify.py tests/test_whisper_proxy.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/flight_identify.py server/tests/test_flight_identify.py
git commit -m "Expose the callsign clue as data for flight attribution"
```

---

### Task 4: The archive tables for airband transmissions

**Files:**
- Create: `server/air_archive.py`
- Modify: `server/conversation_archive.py:1-10` (docstring: SQL now lives in the two archive modules)
- Test: `server/tests/test_air_archive.py`

**Interfaces:**
- Consumes: `conversation_archive.connect(path)`.
- Produces:
  - `air_archive.open_db(path)`: a context manager yielding a connection with the air schema ensured.
  - `insert_transmission(conn, row: dict) -> int`. `row` keys: `t` (str), `epoch` (float), `channel`, `text`, `numbers` (list), `callsign_clue` (dict), `state` (dict|None), `outcome_kind`, `outcome_key`, `badge`. Returns the new id.
  - `set_echo(conn, tid: int, echo: dict, outcome: dict, state: dict | None) -> None`. `outcome` = `{"kind", "key", "badge"}`.
  - `pending_echo(conn, cutoff_epoch: float) -> list[dict]`: rows with `echo_clue IS NULL AND epoch <= cutoff`.
  - `transmissions(conn, start_epoch: float, end_epoch: float) -> list[dict]`: each row has its columns decoded from JSON, plus `effective_key` (latest move's `to_key`, else `outcome_key`) and `moved` (bool), ordered by epoch.
  - `get_transmission(conn, tid: int) -> dict | None`: the same shape as a `transmissions` row.
  - `add_move(conn, tid: int, to_key: str, now: str | None = None) -> dict | None`: returns `{"transmission_id", "from_key", "to_key", "t_moved"}`, or `None` when `tid` doesn't exist.
  - `valid_key(key: str) -> bool`: accepts `unassigned`, `review`, `heard:<1-12 chars A-Z0-9>` and a 6-hex ICAO address.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_air_archive.py`:

```python
import air_archive


def _row(epoch, key="484161", kind="flight", badge="callsign", **extra):
    base = {"t": f"2026-09-24T11:{int(epoch) // 60 % 60:02d}:00+02:00", "epoch": float(epoch),
            "channel": "121.200", "text": "Descend flight level seven zero",
            "numbers": [{"kind": "altitude", "value": 7000, "text": "level seven zero"}],
            "callsign_clue": {"candidate": "KLM12B", "hex": "484161", "flight": "KLM12B",
                              "type": "B738", "reg": "PH-BXH"},
            "state": {"flight": "KLM12B"}, "outcome_kind": kind, "outcome_key": key,
            "badge": badge}
    base.update(extra)
    return base


def test_ids_are_stable_across_reopening(tmp_path):
    db = tmp_path / "conversations.db"
    with air_archive.open_db(db) as conn:
        first = air_archive.insert_transmission(conn, _row(100))
    with air_archive.open_db(db) as conn:
        second = air_archive.insert_transmission(conn, _row(200))
        assert second == first + 1
        assert air_archive.get_transmission(conn, first)["numbers"][0]["value"] == 7000


def test_pending_echo_lists_only_rows_old_enough_and_unchecked(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        old = air_archive.insert_transmission(conn, _row(100))
        air_archive.insert_transmission(conn, _row(500))
        done = air_archive.insert_transmission(conn, _row(90))
        air_archive.set_echo(conn, done, {"status": "none"},
                             {"kind": "flight", "key": "484161", "badge": "callsign"}, None)
        assert [r["id"] for r in air_archive.pending_echo(conn, 200.0)] == [old]


def test_set_echo_updates_clue_outcome_and_state(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        tid = air_archive.insert_transmission(conn, _row(100))
        air_archive.set_echo(conn, tid, {"status": "match", "hex": "484161"},
                             {"kind": "flight", "key": "484161", "badge": "confirmed"},
                             {"flight": "KLM12B", "nav_altitude_mcp": 7008})
        got = air_archive.get_transmission(conn, tid)
        assert got["echo_clue"]["status"] == "match"
        assert got["badge"] == "confirmed"
        assert got["state"]["nav_altitude_mcp"] == 7008


def test_the_latest_move_wins_and_moves_are_kept(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        tid = air_archive.insert_transmission(conn, _row(100))
        first = air_archive.add_move(conn, tid, "unassigned", now="2026-09-24T12:00:00")
        second = air_archive.add_move(conn, tid, "4bb299", now="2026-09-24T12:01:00")
        assert first["from_key"] == "484161"
        assert second["from_key"] == "unassigned"
        row = air_archive.transmissions(conn, 0, 1_000)[0]
        assert row["effective_key"] == "4bb299" and row["moved"] is True
        count = conn.execute("SELECT COUNT(*) FROM air_moves").fetchone()[0]
        assert count == 2


def test_moving_a_missing_transmission_writes_nothing(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        assert air_archive.add_move(conn, 999, "unassigned") is None
        assert conn.execute("SELECT COUNT(*) FROM air_moves").fetchone()[0] == 0


def test_range_query_is_inclusive_and_ordered(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        for e in (300, 100, 200, 400):
            air_archive.insert_transmission(conn, _row(e))
        assert [r["epoch"] for r in air_archive.transmissions(conn, 100, 300)] == [100, 200, 300]


def test_valid_key():
    for good in ("unassigned", "review", "heard:KLM1406", "484161", "4BB299"):
        assert air_archive.valid_key(good), good
    for bad in ("", "heard:", "heard:" + "X" * 13, "48416", "zzzzzz", "flight", "heard:a b"):
        assert not air_archive.valid_key(bad), bad


def test_the_real_archive_is_unreachable_from_tests():
    import pytest
    import conversation_archive
    from pathlib import Path
    real = conversation_archive.default_db_path(Path(air_archive.__file__).parent)
    with pytest.raises(AssertionError):
        with air_archive.open_db(real):
            pass
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_air_archive.py -q`
Expected: FAIL with `ModuleNotFoundError: air_archive`.

- [ ] **Step 3: Implement**

Create `server/air_archive.py`:

```python
"""The airband transmission archive: Approach 4 transmissions, their clues, and hand moves.

Top level for the same reason conversation_archive.py is -- the proxy writes it and the panel
reads and writes it, and neither package may import the other. Same database file as the
conversation archive, opened through conversation_archive.connect so the test guard that
makes the operator's real archive unreachable covers these tables too.

See docs/superpowers/specs/2026-09-24-airband-conversations-design.md.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import re
import sqlite3

import conversation_archive

_SCHEMA = """
CREATE TABLE IF NOT EXISTS air_transmissions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  t             TEXT NOT NULL,
  epoch         REAL NOT NULL,
  channel       TEXT,
  text          TEXT NOT NULL,
  numbers       TEXT NOT NULL,
  callsign_clue TEXT NOT NULL,
  echo_clue     TEXT,
  state         TEXT,
  outcome_kind  TEXT NOT NULL,
  outcome_key   TEXT NOT NULL,
  badge         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS air_transmissions_epoch ON air_transmissions(epoch);

CREATE TABLE IF NOT EXISTS air_moves (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  transmission_id INTEGER NOT NULL,
  from_key        TEXT NOT NULL,
  to_key          TEXT NOT NULL,
  t_moved         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS air_moves_tid ON air_moves(transmission_id);
"""

_KEY = re.compile(r"^(unassigned|review|heard:[A-Z0-9]{1,12}|[0-9A-Fa-f]{6})$")
_JSON_COLS = ("numbers", "callsign_clue", "echo_clue", "state")

# The latest move per transmission, or NULL. MAX(id) rather than MAX(t_moved): two moves in
# the same second must still have a defined winner.
_SELECT = """
SELECT a.*, m.to_key AS moved_to
FROM air_transmissions a
LEFT JOIN air_moves m ON m.id = (
  SELECT MAX(id) FROM air_moves WHERE transmission_id = a.id)
"""


def valid_key(key: str) -> bool:
    return bool(_KEY.match(key or ""))


@contextlib.contextmanager
def open_db(path):
    conn = conversation_archive.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


def _decode(row: sqlite3.Row) -> dict:
    out = dict(row)
    for col in _JSON_COLS:
        out[col] = json.loads(out[col]) if out.get(col) else None
    moved_to = out.pop("moved_to", None)
    out["effective_key"] = moved_to or out["outcome_key"]
    out["moved"] = moved_to is not None
    return out


def insert_transmission(conn: sqlite3.Connection, row: dict) -> int:
    cursor = conn.execute(
        "INSERT INTO air_transmissions (t, epoch, channel, text, numbers, callsign_clue, "
        "state, outcome_kind, outcome_key, badge) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["t"], row["epoch"], row.get("channel"), row["text"],
         json.dumps(row.get("numbers") or []), json.dumps(row["callsign_clue"]),
         json.dumps(row["state"]) if row.get("state") is not None else None,
         row["outcome_kind"], row["outcome_key"], row["badge"]))
    conn.commit()
    return int(cursor.lastrowid)


def set_echo(conn: sqlite3.Connection, tid: int, echo: dict, outcome: dict,
             state: dict | None) -> None:
    conn.execute(
        "UPDATE air_transmissions SET echo_clue = ?, outcome_kind = ?, outcome_key = ?, "
        "badge = ?, state = COALESCE(?, state) WHERE id = ?",
        (json.dumps(echo), outcome["kind"], outcome["key"], outcome["badge"],
         json.dumps(state) if state is not None else None, tid))
    conn.commit()


def pending_echo(conn: sqlite3.Connection, cutoff_epoch: float) -> list[dict]:
    rows = conn.execute(_SELECT + " WHERE a.echo_clue IS NULL AND a.epoch <= ? "
                        "ORDER BY a.epoch", (cutoff_epoch,)).fetchall()
    return [_decode(r) for r in rows]


def transmissions(conn: sqlite3.Connection, start_epoch: float,
                  end_epoch: float) -> list[dict]:
    rows = conn.execute(_SELECT + " WHERE a.epoch >= ? AND a.epoch <= ? ORDER BY a.epoch",
                        (start_epoch, end_epoch)).fetchall()
    return [_decode(r) for r in rows]


def get_transmission(conn: sqlite3.Connection, tid: int) -> dict | None:
    row = conn.execute(_SELECT + " WHERE a.id = ?", (tid,)).fetchone()
    return _decode(row) if row else None


def add_move(conn: sqlite3.Connection, tid: int, to_key: str,
             now: str | None = None) -> dict | None:
    current = get_transmission(conn, tid)
    if current is None:
        return None
    stamp = now or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    conn.execute("INSERT INTO air_moves (transmission_id, from_key, to_key, t_moved) "
                 "VALUES (?, ?, ?, ?)", (tid, current["effective_key"], to_key, stamp))
    conn.commit()
    return {"transmission_id": tid, "from_key": current["effective_key"],
            "to_key": to_key, "t_moved": stamp}
```

In `server/conversation_archive.py`'s docstring, replace "Every line of SQL in the project lives here." with "Every line of SQL in the project lives here or in air_archive.py, which opens the same file through connect() below."

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_air_archive.py tests/test_conversation_archive.py tests/test_archive_guard.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add server/air_archive.py server/conversation_archive.py server/tests/test_air_archive.py
git commit -m "Add the airband transmission archive tables"
```

---

### Task 5: Attribution: echo clue, combining, stage 1 and stage 2

**Files:**
- Create: `server/stt_proxy/flight_attribution.py`
- Modify: `server/webapp/settings_schema.py` (add `AIR_ECHO_ENABLED` after `AIS_SUGGEST_TIEBREAK`)
- Modify: `docs/superpowers/specs/2026-09-24-airband-conversations-design.md` (see Step 5)
- Test: `server/tests/test_flight_attribution.py`

**Interfaces:**
- Consumes:
  - `air_numbers.extract_numbers` / `to_dict` / `from_dict` (Task 2)
  - `flight_identify.callsign_clue` (Task 3)
  - `adsb.snapshots_between` / `adsb.current_aircraft` (Task 1)
  - `air_archive.*` (Task 4)
- Produces:
  - `AIR_CONVERSATION_CHANNELS: frozenset[str]`
  - `AIR_ECHO_ENABLED: bool` (module variable, read from the env at import; tests patch it)
  - `echo_clue(numbers: list[Number], t: float, snapshots: list[dict]) -> dict`. It returns `{"status": "match"|"none"|"ambiguous"|"no_numbers"|"no_snapshots", "hex": str|None, "flight": str|None, "number": dict|None, "delay_s": float|None, "candidates": list[str]}`.
  - `combine(callsign: dict, echo: dict | None, echo_enabled: bool) -> dict`, returning `{"kind", "key", "badge"}`. `kind` is `flight|review|unknown|unassigned`; `badge` is `confirmed|callsign|echo|conflict|unknown|none`.
  - `record_transmission(text: str, channel: str, now: float | None = None, db_path=None) -> int | None`: stage 1. Never raises; returns `None` on any failure.
  - `recheck_pending(now: float | None = None, db_path=None, snapshots_between=None) -> int`: stage 2. Returns how many rows it completed.
  - `start()`: a daemon thread calling `recheck_pending` every 15 s.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_flight_attribution.py`:

```python
import re

import pytest

import air_archive
from stt_proxy import adsb, flight_attribution as fa
from stt_proxy.air_numbers import Number

# A real 2026 instant: on Windows, astimezone() raises OSError for epochs near 1970.
T = 1_790_000_000.0


def ac(hex_, flight, mcp=None, hdg=None, alt=9000):
    return {"hex": hex_, "flight": flight, "t": "B738", "r": "PH-X", "alt_baro": alt,
            "nav_altitude_mcp": mcp, "nav_heading": hdg}


def snaps(*pairs):
    return [{"t": t, "aircraft": list(a)} for t, a in pairs]


ALT7000 = [Number("altitude", 7000, "level seven zero")]


class TestEchoClue:
    def test_a_single_change_to_the_spoken_altitude_matches(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("484161", "KLM12B", mcp=11008), ac("4bb299", "DAL73", mcp=6016)]),
            (T + 8, [ac("484161", "KLM12B", mcp=7008), ac("4bb299", "DAL73", mcp=6016)])))
        assert got["status"] == "match" and got["hex"] == "484161"
        assert got["delay_s"] == pytest.approx(8.0)

    def test_already_set_does_not_count(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("484161", "KLM12B", mcp=7008)]),
            (T + 8, [ac("484161", "KLM12B", mcp=7008)])))
        assert got["status"] == "none"

    def test_two_aircraft_changing_is_ambiguous(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("a1", "KLM1", mcp=9000), ac("a2", "KLM2", mcp=9000)]),
            (T + 20, [ac("a1", "KLM1", mcp=7008), ac("a2", "KLM2", mcp=6992)])))
        assert got["status"] == "ambiguous" and sorted(got["candidates"]) == ["a1", "a2"]

    def test_a_change_after_the_window_does_not_count(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("484161", "KLM12B", mcp=11008)]),
            (T + 45, [ac("484161", "KLM12B", mcp=11008)]),
            (T + 75, [ac("484161", "KLM12B", mcp=7008)])))
        assert got["status"] == "none"

    def test_an_aircraft_absent_before_the_window_is_not_a_candidate(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, []), (T + 8, [ac("484161", "KLM12B", mcp=7008)])))
        assert got["status"] == "none"

    def test_heading_within_five_degrees_matches_across_north(self):
        got = fa.echo_clue([Number("heading", 360, "heading three six zero")], T, snaps(
            (T - 12, [ac("a1", "UAL947", hdg=250.0)]),
            (T + 10, [ac("a1", "UAL947", hdg=2.5)])))
        assert got["status"] == "match" and got["hex"] == "a1"

    def test_no_numbers_and_no_snapshots_are_distinct_from_none(self):
        assert fa.echo_clue([], T, snaps((T - 12, []), (T + 8, [])))["status"] == "no_numbers"
        assert fa.echo_clue(ALT7000, T, [])["status"] == "no_snapshots"
        # nothing at or before t-10: we cannot know what was "already set"
        assert fa.echo_clue(ALT7000, T, snaps((T + 8, []))) ["status"] == "no_snapshots"


CS_X = {"candidate": "KLM12B", "hex": "484161", "flight": "KLM12B", "type": "B738", "reg": "PH"}
CS_HEARD = {"candidate": "KLM1406", "hex": None, "flight": None, "type": None, "reg": None}
CS_NONE = {"candidate": None, "hex": None, "flight": None, "type": None, "reg": None}
E_X = {"status": "match", "hex": "484161"}
E_Y = {"status": "match", "hex": "4bb299"}
E_NONE = {"status": "none", "hex": None}


@pytest.mark.parametrize("cs, echo, enabled, expected", [
    (CS_X, E_X, True, ("flight", "484161", "confirmed")),
    (CS_X, E_NONE, True, ("flight", "484161", "callsign")),
    (CS_X, None, True, ("flight", "484161", "callsign")),
    (CS_NONE, E_Y, True, ("flight", "4bb299", "echo")),
    (CS_X, E_Y, True, ("review", "review", "conflict")),
    (CS_HEARD, E_Y, True, ("unknown", "heard:KLM1406", "unknown")),
    (CS_NONE, E_NONE, True, ("unassigned", "unassigned", "none")),
    (CS_NONE, {"status": "ambiguous", "hex": None}, True, ("unassigned", "unassigned", "none")),
    # shadow mode: the echo clue never changes the outcome
    (CS_X, E_X, False, ("flight", "484161", "callsign")),
    (CS_X, E_Y, False, ("flight", "484161", "callsign")),
    (CS_NONE, E_Y, False, ("unassigned", "unassigned", "none")),
])
def test_combine(cs, echo, enabled, expected):
    got = fa.combine(cs, echo, echo_enabled=enabled)
    assert (got["kind"], got["key"], got["badge"]) == expected


def test_combine_upper_cases_and_trims_the_heard_key():
    got = fa.combine({**CS_NONE, "candidate": "klm 14"}, None, echo_enabled=False)
    assert got["key"] == "heard:KLM14"


class TestStages:
    def test_stage_one_stores_the_callsign_outcome_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_X)
        monkeypatch.setattr(fa.adsb, "current_aircraft",
                            lambda: [ac("484161", "KLM12B", mcp=11008)])
        db = tmp_path / "c.db"
        tid = fa.record_transmission("Descend flight level seven zero, KLM one two bravo",
                                     "121,200", now=T, db_path=db)
        with air_archive.open_db(db) as conn:
            row = air_archive.get_transmission(conn, tid)
        assert row["outcome_key"] == "484161" and row["badge"] == "callsign"
        assert row["echo_clue"] is None
        assert row["numbers"] == [{"kind": "altitude", "value": 7000,
                                   "text": "descend flight level seven zero"}]
        assert row["state"]["flight"] == "KLM12B"
        assert re.search(r"[+-]\d\d:\d\d$", row["t"]), row["t"]   # offset kept

    def test_stage_one_never_raises(self, tmp_path, monkeypatch):
        def boom(_t):
            raise RuntimeError("matcher exploded")
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", boom)
        assert fa.record_transmission("x", "121.200", now=T, db_path=tmp_path / "c.db") is None

    def test_stage_two_waits_sixty_seconds_then_stores_the_echo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        monkeypatch.setattr(fa, "AIR_ECHO_ENABLED", False)
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero", "121.200",
                                     now=T, db_path=db)
        history = snaps((T - 12, [ac("484161", "KLM12B", mcp=11008)]),
                        (T + 8, [ac("484161", "KLM12B", mcp=7008)]))
        source = lambda t0, t1: [s for s in history if t0 <= s["t"] <= t1]   # noqa: E731
        assert fa.recheck_pending(now=T + 30, db_path=db, snapshots_between=source) == 0
        assert fa.recheck_pending(now=T + 61, db_path=db, snapshots_between=source) == 1
        with air_archive.open_db(db) as conn:
            row = air_archive.get_transmission(conn, tid)
        assert row["echo_clue"]["status"] == "match"
        # shadow mode: recorded, but the displayed outcome is still the callsign's
        assert row["outcome_key"] == "unassigned"

    def test_stage_two_with_echo_enabled_assigns_by_echo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        monkeypatch.setattr(fa, "AIR_ECHO_ENABLED", True)
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero", "121.200",
                                     now=T, db_path=db)
        history = snaps((T - 12, [ac("484161", "KLM12B", mcp=11008)]),
                        (T + 8, [ac("484161", "KLM12B", mcp=7008)]))
        fa.recheck_pending(now=T + 61, db_path=db,
                           snapshots_between=lambda a, b: [s for s in history
                                                           if a <= s["t"] <= b])
        with air_archive.open_db(db) as conn:
            row = air_archive.get_transmission(conn, tid)
        assert row["outcome_key"] == "484161" and row["badge"] == "echo"
        assert row["state"]["nav_altitude_mcp"] == 7008

    def test_a_restart_with_no_snapshot_evidence_is_no_snapshots_not_none(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero", "121.200",
                                     now=T, db_path=db)
        fa.recheck_pending(now=T + 3600, db_path=db, snapshots_between=lambda a, b: [])
        with air_archive.open_db(db) as conn:
            assert air_archive.get_transmission(conn, tid)["echo_clue"]["status"] == \
                "no_snapshots"

    def test_a_restart_recovers_evidence_from_the_daily_log(self, tmp_path, monkeypatch):
        """Review Focus 3: the default snapshot source is adsb.snapshots_between, whose log
        fallback survives a restart that emptied the ring."""
        import datetime
        import json
        now = datetime.datetime(2026, 9, 24, 11, 0, 0).astimezone().timestamp()
        before = {"aircraft": [ac("484161", "KLM12B", mcp=11008)]}
        after = {"aircraft": [ac("484161", "KLM12B", mcp=7008)]}
        adsb.poll_once(0, 0, 0, fetch=lambda _u: json.dumps(before).encode(), now=now - 12)
        adsb.poll_once(0, 0, 0, fetch=lambda _u: json.dumps(after).encode(), now=now + 8)
        adsb.reset_snapshot_ring()
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero", "121.200",
                                     now=now, db_path=db)
        fa.recheck_pending(now=now + 3600, db_path=db)
        with air_archive.open_db(db) as conn:
            assert air_archive.get_transmission(conn, tid)["echo_clue"]["status"] == "match"


def test_the_channel_list_covers_both_separators():
    assert {"121.200", "121,200", "121.205", "121,205"} == set(fa.AIR_CONVERSATION_CHANNELS)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_flight_attribution.py -q`
Expected: FAIL with `ImportError: flight_attribution`.

- [ ] **Step 3: Implement**

Create `server/stt_proxy/flight_attribution.py`:

```python
"""Which flight an Approach 4 transmission belongs to, from two independent clues.

Clue 1 is the callsign tag flight_identify already produces. Clue 2 -- the "echo" -- is an
aircraft whose autopilot SELECTED altitude or heading changes to a number spoken in the
transmission within seconds of it. The echo clue is computed and stored for every
transmission, but only counts towards the displayed outcome when AIR_ECHO_ENABLED is on, which
it is not until bench_air_echo.py says it has earned it.

Two stages, because a cleared altitude reaches ADS-B only after the transmission: stage 1
stores the callsign outcome at once; stage 2 runs once t+60 s has passed.

See docs/superpowers/specs/2026-09-24-airband-conversations-design.md.
"""
from __future__ import annotations

import datetime
import os
import re
import threading
import time

import air_archive
from stt_proxy import adsb, flight_identify
from stt_proxy.air_numbers import Number, extract_numbers, from_dict, to_dict

AIR_CONVERSATION_CHANNELS = frozenset({"121.200", "121,200", "121.205", "121,205"})

AIR_ECHO_ENABLED = os.environ.get("AIR_ECHO_ENABLED", "off").strip().lower() == "on"

WINDOW_BEFORE_S = 10.0
WINDOW_AFTER_S = 60.0
ALT_TOLERANCE_FT = 100
HEADING_TOLERANCE_DEG = 5.0
RECHECK_EVERY_S = 15.0

_STATE_FIELDS = ("flight", "t", "r", "alt_baro", "nav_altitude_mcp", "nav_heading", "track")


def _field_for(n: Number) -> str:
    return "nav_altitude_mcp" if n.kind == "altitude" else "nav_heading"


def _close(n: Number, value) -> bool:
    if not isinstance(value, (int, float)):
        return False
    if n.kind == "altitude":
        return abs(value - n.value) <= ALT_TOLERANCE_FT
    diff = abs((value - n.value + 180.0) % 360.0 - 180.0)
    return diff <= HEADING_TOLERANCE_DEG


def echo_clue(numbers: list[Number], t: float, snapshots: list[dict]) -> dict:
    empty = {"hex": None, "flight": None, "number": None, "delay_s": None, "candidates": []}
    if not numbers:
        return {"status": "no_numbers", **empty}
    before = [s for s in snapshots if s["t"] <= t - WINDOW_BEFORE_S]
    after = [s for s in snapshots if t - WINDOW_BEFORE_S < s["t"] <= t + WINDOW_AFTER_S]
    if not before or not after:
        return {"status": "no_snapshots", **empty}
    baseline = {a.get("hex"): a for a in before[-1]["aircraft"]}

    hits: dict[str, dict] = {}
    for n in numbers:
        field = _field_for(n)
        for snap in after:
            for a in snap["aircraft"]:
                hex_ = a.get("hex")
                if hex_ in hits or hex_ not in baseline or a.get("alt_baro") == "ground":
                    continue
                if _close(n, a.get(field)) and not _close(n, baseline[hex_].get(field)):
                    hits[hex_] = {"hex": hex_, "flight": a.get("flight"),
                                  "number": to_dict(n), "delay_s": snap["t"] - t}
    if not hits:
        return {"status": "none", **empty}
    if len(hits) > 1:
        return {"status": "ambiguous", **empty, "candidates": sorted(hits)}
    only = next(iter(hits.values()))
    return {"status": "match", **only, "candidates": [only["hex"]]}


def _heard_key(candidate: str) -> str:
    return "heard:" + re.sub(r"[^A-Z0-9]", "", candidate.upper())[:12]


def combine(callsign: dict, echo: dict | None, echo_enabled: bool) -> dict:
    cs_hex = callsign.get("hex")
    echo_hex = (echo or {}).get("hex") if (echo or {}).get("status") == "match" else None
    if not echo_enabled:
        echo_hex = None

    if callsign.get("candidate") and not cs_hex:
        return {"kind": "unknown", "key": _heard_key(callsign["candidate"]), "badge": "unknown"}
    if cs_hex and echo_hex and cs_hex != echo_hex:
        return {"kind": "review", "key": "review", "badge": "conflict"}
    if cs_hex and echo_hex:
        return {"kind": "flight", "key": cs_hex, "badge": "confirmed"}
    if cs_hex:
        return {"kind": "flight", "key": cs_hex, "badge": "callsign"}
    if echo_hex:
        return {"kind": "flight", "key": echo_hex, "badge": "echo"}
    return {"kind": "unassigned", "key": "unassigned", "badge": "none"}


def _state_of(hex_: str | None, aircraft: list[dict], airline: str | None = None) -> dict | None:
    if not hex_:
        return None
    for a in aircraft:
        if a.get("hex") == hex_:
            state = {k: a.get(k) for k in _STATE_FIELDS}
            state["airline"] = airline or _airline_name(a.get("flight"))
            return state
    return None


def _airline_name(flight: str | None) -> str | None:
    """'KLM12B' -> 'KLM', 'DAL73' -> 'Delta', from flight_identify's own telephony table."""
    code = (flight or "")[:3].upper()
    for word, icao in flight_identify.AIRLINE_TELEPHONY.items():
        if icao == code and len(word) > 3:
            return word.capitalize()
    return code or None


def _db_path():
    from stt_proxy import conversations           # late: a heavy module, and conftest patches it
    return conversations.CONVERSATIONS_DB


def record_transmission(text: str, channel: str, now: float | None = None,
                        db_path=None) -> int | None:
    """Stage 1. Never raises: the plugin is waiting for its transcription."""
    try:
        t = time.time() if now is None else now
        clue = flight_identify.callsign_clue(text)
        numbers = extract_numbers(text)
        outcome = combine(clue, None, echo_enabled=AIR_ECHO_ENABLED)
        state_hex = outcome["key"] if outcome["kind"] == "flight" else None
        row = {
            "t": datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds"),
            "epoch": t, "channel": channel, "text": text,
            "numbers": [to_dict(n) for n in numbers], "callsign_clue": clue,
            "state": _state_of(state_hex, adsb.current_aircraft()),
            "outcome_kind": outcome["kind"], "outcome_key": outcome["key"],
            "badge": outcome["badge"],
        }
        with air_archive.open_db(db_path or _db_path()) as conn:
            return air_archive.insert_transmission(conn, row)
    except Exception as exc:
        print(f"[air] could not record transmission: {type(exc).__name__}: {exc}", flush=True)
        return None


def recheck_pending(now: float | None = None, db_path=None, snapshots_between=None) -> int:
    """Stage 2 for every transmission older than 60 s that has no echo clue yet."""
    t_now = time.time() if now is None else now
    source = snapshots_between or adsb.snapshots_between
    done = 0
    with air_archive.open_db(db_path or _db_path()) as conn:
        for row in air_archive.pending_echo(conn, t_now - WINDOW_AFTER_S):
            t = row["epoch"]
            snaps = source(t - WINDOW_BEFORE_S - adsb.POLL_SEC * 2, t + WINDOW_AFTER_S)
            echo = echo_clue([from_dict(n) for n in row["numbers"] or []], t, snaps)
            outcome = combine(row["callsign_clue"], echo, echo_enabled=AIR_ECHO_ENABLED)
            latest = snaps[-1]["aircraft"] if snaps else []
            state = (_state_of(outcome["key"], latest)
                     if outcome["kind"] == "flight" else None)
            air_archive.set_echo(conn, row["id"], echo, outcome, state)
            done += 1
    return done


def _loop() -> None:
    while True:
        try:
            recheck_pending()
        except Exception as exc:
            print(f"[air] stage-2 pass failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(RECHECK_EVERY_S)


def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()
```

Add to `server/webapp/settings_schema.py`, directly after the `AIS_SUGGEST_TIEBREAK` entry:

```python
    SettingSpec(key="AIR_ECHO_ENABLED", type=SettingType.BOOL, default="off",
                group="Identification",
                description="Let the autopilot clue (a selected altitude/heading changing to a "
                            "spoken number) place Approach 4 transmissions under a flight. "
                            "It is recorded either way; switch on only after bench_air_echo.py "
                            "passes its three conditions."),
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_flight_attribution.py tests/test_catalogue_defaults.py tests/test_settings_schema.py tests/test_env_builder.py -q`
Expected: PASS. `test_catalogue_defaults` picks up `os.environ.get("AIR_ECHO_ENABLED", "off")` and checks it matches the catalogue's `"off"`.

- [ ] **Step 5: Record the one spec deviation**

In the spec's Error handling section, replace the `missed_restart` sentence with: "on startup nothing special is needed: stage 2 reads `adsb.snapshots_between`, which falls back to the daily log, and records `{"status": "no_snapshots"}` when the log doesn't cover the window either. That merges the spec's `missed_restart` into `no_snapshots`, since both mean 'no evidence', which is the only distinction the measurement needs."

- [ ] **Step 6: Commit**

```bash
git add server/stt_proxy/flight_attribution.py server/webapp/settings_schema.py server/tests/test_flight_attribution.py docs/superpowers/specs/2026-09-24-airband-conversations-design.md
git commit -m "Attribute Approach 4 transmissions from callsign and autopilot clues"
```

---

### Task 6: Wire attribution into the proxy, and prove nothing changed

**Files:**
- Modify: `server/whisper-proxy.py` (airband branch around line 485; startup around line 612)
- Modify: `server/bench_flight_identify.py` (`score()` and `main()`)
- Test: `server/tests/test_whisper_proxy.py`, `server/tests/test_bench_flight_identify.py`

**Interfaces:**
- Consumes: `flight_attribution.record_transmission`, `flight_attribution.start`, `flight_attribution.AIR_CONVERSATION_CHANNELS`, `flight_attribution.combine`, `flight_identify.callsign_clue`.
- Produces: `bench_flight_identify.score(..., via_attribution: bool = False)` and the CLI flag `--via-attribution`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_bench_flight_identify.py`, inside or next to the class holding `test_replay_reproduces_the_live_record_on_unchanged_code`, reusing its `corpus` fixture:

```python
def test_attribution_with_the_echo_off_reproduces_the_replay_exactly(corpus):
    """The spec's regression criterion: the new attribution path changes how traffic is
    grouped, never what gets identified."""
    labels, snaps = corpus
    text = labels.read_text(encoding="utf-8")
    plain = bench.score(text, snaps, replay=True)
    via = bench.score(text, snaps, replay=True, via_attribution=True)
    assert via.counts == plain.counts
    assert [r.tagged for r in via.rows] == [r.tagged for r in plain.rows]


def test_via_attribution_requires_replay(corpus):
    labels, snaps = corpus
    with pytest.raises(ValueError):
        bench.score(labels.read_text(encoding="utf-8"), snaps, via_attribution=True)
```

No existing test drives the HTTP handler's airband branch, so the branch body moves into a small function, `_postprocess_airband(raw_text, channel) -> str`, that can be tested directly. Append to `server/tests/test_whisper_proxy.py` (it already has `proxy = _load_proxy_module()` at the top):

```python
def test_an_approach_4_transmission_is_recorded_before_tagging(monkeypatch):
    recorded = []
    monkeypatch.setattr(proxy.flight_attribution, "record_transmission",
                        lambda text, channel, **_k: recorded.append((text, channel)) or 1)
    monkeypatch.setattr(proxy, "_maybe_identify_flight",
                        lambda text, channel: f"[KLM12B/B738] {text}")
    shown = proxy._postprocess_airband("KLM one two bravo, good morning", "121,200")
    assert shown == "[KLM12B/B738] KLM one two bravo, good morning"
    assert recorded == [("KLM one two bravo, good morning", "121,200")]


def test_a_tower_transmission_is_not_recorded_for_attribution(monkeypatch):
    recorded = []
    monkeypatch.setattr(proxy.flight_attribution, "record_transmission",
                        lambda text, channel, **_k: recorded.append((text, channel)) or 1)
    proxy._postprocess_airband("KLM one two bravo", "119.230")
    assert recorded == []


def test_a_failing_archive_still_returns_the_text(monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("disk gone")
    monkeypatch.setattr(proxy.flight_attribution, "record_transmission", boom)
    assert "one two bravo" in proxy._postprocess_airband("KLM one two bravo", "121.200")
```

(`record_transmission` never raises by itself; the last test pins that the call site survives even if it someday does.)

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_bench_flight_identify.py tests/test_whisper_proxy.py -q`
Expected: FAIL. `score()` has no `via_attribution`, and the proxy has no `_postprocess_airband`.

- [ ] **Step 3: Implement**

In `server/bench_flight_identify.py`:
- Add `from stt_proxy import flight_attribution` next to the existing `flight_identify` import.
- Add the parameter `via_attribution: bool = False` to `score()`, and at the top of the function add:

```python
    if via_attribution and not replay:
        raise ValueError("--via-attribution replays through the new code and needs --replay")
```

- In the replay branch, replace:

```python
                candidate = flight_identify.extract_callsign_candidate(text)
                matched = flight_identify.match_flight(candidate)
```

with:

```python
                if via_attribution:
                    clue = flight_identify.callsign_clue(text)
                    outcome = flight_attribution.combine(clue, None, echo_enabled=False)
                    candidate = clue["candidate"]
                    matched = ({"flight": clue["flight"]} if outcome["kind"] == "flight"
                               else None)
                else:
                    candidate = flight_identify.extract_callsign_candidate(text)
                    matched = flight_identify.match_flight(candidate)
```

- In `main()`, add `ap.add_argument("--via-attribution", action="store_true", help="replay through flight_attribution with the echo clue off (regression check)")`. Pass `via_attribution=args.via_attribution` to `score()`, and append `" via attribution"` to `mode` when it's set.

In `server/whisper-proxy.py`:
- Change the import line `from stt_proxy import adsb, flight_identify  # noqa: E402` to `from stt_proxy import adsb, flight_attribution, flight_identify  # noqa: E402`.
- Add this function just below `_maybe_identify_flight`:

```python
def _postprocess_airband(raw_text: str, channel: str) -> str:
    """Corrections, the Approach 4 archive, then the [FLIGHT/TYPE] tag -- in that order.

    Archived before tagging: the archive stores what was said, and the tag is a display
    decision the Airband tab makes for itself. Wrapped so a broken archive can never cost the
    plugin its transcription.
    """
    corrected = _apply_sttt_corrections(raw_text, mode="airband")
    if channel in flight_attribution.AIR_CONVERSATION_CHANNELS:
        try:
            flight_attribution.record_transmission(corrected, channel)
        except Exception as exc:
            print(f"[air] archive call failed: {type(exc).__name__}: {exc}", flush=True)
    return _maybe_identify_flight(corrected, channel)
```

- In the `elif mode == "airband":` branch, replace its first two lines:

```python
                    corrected = _apply_sttt_corrections(raw_text, mode="airband")
                    corrected = _maybe_identify_flight(corrected, channel)
```

with:

```python
                    corrected = _postprocess_airband(raw_text, channel)
```

(Keep the lines that follow, from `channel_label = ...` to `resp_body = ...`, unchanged.)
- In the startup block, inside the `else:` that calls `adsb.start(...)`, add after the print:

```python
        flight_attribution.start()
        print(f"Airband conversations: {sorted(flight_attribution.AIR_CONVERSATION_CHANNELS)}, "
              f"autopilot clue {'ON' if flight_attribution.AIR_ECHO_ENABLED else 'recorded, not shown'}",
              flush=True)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_bench_flight_identify.py tests/test_whisper_proxy.py -q`, then `py -m pytest tests -q`.
Expected: PASS.

- [ ] **Step 5: The real-corpus regression run**

From `server/`, run both and compare. The two outputs must be identical apart from the header's mode text:

```bash
py bench_flight_identify.py --labels flight-labels-2026-09-10.txt --transcripts air_shipped.json > %TEMP%\plain.txt
py bench_flight_identify.py --labels flight-labels-2026-09-10.txt --transcripts air_shipped.json --via-attribution > %TEMP%\via.txt
```

(In Git Bash, use `"$TEMP/plain.txt"`.) Expected in both: recall **43.8%** and **7** in the wrong-match bucket, the numbers recorded in the 09-11 spec. If they differ, stop and investigate before continuing. Don't adjust anything to make them match.

- [ ] **Step 6: Commit**

```bash
git add server/whisper-proxy.py server/bench_flight_identify.py server/tests/test_bench_flight_identify.py server/tests/test_whisper_proxy.py
git commit -m "Record Approach 4 transmissions for attribution, with a replay regression check"
```

---

### Task 7: Panel API: flights, conversation, moves

**Files:**
- Create: `server/webapp/air_view.py`
- Modify: `server/webapp/app.py` (new routes after `read_clip`; new `AirMoveIn` model next to `CommentIn`)
- Test: `server/tests/test_air_view.py`, `server/tests/test_app_routes.py`

**Interfaces:**
- Consumes: `air_archive.open_db/transmissions/get_transmission/add_move/valid_key` (Task 4); `clips.annotate(turns, start, root)`.
- Produces:
  - `air_view.LIVE_WINDOW_S = 900`, `MOVE_TARGET_WINDOW_S = 600`
  - `air_view.strips(rows: list[dict], now: float, live: bool) -> list[dict]`. Each strip is `{"key", "kind", "label", "airline", "type", "reg", "count", "last_epoch", "last_t", "state"}`. Order: flights by `last_epoch` descending, then `review`, then `unknown` (heard:*) by last descending, then `unassigned`.
  - `air_view.thread(rows: list[dict], key: str) -> list[dict]`. Each row is `{"id", "t", "epoch", "text", "badge", "moved", "evidence", "effective_key"}`, oldest first.
  - `air_view.move_targets(rows: list[dict], epoch: float) -> list[dict]`: `[{"key", "label"}]` for flight keys heard within ±600 s, plus `{"key": "unassigned", "label": "Unassigned"}`.
  - `air_view.evidence(row: dict) -> str`
  - `air_view.parse_range(frm: str | None, to: str | None, now: float) -> tuple[float, float, bool]`. Neither given → `(now-900, now, True)` (live); raises `ValueError` on a bad timestamp or `from > to`.
  - Routes:
    - `GET /api/air/flights?from=&to=` → `{"live", "now", "strips", "echo_enabled", "adsb": <proxy adsb feed status or null>, "error": str|null}`
    - `GET /api/air/thread?key=&from=&to=` → `{"rows": [...each with "clip", "clip_day", "targets"]}`
    - `POST /api/air/moves` with body `{"transmission_id": int, "to_key": str}` → `{"move": {...}}`; 400 for a bad key, 404 for a missing id.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_air_view.py`:

```python
import pytest

from webapp import air_view

NOW = 100_000.0


def row(tid, epoch, key, kind="flight", badge="callsign", moved=False, text="x",
        clue=None, echo=None, state=None):
    return {"id": tid, "t": f"2026-09-24T11:00:{tid:02d}+02:00", "epoch": epoch, "text": text,
            "effective_key": key, "outcome_kind": kind, "outcome_key": key, "badge": badge,
            "moved": moved, "numbers": [],
            "callsign_clue": clue or {"candidate": None, "hex": None, "flight": None,
                                      "type": None, "reg": None},
            "echo_clue": echo,
            "state": state}


KLM = {"flight": "KLM12B", "t": "B738", "r": "PH-BXH", "airline": "KLM",
       "alt_baro": 9725, "nav_altitude_mcp": 7008, "nav_heading": 51.3}
DAL = {"flight": "DAL73", "t": "B763", "r": "N183DN", "airline": "Delta",
       "alt_baro": 7200, "nav_altitude_mcp": 6016, "nav_heading": 245.0}


def test_strips_group_by_effective_key_and_sort_flights_first():
    rows = [row(1, NOW - 300, "484161", state=KLM), row(2, NOW - 100, "4bb299", state=DAL),
            row(3, NOW - 50, "unassigned", kind="unassigned", badge="none"),
            row(4, NOW - 40, "review", kind="review", badge="conflict"),
            row(5, NOW - 30, "heard:KLM1406", kind="unknown", badge="unknown"),
            row(6, NOW - 20, "484161", state=KLM)]
    got = air_view.strips(rows, NOW, live=True)
    assert [s["key"] for s in got] == ["484161", "4bb299", "review", "heard:KLM1406",
                                       "unassigned"]
    klm = got[0]
    assert klm["label"] == "KLM12B" and klm["count"] == 2 and klm["type"] == "B738"
    assert klm["state"]["nav_altitude_mcp"] == 7008
    assert got[3]["label"] == "KLM1406"


def test_a_moved_row_counts_where_it_was_moved_to():
    rows = [row(1, NOW - 10, "4bb299", moved=True, state=None),
            row(2, NOW - 20, "4bb299", state=DAL)]
    got = air_view.strips(rows, NOW, live=True)
    assert got[0]["count"] == 2 and got[0]["label"] == "DAL73"


def test_live_drops_flights_silent_for_fifteen_minutes_history_keeps_them():
    rows = [row(1, NOW - 901, "484161", state=KLM), row(2, NOW - 10, "4bb299", state=DAL)]
    assert [s["key"] for s in air_view.strips(rows, NOW, live=True)] == ["4bb299"]
    assert len(air_view.strips(rows, NOW, live=False)) == 2


def test_thread_is_oldest_first_and_explains_itself():
    rows = [row(2, NOW - 10, "484161", badge="confirmed", state=KLM,
                clue={"candidate": "KLM12B", "hex": "484161", "flight": "KLM12B",
                      "type": "B738", "reg": "PH"},
                echo={"status": "match", "hex": "484161", "flight": "KLM12B",
                      "number": {"kind": "altitude", "value": 7000, "text": "l"},
                      "delay_s": 8.0}),
            row(1, NOW - 60, "484161", state=KLM),
            row(3, NOW - 5, "4bb299", state=DAL)]
    got = air_view.thread(rows, "484161")
    assert [r["id"] for r in got] == [1, 2]
    assert "KLM12B heard" in got[1]["evidence"]
    assert "selected 7000 ft 8 s later" in got[1]["evidence"]


def test_evidence_for_a_moved_row_says_so():
    assert "moved by hand" in air_view.evidence(row(1, NOW, "484161", moved=True))


def test_move_targets_are_nearby_flights_plus_unassigned():
    rows = [row(1, NOW - 700, "aaaaaa", state={**KLM, "flight": "OLD1"}),
            row(2, NOW - 100, "484161", state=KLM),
            row(3, NOW + 100, "4bb299", state=DAL),
            row(4, NOW, "review", kind="review", badge="conflict")]
    keys = [t["key"] for t in air_view.move_targets(rows, NOW)]
    assert keys == ["484161", "4bb299", "unassigned"]


def test_parse_range():
    assert air_view.parse_range(None, None, NOW) == (NOW - 900, NOW, True)
    frm, to, live = air_view.parse_range("2026-09-24T11:00:00+02:00",
                                         "2026-09-24T12:00:00+02:00", NOW)
    assert to - frm == 3600 and live is False
    with pytest.raises(ValueError):
        air_view.parse_range("yesterday", None, NOW)
    with pytest.raises(ValueError):
        air_view.parse_range("2026-09-24T12:00:00+02:00", "2026-09-24T11:00:00+02:00", NOW)
```

Append to `server/tests/test_app_routes.py`:

```python
def _seed_air(tmp_path, epoch, key="484161", text="Descend flight level seven zero"):
    import datetime
    import air_archive
    t = datetime.datetime.fromtimestamp(epoch).astimezone().isoformat(timespec="seconds")
    with air_archive.open_db(tmp_path / "conversations.db") as conn:
        return air_archive.insert_transmission(conn, {
            "t": t, "epoch": epoch, "channel": "121.200", "text": text, "numbers": [],
            "callsign_clue": {"candidate": "KLM12B", "hex": key, "flight": "KLM12B",
                              "type": "B738", "reg": "PH-BXH"},
            "state": {"flight": "KLM12B", "t": "B738", "r": "PH-BXH", "airline": "KLM"},
            "outcome_kind": "flight", "outcome_key": key, "badge": "callsign"})


def test_air_flights_live_lists_recent_strips(client, tmp_path):
    import time
    _seed_air(tmp_path, time.time() - 30)
    body = client.get("/api/air/flights").json()
    assert body["live"] is True
    assert [s["label"] for s in body["strips"]] == ["KLM12B"]
    assert body["echo_enabled"] is False


def test_air_flights_history_range(client, tmp_path):
    _seed_air(tmp_path, 1_790_000_000.0)
    body = client.get("/api/air/flights", params={
        "from": "2026-09-21T00:00:00+00:00", "to": "2026-09-23T00:00:00+00:00"}).json()
    assert body["live"] is False and len(body["strips"]) == 1


def test_air_flights_rejects_a_bad_range(client):
    assert client.get("/api/air/flights", params={"from": "nonsense"}).status_code == 400


def test_air_thread_carries_targets_and_no_clip_without_captures(client, tmp_path):
    import time
    _seed_air(tmp_path, time.time() - 30)
    rows = client.get("/api/air/thread", params={"key": "484161"}).json()["rows"]
    assert len(rows) == 1
    assert rows[0]["clip"] is None
    assert {"key": "unassigned", "label": "Unassigned"} in rows[0]["targets"]


def test_moving_a_transmission_is_stored_and_shown(client, tmp_path):
    import time
    tid = _seed_air(tmp_path, time.time() - 30)
    body = client.post("/api/air/moves", json={"transmission_id": tid,
                                                "to_key": "unassigned"}).json()
    assert body["move"]["from_key"] == "484161"
    strips = client.get("/api/air/flights").json()["strips"]
    assert [s["key"] for s in strips] == ["unassigned"]


def test_moving_to_a_malformed_key_is_refused(client, tmp_path):
    import time
    tid = _seed_air(tmp_path, time.time() - 30)
    assert client.post("/api/air/moves", json={"transmission_id": tid,
                                                "to_key": "DROP TABLE"}).status_code == 400


def test_moving_a_missing_transmission_is_404(client):
    assert client.post("/api/air/moves", json={"transmission_id": 424242,
                                                "to_key": "unassigned"}).status_code == 404


def test_air_thread_looks_in_the_transmissions_own_capture_day(tmp_path):
    """Review Focus 5: a transmission at 00:00:30 local belongs to that day's directory."""
    import datetime
    from webapp import clips
    local = datetime.datetime(2026, 9, 24, 0, 0, 30).astimezone()
    t = local.isoformat(timespec="seconds")
    annotated = clips.annotate([{"time": t}], t, None)
    assert annotated[0]["clip"] is None   # no captures root: no clip, and no crash
    assert clips.turn_day(t, t) == "2026-09-24"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_air_view.py tests/test_app_routes.py -q`
Expected: FAIL. `air_view` doesn't exist and the `/api/air/*` routes return 404.

- [ ] **Step 3: Implement `air_view.py`**

Create `server/webapp/air_view.py`:

```python
"""The Airband tab's shape: strips per flight, one flight's thread, and where a row may move.

Pure -- reads nothing from disk. The routes in app.py fetch rows from air_archive and hand
them here, the same split conversations_view.py keeps.
"""
from __future__ import annotations

import datetime

LIVE_WINDOW_S = 900
MOVE_TARGET_WINDOW_S = 600

_SPECIAL_ORDER = {"review": 1, "unknown": 2, "unassigned": 3}


def _kind_of(key: str) -> str:
    if key == "unassigned":
        return "unassigned"
    if key == "review":
        return "review"
    if key.startswith("heard:"):
        return "unknown"
    return "flight"


def _label(key: str, state: dict | None) -> str:
    kind = _kind_of(key)
    if kind == "review":
        return "Needs review"
    if kind == "unassigned":
        return "Unassigned"
    if kind == "unknown":
        return key.split(":", 1)[1]
    return (state or {}).get("flight") or key.upper()


def parse_range(frm: str | None, to: str | None, now: float) -> tuple[float, float, bool]:
    if not frm and not to:
        return now - LIVE_WINDOW_S, now, True
    try:
        start = datetime.datetime.fromisoformat(frm).timestamp() if frm else now - 3600
        end = datetime.datetime.fromisoformat(to).timestamp() if to else now
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not an ISO-8601 time: {exc}") from None
    if start > end:
        raise ValueError("from is after to")
    return start, end, False


def strips(rows: list[dict], now: float, live: bool) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["epoch"]):
        key = r["effective_key"]
        g = groups.setdefault(key, {"key": key, "kind": _kind_of(key), "count": 0,
                                    "last_epoch": 0.0, "last_t": None, "state": None})
        g["count"] += 1
        g["last_epoch"] = r["epoch"]
        g["last_t"] = r["t"]
        if r.get("state"):
            g["state"] = r["state"]
    out = []
    for g in groups.values():
        if live and g["last_epoch"] < now - LIVE_WINDOW_S:
            continue
        state = g["state"] or {}
        g.update(label=_label(g["key"], g["state"]), airline=state.get("airline"),
                 type=state.get("t"), reg=state.get("r"))
        out.append(g)
    out.sort(key=lambda g: (_SPECIAL_ORDER.get(g["kind"], 0), -g["last_epoch"]))
    return out


def evidence(row: dict) -> str:
    if row.get("moved"):
        return "moved by hand"
    parts = []
    clue = row.get("callsign_clue") or {}
    if clue.get("flight"):
        parts.append(f"callsign {clue['flight']} heard")
    elif clue.get("candidate"):
        parts.append(f"callsign {clue['candidate']} heard, not in ADS-B")
    echo = row.get("echo_clue") or {}
    status = echo.get("status")
    if status == "match":
        n = echo.get("number") or {}
        unit = "ft" if n.get("kind") == "altitude" else "°"
        parts.append(f"{echo.get('flight') or echo.get('hex')} selected {n.get('value')} "
                     f"{unit} {round(echo.get('delay_s') or 0)} s later")
    elif status == "ambiguous":
        parts.append("autopilot: several aircraft changed to that number")
    elif status == "no_snapshots":
        parts.append("autopilot: no ADS-B data for that moment")
    elif status is None:
        parts.append("autopilot: not checked yet")
    return "; ".join(parts) or "no clue"


def thread(rows: list[dict], key: str) -> list[dict]:
    mine = sorted((r for r in rows if r["effective_key"] == key), key=lambda r: r["epoch"])
    return [{"id": r["id"], "t": r["t"], "epoch": r["epoch"], "text": r["text"],
             "badge": "moved" if r.get("moved") else r["badge"], "moved": r.get("moved", False),
             "evidence": evidence(r), "effective_key": r["effective_key"]} for r in mine]


def move_targets(rows: list[dict], epoch: float) -> list[dict]:
    """Flights heard within +/-10 minutes of `epoch`, oldest first, then Unassigned."""
    labels: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r["epoch"]):
        key = r["effective_key"]
        if _kind_of(key) != "flight" or abs(r["epoch"] - epoch) > MOVE_TARGET_WINDOW_S:
            continue
        if key not in labels or r.get("state"):
            labels[key] = _label(key, r.get("state"))
    out = [{"key": k, "label": v} for k, v in labels.items()]
    out.append({"key": "unassigned", "label": "Unassigned"})
    return out
```

- [ ] **Step 4: Implement the routes in `app.py`**

Add `import air_archive` next to `import conversation_archive`, and add `air_view` to the existing `from webapp import ...` line (keep it alphabetical). Next to `class CommentIn`, add:

```python
class AirMoveIn(BaseModel):
    transmission_id: int
    to_key: str
```

After `read_clip`, add:

```python
    def _air_rows(start: float, end: float) -> list[dict]:
        with air_archive.open_db(_archive_db()) as conn:
            return air_archive.transmissions(conn, start, end)

    @guarded.get("/api/air/flights")
    def read_air_flights(from_: str | None = Query(None, alias="from"),
                         to: str | None = None) -> dict:
        now = time.time()
        try:
            start, end, live = air_view.parse_range(from_, to, now)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        proxy, _err = health_module.proxy_status(values())
        body = {"live": live, "now": now, "strips": [], "error": None,
                "echo_enabled": (values().get("AIR_ECHO_ENABLED") or "off") == "on",
                "adsb": (proxy or {}).get("adsb")}
        try:
            body["strips"] = air_view.strips(_air_rows(start, end), now, live)
        except Exception as exc:
            body["error"] = f"the airband archive could not be read ({type(exc).__name__})"
        return body

    @guarded.get("/api/air/thread")
    def read_air_thread(key: str, from_: str | None = Query(None, alias="from"),
                        to: str | None = None) -> dict:
        now = time.time()
        try:
            start, end, _live = air_view.parse_range(from_, to, now)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        # Wide enough that move targets near the edges of the range are still offered.
        rows = _air_rows(start - air_view.MOVE_TARGET_WINDOW_S,
                         end + air_view.MOVE_TARGET_WINDOW_S)
        in_range = [r for r in rows if start <= r["epoch"] <= end]
        root = _captures_root()
        out = []
        for item in air_view.thread(in_range, key):
            clip = clips.annotate([{"time": item["t"]}], item["t"], root)[0]
            item["clip"], item["clip_day"] = clip["clip"], clip["clip_day"]
            item["time"] = item["t"]
            item["targets"] = [t for t in air_view.move_targets(rows, item["epoch"])
                               if t["key"] != key]
            out.append(item)
        return {"rows": out}

    @mutating.post("/api/air/moves")
    def write_air_move(body: AirMoveIn) -> dict:
        if not air_archive.valid_key(body.to_key):
            raise HTTPException(status_code=400, detail="not a flight key")
        with air_archive.open_db(_archive_db()) as conn:
            move = air_archive.add_move(conn, body.transmission_id, body.to_key)
        if move is None:
            raise HTTPException(status_code=404, detail="no such transmission")
        return {"move": move}
```

`app.py` imports neither of these yet: add `import time` to the stdlib imports and `Query` to the existing `from fastapi import (...)` line. `health_module` is already imported (`health as health_module`), and `health_module.proxy_status(values)` takes the settings dict and returns `(payload, error)`.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -m pytest tests/test_air_view.py tests/test_app_routes.py tests/test_app_auth.py -q`
Expected: PASS. `test_app_auth.py::test_every_mutating_route_rejects_a_request_with_no_session` now covers `POST /api/air/moves` automatically.

- [ ] **Step 6: Commit**

```bash
git add server/webapp/air_view.py server/webapp/app.py server/tests/test_air_view.py server/tests/test_app_routes.py
git commit -m "Serve Approach 4 flights, threads and moves to the control panel"
```

---

### Task 8: The Airband tab

**Files:**
- Create: `server/webapp/static/air.js`
- Modify: `server/webapp/static/index.html` (tab button after Conversations; `<main id="airband">` after `</main>` of conversations; `<script src="/static/air.js">` after `app.js`)
- Modify: `server/webapp/static/app.js` (`showTab`, `tick`, `startPolling`)
- Modify: `server/webapp/static/app.css` (append the air styles)
- Test: `server/tests/test_app_routes.py` (static serving) plus the manual browser check in Step 5

**Interfaces:**
- Consumes: the three `/api/air/*` routes (Task 7); `api()`, `element()`, `$()` and `renderTurnAudio(turn)` from `app.js`. `renderTurnAudio` reads `turn.clip`, `turn.clip_day` and `turn.time`, which the thread rows carry.
- Produces: `refreshAirband()` (global, called by `tick()`).

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_app_routes.py`:

```python
def test_the_airband_tab_is_served(client):
    html = client.get("/").text
    assert 'data-tab="airband"' in html and 'id="airband"' in html
    assert "/static/air.js" in html
    assert client.get("/static/air.js").status_code == 200
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `py -m pytest tests/test_app_routes.py -q -k airband_tab`
Expected: FAIL.

- [ ] **Step 3: Implement**

In `index.html`, after the Conversations tab button:

```html
      <button class="tab" role="tab" data-tab="airband" aria-selected="false">Airband</button>
```

After the conversations `</main>` (line ~147):

```html
  <main id="airband" class="view" hidden>
    <div class="air-bar">
      <button class="air-pill" type="button" data-range="live" aria-pressed="true">● Live</button>
      <button class="air-pill" type="button" data-range="hour" aria-pressed="false">Last hour</button>
      <label class="air-pick">Day/hour
        <input id="air-day" type="date"> <input id="air-hour" type="number" min="0" max="23" step="1">
      </label>
      <span id="air-status" class="air-status"></span>
    </div>
    <p id="air-error" class="note note-port" hidden></p>
    <div class="air-layout">
      <ul id="air-strips" class="air-strips" aria-label="Flights"></ul>
      <section id="air-thread" class="air-thread" aria-live="polite">
        <p class="conv-empty">Pick a flight on the left.</p>
      </section>
    </div>
  </main>
```

After `<script src="/static/app.js"></script>`:

```html
<script src="/static/air.js"></script>
```

In `app.js`, in `showTab`, add `$("airband").hidden = name !== "airband";`. In `tick()`, add `else if (state.tab === "airband") refreshAirband().catch(() => {});` after the conversations line. In `startPolling()`, add:

```javascript
  // Airband: the proxy archives a transmission the moment it arrives, and stage 2 fills in
  // the autopilot clue a minute later -- 15 s matches the ADS-B poll.
  state.timers.push(setInterval(() => { if (state.tab === "airband") tick(); }, 15000));
```

Create `server/webapp/static/air.js`:

```javascript
/* The Airband tab: Approach 4 transmissions grouped per flight.
 *
 * Left: strips, one per flight (plus Needs review, unknown callsigns and Unassigned).
 * Right: the selected strip's transmissions, each with its clue badge, audio and "move to".
 * Uses api(), element(), $() and renderTurnAudio() from app.js.
 */
"use strict";

const airState = { range: "live", day: "", hour: "", selected: null };

const AIR_BADGES = {
  confirmed: ["✔✔", "callsign and autopilot agree"],
  callsign: ["✔", "callsign heard"],
  echo: ["✔", "autopilot change only"],
  conflict: ["⚠", "callsign and autopilot disagree"],
  unknown: ["?", "callsign heard, not in ADS-B"],
  none: ["—", "no clue"],
  moved: ["✎", "moved by hand"],
};

function airRangeParams() {
  if (airState.range === "live") return "";
  const now = new Date();
  let from;
  let to;
  if (airState.range === "hour") {
    to = now;
    from = new Date(now.getTime() - 3600 * 1000);
  } else {
    const [y, m, d] = airState.day.split("-").map(Number);
    from = new Date(y, m - 1, d, Number(airState.hour) || 0, 0, 0);
    to = new Date(from.getTime() + 3600 * 1000);
  }
  const iso = (dt) => {
    const off = -dt.getTimezoneOffset();
    const sign = off >= 0 ? "+" : "-";
    const pad = (n) => String(Math.floor(Math.abs(n))).padStart(2, "0");
    return `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T` +
      `${pad(dt.getHours())}:${pad(dt.getMinutes())}:${pad(dt.getSeconds())}` +
      `${sign}${pad(off / 60)}:${pad(off % 60)}`;
  };
  return `from=${encodeURIComponent(iso(from))}&to=${encodeURIComponent(iso(to))}`;
}

function airFeet(value) {
  if (value === null || value === undefined || value === "ground") return value || "—";
  return value >= 10000 ? `FL${Math.round(value / 100)}` : `${Math.round(value)}`;
}

function airStateLine(s) {
  if (!s) return "";
  const parts = [];
  if (s.alt_baro !== undefined && s.alt_baro !== null) {
    parts.push(s.nav_altitude_mcp ? `${airFeet(s.alt_baro)} → set ${airFeet(s.nav_altitude_mcp)}`
                                  : airFeet(s.alt_baro));
  }
  if (typeof s.nav_heading === "number") parts.push(`hdg ${String(Math.round(s.nav_heading)).padStart(3, "0")}`);
  return parts.join(" · ");
}

function airAgo(nowEpoch, epoch) {
  return elapsed(nowEpoch - epoch);
}

function renderAirStrips(body) {
  const list = $("air-strips");
  list.replaceChildren();
  if (!body.strips.length) {
    list.append(element("li", "conv-empty",
      body.live ? "Nothing on Approach 4 in the last 15 minutes." : "Nothing in this period."));
    return;
  }
  for (const strip of body.strips) {
    const li = element("li", `air-strip air-${strip.kind}`);
    li.tabIndex = 0;
    li.setAttribute("aria-selected", String(strip.key === airState.selected));
    const head = element("div", "air-strip-head");
    head.append(element("b", "", strip.kind === "unknown" ? `? “${strip.label}”` : strip.label));
    const meta = [strip.airline, strip.type].filter(Boolean).join(" · ");
    if (meta) head.append(element("span", "air-meta", ` ${meta}`));
    head.append(element("span", "air-ago", airAgo(body.now, strip.last_epoch)));
    li.append(head);
    const line = strip.kind === "unknown" ? `heard ${strip.count}× · not in ADS-B`
      : [airStateLine(strip.state), `${strip.count} tx`].filter(Boolean).join(" · ");
    li.append(element("div", "air-sub", line));
    const pick = () => { airState.selected = strip.key; refreshAirband().catch(() => {}); };
    li.addEventListener("click", pick);
    li.addEventListener("keydown", (e) => { if (e.key === "Enter") pick(); });
    list.append(li);
  }
}

async function moveAirRow(row, toKey) {
  await api("/api/air/moves", { method: "POST",
    body: JSON.stringify({ transmission_id: row.id, to_key: toKey }) });
  await refreshAirband();
}

function renderAirThread(strip, rows) {
  const box = $("air-thread");
  box.replaceChildren();
  if (!strip) {
    box.append(element("p", "conv-empty", "Pick a flight on the left."));
    return;
  }
  const head = element("p", "air-thread-head");
  head.append(element("b", "", strip.label));
  const extra = [strip.airline, strip.type, strip.reg, airStateLine(strip.state)].filter(Boolean);
  if (extra.length) head.append(element("span", "air-meta", ` · ${extra.join(" · ")}`));
  box.append(head);

  for (const row of rows) {
    const [mark, word] = AIR_BADGES[row.badge] || AIR_BADGES.none;
    const line = element("div", `air-row air-badge-${row.badge}`);
    line.append(element("span", "air-time", row.t.slice(11, 19)));
    const badge = element("span", "air-badge", mark);
    badge.title = `${word}: ${row.evidence}`;
    line.append(badge);
    line.append(element("span", "air-text", row.text));
    const controls = element("span", "air-controls");
    const audio = renderTurnAudio(row);
    if (audio) controls.append(audio);
    const select = document.createElement("select");
    select.className = "air-move";
    select.setAttribute("aria-label", "Move this transmission to another flight");
    select.append(new Option("move to…", ""));
    for (const target of row.targets) select.append(new Option(target.label, target.key));
    select.addEventListener("change", () => {
      if (select.value) moveAirRow(row, select.value).catch((e) => showAirError(e.message));
    });
    controls.append(select);
    line.append(controls);
    box.append(line);
  }
}

function showAirError(message) {
  const note = $("air-error");
  note.textContent = message || "";
  note.hidden = !message;
}

function renderAirStatus(body) {
  const feed = body.adsb;
  const feedText = !feed ? "ADS-B: proxy not answering"
    : feed.consecutive_failures ? `ADS-B: failing (${feed.consecutive_failures})`
    : `ADS-B: OK · ${feed.last_count ?? "?"} aircraft`;
  $("air-status").textContent =
    `${feedText} · autopilot clue: ${body.echo_enabled ? "on" : "recording, not shown"}`;
}

async function refreshAirband() {
  const params = airRangeParams();
  const body = await api(`/api/air/flights${params ? `?${params}` : ""}`);
  showAirError(body.error);
  renderAirStatus(body);
  if (airState.selected && !body.strips.some((s) => s.key === airState.selected)) {
    airState.selected = null;
  }
  renderAirStrips(body);
  const strip = body.strips.find((s) => s.key === airState.selected);
  if (!strip) {
    renderAirThread(null, []);
    return;
  }
  const extra = params ? `&${params}` : "";
  const thread = await api(`/api/air/thread?key=${encodeURIComponent(strip.key)}${extra}`);
  renderAirThread(strip, thread.rows);
}

function setAirRange(range) {
  airState.range = range;
  for (const pill of document.querySelectorAll(".air-pill")) {
    pill.setAttribute("aria-pressed", String(pill.dataset.range === range));
  }
  refreshAirband().catch((e) => showAirError(e.message));
}

for (const pill of document.querySelectorAll(".air-pill")) {
  pill.addEventListener("click", () => setAirRange(pill.dataset.range));
}
for (const id of ["air-day", "air-hour"]) {
  $(id).addEventListener("change", () => {
    airState.day = $("air-day").value;
    airState.hour = $("air-hour").value;
    if (airState.day) setAirRange("pick");
  });
}
```

Append to `app.css`. Use the file's existing custom properties. First check the names with `grep -n "^  --" webapp/static/app.css | head -30`, and replace `var(--line)`, `var(--muted)`, `var(--accent)` and `var(--warn)` below with the file's actual equivalents if they're named differently:

```css
/* -- Airband tab ---------------------------------------------------------- */
.air-bar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 10px; }
.air-pill { border: 1px solid var(--line); border-radius: 999px; padding: 2px 10px;
            background: transparent; color: inherit; cursor: pointer; }
.air-pill[aria-pressed="true"] { background: var(--accent); color: #fff; border-color: var(--accent); }
.air-pick { display: inline-flex; gap: 4px; align-items: center; font-size: 0.9em; }
.air-pick input[type="number"] { width: 4em; }
.air-status { margin-left: auto; font-size: 0.85em; color: var(--muted); }
.air-layout { display: grid; grid-template-columns: minmax(220px, 34%) 1fr; gap: 14px; }
@media (max-width: 720px) { .air-layout { grid-template-columns: 1fr; } }
.air-strips { list-style: none; margin: 0; padding: 0; }
.air-strip { border-left: 4px solid var(--accent); padding: 6px 8px; margin: 4px 0;
             border-radius: 3px; cursor: pointer; background: color-mix(in srgb, currentColor 5%, transparent); }
.air-strip[aria-selected="true"] { outline: 2px solid var(--accent); }
.air-strip.air-review { border-left-color: var(--warn); }
.air-strip.air-unknown, .air-strip.air-unassigned { border-left-color: var(--muted); }
.air-strip-head { display: flex; gap: 6px; align-items: baseline; }
.air-ago { margin-left: auto; font-size: 0.8em; color: var(--muted); }
.air-meta, .air-sub { font-size: 0.85em; color: var(--muted); }
.air-sub { font-family: ui-monospace, monospace; }
.air-thread-head { margin: 0 0 8px; }
.air-row { display: flex; gap: 8px; align-items: flex-start; padding: 6px 8px; margin: 4px 0;
           border-radius: 3px; background: color-mix(in srgb, currentColor 5%, transparent); }
.air-time { font-family: ui-monospace, monospace; color: var(--muted); white-space: nowrap; }
.air-badge { white-space: nowrap; cursor: help; }
.air-badge-conflict .air-badge { color: var(--warn); }
.air-text { flex: 1; }
.air-controls { display: inline-flex; gap: 6px; align-items: center; white-space: nowrap; }
```

- [ ] **Step 4: Run the tests**

Run: `py -m pytest tests/test_app_routes.py tests/test_app_auth.py -q`, then `py -m pytest tests -q`.
Expected: PASS.

- [ ] **Step 5: Browser check (operator confirmation required)**

Ask the operator before restarting. Restart the control panel and the proxy through the panel, then open the panel and sign in. Check each of these and report what you saw:
- The **Airband** tab appears between Conversations and Vessels.
- With SDR# on 121.200, strips appear within ~15 s of a transmission. The status line reads "autopilot clue: recording, not shown".
- Clicking a strip shows its transmissions. Hovering a badge shows the evidence. After ~60 s the evidence text changes from "autopilot: not checked yet" to one of the stage-2 outcomes.
- ▶ plays audio when capture is on.
- "move to… → Unassigned" moves the row, and it stays moved after a page reload.
- Last hour and Day/hour (today, current hour) show the same data.
- Resize to phone width: the strips stack above the thread, with no horizontal scroll.

- [ ] **Step 6: Commit**

```bash
git add server/webapp/static/air.js server/webapp/static/index.html server/webapp/static/app.js server/webapp/static/app.css server/tests/test_app_routes.py
git commit -m "Add the Airband tab to the control panel"
```

---

### Task 9: The switch-on gate: `bench_air_echo.py`

**Files:**
- Create: `server/bench_air_echo.py`
- Test: `server/tests/test_bench_air_echo.py`

**Interfaces:**
- Consumes: `air_archive.open_db`, `air_archive.transmissions`.
- Produces:
  - `bench_air_echo.gate(rows: list[dict]) -> dict`, with keys `overlap`, `agree`, `agreement` (float|None), `moved`, `echo_right_on_moves`, `callsign_right_on_moves`, `unassigned`, `echo_recovers`, `recovery` (float|None), `passes` (bool), `reasons` (list[str]).
  - CLI: `py bench_air_echo.py [--db PATH] [--from ISO] [--to ISO]`

The three conditions come from the spec:
1. `overlap` = rows where the callsign clue has a `hex` AND `echo_clue.status == "match"`. `agreement` = `agree / overlap`. Needs `agreement >= 0.95` **and** `overlap >= 50`.
2. On moved rows (`moved` is true and `effective_key` is a flight key), count how often `echo_clue.hex == effective_key` and how often `callsign_clue.hex == effective_key`. Needs echo ≥ callsign.
3. `unassigned` = rows whose **callsign-only** outcome would be unassigned (no candidate, no hex). `echo_recovers` = those with echo status `match`. Needs `recovery = echo_recovers / unassigned >= 0.10`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_bench_air_echo.py`:

```python
import bench_air_echo as bench


def r(cs_hex=None, cand=None, echo=None, moved=False, key="unassigned"):
    return {"callsign_clue": {"candidate": cand or (cs_hex and "X"), "hex": cs_hex},
            "echo_clue": {"status": "match", "hex": echo} if echo else {"status": "none"},
            "moved": moved, "effective_key": key}


def test_a_clean_pass():
    rows = [r(cs_hex="a1", echo="a1", key="a1") for _ in range(50)]
    rows += [r(echo="b2") for _ in range(2)] + [r() for _ in range(8)]
    rows += [r(cs_hex="a1", echo="c3", moved=True, key="c3")]
    got = bench.gate(rows)
    assert got["overlap"] == 51 and got["agree"] == 50
    assert got["agreement"] >= 0.95
    assert got["echo_right_on_moves"] == 1 and got["callsign_right_on_moves"] == 0
    assert got["recovery"] == 0.2
    assert got["passes"] is True and got["reasons"] == []


def test_too_few_overlaps_fails_even_at_perfect_agreement():
    rows = [r(cs_hex="a1", echo="a1", key="a1") for _ in range(49)] + [r(echo="b2")] * 5
    got = bench.gate(rows)
    assert got["passes"] is False
    assert any("50" in reason for reason in got["reasons"])


def test_disagreement_fails():
    rows = ([r(cs_hex="a1", echo="a1", key="a1")] * 45 + [r(cs_hex="a1", echo="zz")] * 5
            + [r(echo="b2")] * 5)
    assert bench.gate(rows)["passes"] is False


def test_echo_worse_than_callsign_on_moves_fails():
    rows = [r(cs_hex="a1", echo="a1", key="a1")] * 60 + [r(echo="b2")] * 5
    rows += [r(cs_hex="k1", echo="zz", moved=True, key="k1")]
    got = bench.gate(rows)
    assert got["passes"] is False


def test_no_data_is_not_a_pass():
    got = bench.gate([])
    assert got["passes"] is False and got["agreement"] is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_bench_air_echo.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `server/bench_air_echo.py`:

```python
"""Has the autopilot clue earned AIR_ECHO_ENABLED=on? Three conditions, all required.

Reads the airband archive the proxy has been filling in shadow mode. See the spec's
"Measuring the autopilot clue" section:
docs/superpowers/specs/2026-09-24-airband-conversations-design.md.

Usage:
    py bench_air_echo.py                       # everything archived so far
    py bench_air_echo.py --from 2026-09-25T00:00:00+02:00 --to 2026-09-29T00:00:00+02:00
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVER_DIR))

import air_archive  # noqa: E402
import conversation_archive  # noqa: E402

MIN_OVERLAP = 50
MIN_AGREEMENT = 0.95
MIN_RECOVERY = 0.10


def _echo_hex(row: dict) -> str | None:
    echo = row.get("echo_clue") or {}
    return echo.get("hex") if echo.get("status") == "match" else None


def _is_flight_key(key: str) -> bool:
    return key not in ("unassigned", "review") and not key.startswith("heard:")


def gate(rows: list[dict]) -> dict:
    overlap = agree = 0
    moved = echo_right = cs_right = 0
    unassigned = recovers = 0
    for row in rows:
        cs = row.get("callsign_clue") or {}
        echo_hex = _echo_hex(row)
        if cs.get("hex") and echo_hex:
            overlap += 1
            agree += cs["hex"] == echo_hex
        if row.get("moved") and _is_flight_key(row.get("effective_key") or ""):
            moved += 1
            echo_right += echo_hex == row["effective_key"]
            cs_right += cs.get("hex") == row["effective_key"]
        if not cs.get("candidate") and not cs.get("hex"):
            unassigned += 1
            recovers += echo_hex is not None

    agreement = agree / overlap if overlap else None
    recovery = recovers / unassigned if unassigned else None
    reasons = []
    if overlap < MIN_OVERLAP:
        reasons.append(f"only {overlap} transmissions have both clues; need {MIN_OVERLAP}")
    if agreement is None or agreement < MIN_AGREEMENT:
        reasons.append(f"agreement with the callsign is {agreement}; need >= {MIN_AGREEMENT}")
    if echo_right < cs_right:
        reasons.append(f"on hand-moved rows the echo was right {echo_right}x, "
                       f"the callsign {cs_right}x")
    if recovery is None or recovery < MIN_RECOVERY:
        reasons.append(f"recovers {recovery} of unassigned; need >= {MIN_RECOVERY}")
    return {"overlap": overlap, "agree": agree, "agreement": agreement, "moved": moved,
            "echo_right_on_moves": echo_right, "callsign_right_on_moves": cs_right,
            "unassigned": unassigned, "echo_recovers": recovers, "recovery": recovery,
            "passes": not reasons, "reasons": reasons}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    args = ap.parse_args(argv)
    db = conversation_archive.resolve_db_path(args.db, _SERVER_DIR)
    start = datetime.datetime.fromisoformat(args.frm).timestamp() if args.frm else 0.0
    end = datetime.datetime.fromisoformat(args.to).timestamp() if args.to else 4e9
    with air_archive.open_db(db) as conn:
        rows = air_archive.transmissions(conn, start, end)
    checked = [r for r in rows if r.get("echo_clue")]
    statuses: dict[str, int] = {}
    for r in checked:
        statuses[r["echo_clue"]["status"]] = statuses.get(r["echo_clue"]["status"], 0) + 1
    result = gate(checked)
    print(f"{len(rows)} transmissions, {len(checked)} with stage 2 done   db: {db}")
    print(f"echo status: {statuses}")
    print(f"1. agreement  {result['agree']}/{result['overlap']} = {result['agreement']}")
    print(f"2. moves      echo {result['echo_right_on_moves']} vs callsign "
          f"{result['callsign_right_on_moves']} of {result['moved']}")
    print(f"3. recovery   {result['echo_recovers']}/{result['unassigned']} = {result['recovery']}")
    print("PASS -- AIR_ECHO_ENABLED may be switched on" if result["passes"]
          else "FAIL -- keep it off:\n  " + "\n  ".join(result["reasons"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_bench_air_echo.py -q`, then `py -m pytest tests -q`.
Expected: PASS.

- [ ] **Step 5: Smoke-run on the real archive**

Run: `py bench_air_echo.py`. This only **reads** the real archive, which the CLI is allowed to do; the tests are what may not touch it. Expected: it prints counts, which will be near zero until airband traffic has been recorded, and `FAIL -- keep it off`. That's the correct result at this stage.

- [ ] **Step 6: Commit**

```bash
git add server/bench_air_echo.py server/tests/test_bench_air_echo.py
git commit -m "Add the autopilot-clue switch-on gate"
```

---

### Task 10: Documentation, full verification, handover

**Files:**
- Modify: `docs/user-manual.md` (new section "The Airband tab", placed after the Conversations tab section)
- Modify: `docs/superpowers/specs/2026-09-24-airband-conversations-design.md` (Status line, success-criteria checkboxes)

- [ ] **Step 1: Write the manual section**

Add a section to `docs/user-manual.md` covering:
- **What the tab is for:** Approach 4 grouped per flight.
- **The time bar.**
- **What a strip shows:** the altitude → selected altitude, heading and count, and in history mode, "values at the flight's last transmission".
- **The badges** (✔✔ ✔ ⚠ ? — ✎) and hover for evidence.
- **How to use "move to…"**, and that every move is kept and feeds the measurement.
- **The autopilot clue:** it's recorded but not shown until `py bench_air_echo.py` prints PASS; to switch it on, go to Settings → Identification → `AIR_ECHO_ENABLED` and restart the proxy.
- **Where the data lives:** `conversations.db` tables `air_transmissions` / `air_moves`, and `logs/adsb-YYYY-MM-DD.jsonl`.

Keep the manual's existing tone and heading levels (read the Conversations section first and match it).

- [ ] **Step 2: Full verification**

Run: `py -m pytest tests -q` from `server/`. Expected: all pass, and the count has risen by the number of tests added in Tasks 1–9. Then run the 09-10 regression pair from Task 6 Step 5 once more and confirm 43.8% / 7 wrong in both.

- [ ] **Step 3: Tick the spec's success criteria**

In the spec, tick each success criterion that has been verified, and change the Status line to `IMPLEMENTED <date>, autopilot clue in shadow mode; gate run due after 3-5 days of traffic`. Leave the `bench_air_echo.py` gate decision unticked, since it's a later run.

- [ ] **Step 4: Commit**

```bash
git add docs/user-manual.md docs/superpowers/specs/2026-09-24-airband-conversations-design.md
git commit -m "Document the Airband tab and record the release state"
```

- [ ] **Step 5: Hand over to finishing-a-development-branch**

Use `superpowers:finishing-a-development-branch` to decide between merging `feat/airband-conversations` into master and opening a PR. CI must be green before any push.
