# Airband Prompt Biasing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Measure whether editing the static airband decoding prompt stops the decoder writing "QNH" where "KLM" was spoken, and report the effect on flight identification.

**Architecture:** Six transcription arms over the same 136 captured clips, run through the existing `bench_stt.py` (which calls `stt_proxy.backends.transcribe` directly, the same function the proxy calls). Four small pieces of new code make the arms scoreable: airband prompt variants in the prompt registry, a converter turning the labelling worksheet into a WER reference file, a `--transcripts` mode on the identification scorer so it can score re-transcribed text, and a reference-free word-production counter. No production code changes in this plan — the prompt constant is only edited after the measurement says which variant wins.

**Tech Stack:** Python 3.14, pytest, Groq `whisper-large-v3` via `stt_proxy.backends`, existing `bench.py` / `bench_stt.py` / `bench_prompt_ab.py` / `bench_flight_identify.py`.

**Spec:** `docs/superpowers/specs/2026-09-11-airband-prompt-biasing-design.md`

## Global Constraints

- Run every command from `D:\Claudecode\projects\SDRSharp-Plugin\server`. The test suite and all bench scripts assume that working directory.
- Python is invoked as `py` on this machine, not `python3`.
- `GROQ_PROMPT_MAX_WORDS` is **140**. Any prompt longer than that is a hard 400 from Groq and would cost a whole arm. The shipped airband prompt is **92 words**, leaving 48.
- The production backend is `STT_BACKEND=groq`, `GROQ_MODEL=whisper-large-v3` (from `server/config.json`). Do not change either.
- Clip ids are the capture filename minus `_sent.wav`, i.e. four zero-padded digits: worksheet index `18` is clip id `"0018"`.
- **Do not modify `server/stt_proxy/` in any task in this plan.** The prompt edit is a separate decision that follows the measurement.
- **Do not restart the proxy.** It is carrying live radio traffic; the bench scripts do not need it.
- The corpus worksheet is `server/flight-labels-2026-09-10.txt` and is gitignored. Never `git add` it.
- Every task ends green on the full suite: `py -m pytest tests/ -q`.
- The `for ... do ... done` loops in Task 5 are **bash** syntax (use the Bash tool / Git
  Bash). PowerShell is this machine's default shell and will not parse them; run the
  commands one at a time there instead.

---

### Task 1: Airband prompt variants in the registry

`bench.PROMPTS` is the registry `bench_stt.py --prompt` selects from. Every entry in it today is a maritime prompt; this adds the five airband arms. `bench_stt.py` passes the selected text explicitly as `client_prompt`, which wins over the per-mode default in `backends._effective_prompt`, so no change to `bench_stt.py` is needed.

**Files:**
- Modify: `server/bench.py` (add constants next to the existing `NO_NAMES_PROMPT`, then extend the `PROMPTS` dict)
- Test: `server/tests/test_bench.py`

**Interfaces:**
- Consumes: `stt_proxy.backends.DEFAULT_AVIATION_PROMPT`, `stt_proxy.backends.GROQ_PROMPT_MAX_WORDS`
- Produces: `bench.PROMPTS` keys `"air_shipped"`, `"air_no_qnh"`, `"air_airlines"`, `"air_both"`, `"air_empty"`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_bench.py`:

```python
class TestAirbandPromptVariants:
    """The airband arms for the prompt-biasing measurement. The word-cap assertion is not
    ceremony: Groq rejects an over-long prompt with a hard 400, which would silently cost a
    whole arm of 136 transcriptions."""

    AIR_ARMS = ("air_shipped", "air_no_qnh", "air_airlines", "air_both", "air_empty")

    def test_every_airband_arm_is_registered(self):
        for name in self.AIR_ARMS:
            assert name in bench.PROMPTS, name

    def test_every_airband_arm_fits_the_groq_word_cap(self):
        from stt_proxy.backends import GROQ_PROMPT_MAX_WORDS
        for name in self.AIR_ARMS:
            words = len(bench.PROMPTS[name].split())
            assert words <= GROQ_PROMPT_MAX_WORDS, f"{name} is {words} words"

    def test_air_shipped_is_exactly_what_the_proxy_sends_today(self):
        from stt_proxy.backends import DEFAULT_AVIATION_PROMPT
        assert bench.PROMPTS["air_shipped"] == DEFAULT_AVIATION_PROMPT

    def test_air_no_qnh_drops_the_token_and_keeps_the_reading(self):
        """Isolate one variable: the pressure reading stays, only the initialism goes."""
        prompt = bench.PROMPTS["air_no_qnh"]
        assert "QNH" not in prompt
        assert "one zero one three" in prompt

    def test_air_airlines_keeps_the_shipped_text_and_adds_operators(self):
        from stt_proxy.backends import DEFAULT_AVIATION_PROMPT
        prompt = bench.PROMPTS["air_airlines"]
        assert DEFAULT_AVIATION_PROMPT in prompt
        for operator in ("KLM", "Transavia", "Delta", "American", "United",
                         "JetBlue", "Orange", "Speedbird"):
            assert operator in prompt, operator

    def test_air_both_applies_both_edits(self):
        prompt = bench.PROMPTS["air_both"]
        assert "QNH" not in prompt
        assert "KLM" in prompt

    def test_air_empty_sends_no_prompt_at_all(self):
        assert bench.PROMPTS["air_empty"] == ""
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_bench.py::TestAirbandPromptVariants -q`
Expected: FAIL — `KeyError: 'air_shipped'` / assertion errors on `"air_shipped" in bench.PROMPTS`.

- [ ] **Step 3: Write the minimal implementation**

In `server/bench.py`, after the existing `NO_NAMES_PROMPT` definition and before the `PROMPTS` dict, add:

```python
# -- airband arms for the 2026-09-11 prompt-biasing measurement -------------------------
#
# The shipped airband prompt over-produces its own vocabulary and contains no airline name at
# all: measured over the 2026-09-10 labelled corpus the decoder wrote QNH 27 times against the
# operator's 12, and KLM 5 times against 15, writing QNH in the exact slot where KLM was spoken
# in four transmissions. These arms separate "QNH is over-primed" from "KLM is absent".

AIR_SHIPPED_PROMPT = backends.DEFAULT_AVIATION_PROMPT

# Only the initialism is removed; the pressure reading stays, so exactly one thing changes.
AIR_NO_QNH_PROMPT = AIR_SHIPPED_PROMPT.replace(
    "QNH one zero one three", "one zero one three")

# Operators actually observed on 121.205, in phraseology rather than as a word list -- the
# prompt is read as prior speech, so a bare list of names is not the thing being tested.
# 41 words, which is what fits: the shipped prompt is 92 and the Groq cap is 140. Shamrock
# (Aer Lingus) was cut for the budget; swap it in for a rarer operator if a later corpus
# says it matters more than one of these.
_AIRLINE_PHRASES = (
    " KLM one two three four, descend flight level seven zero. "
    "Transavia six seven eight nine, roger. "
    "Delta seven three, American two zero three, United nine four seven, "
    "JetBlue three two, Orange three six seven, Speedbird four three zero, contact Tower."
)

AIR_AIRLINES_PROMPT = AIR_SHIPPED_PROMPT + _AIRLINE_PHRASES
AIR_BOTH_PROMPT = AIR_NO_QNH_PROMPT + _AIRLINE_PHRASES
AIR_EMPTY_PROMPT = ""
```

Then extend the `PROMPTS` dict with:

```python
    "air_shipped": AIR_SHIPPED_PROMPT,    # today's airband prompt; the control
    "air_no_qnh": AIR_NO_QNH_PROMPT,      # is QNH over-primed?
    "air_airlines": AIR_AIRLINES_PROMPT,  # is KLM's absence the problem?
    "air_both": AIR_BOTH_PROMPT,          # do the two edits compose?
    "air_empty": AIR_EMPTY_PROMPT,        # does the prompt help at all?
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_bench.py -q`
Expected: PASS, all of them.

- [ ] **Step 5: Run the full suite**

Run: `py -m pytest tests/ -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/bench.py server/tests/test_bench.py
git commit -m "Register the airband prompt arms"
```

---

### Task 2: Turn the labelling worksheet into a WER reference file

`bench.load_references` reads `clip_id<TAB>text` lines. The worksheet's hand-corrected `heard` lines are the only ear-verified airband reference set that exists, so this converts one into the other. It lives in `make_flight_labels.py` because that module owns the worksheet format.

**Files:**
- Modify: `server/make_flight_labels.py` (add `to_references`, and a `--references` output flag to `main`)
- Test: `server/tests/test_make_flight_labels.py`

**Interfaces:**
- Consumes: `make_flight_labels.parse_worksheet`, `make_flight_labels.render_worksheet`
- Produces: `make_flight_labels.to_references(worksheet: str) -> str`

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_make_flight_labels.py`:

```python
class TestReferenceExport:
    """The worksheet's hand-corrected `heard` lines are the only ear-verified airband
    reference set there is. bench.load_references wants clip_id<TAB>text."""

    def _sheet(self, heard: str) -> str:
        rows = [{"index": 18, "timestamp": "2026-09-10T11:00:00+02:00",
                 "channel": "121,205", "durationSec": 2.5, "text": "machine text"}]
        sheet = make_flight_labels.render_worksheet(rows)
        return sheet.replace("heard    : machine text", f"heard    : {heard}")

    def test_a_corrected_line_is_exported_against_its_clip_id(self):
        out = make_flight_labels.to_references(self._sheet("KLM one two bravo."))
        assert out.splitlines() == ["0018\tKLM one two bravo."]

    def test_a_question_mark_becomes_an_inaudible_marker(self):
        """bench._normalize already strips [bracketed] markers, so an unintelligible word
        costs nothing instead of counting as a wrong word against every arm equally."""
        out = make_flight_labels.to_references(self._sheet("Approach, ? good day."))
        assert out.splitlines() == ["0018\tApproach, [inaudible] good day."]

    def test_an_unlabelled_clip_exports_no_reference_line(self):
        assert make_flight_labels.to_references(self._sheet("")) == ""

    def test_a_tab_inside_the_text_cannot_break_the_format(self):
        out = make_flight_labels.to_references(self._sheet("one\ttwo"))
        assert out.splitlines() == ["0018\tone two"]
        assert out.count("\t") == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_make_flight_labels.py::TestReferenceExport -q`
Expected: FAIL with `AttributeError: module 'make_flight_labels' has no attribute 'to_references'`.

- [ ] **Step 3: Write the minimal implementation**

Add to `server/make_flight_labels.py`, after `parse_worksheet`:

```python
def to_references(worksheet: str) -> str:
    """The worksheet's corrected `heard` lines as a bench.load_references file.

    A `?` the labeller wrote is an unintelligible word, not a word they transcribed as "?".
    It is emitted as `[inaudible]`, which bench._normalize already strips, so it costs no WER
    against any arm rather than counting as one wrong word against all of them.

    A row with an empty `heard` line emits nothing: bench treats a missing reference as
    "excluded from aggregates", which is what an unlabelled clip deserves.
    """
    lines = []
    for record in parse_worksheet(worksheet):
        heard = (record.get("heard") or "").strip()
        if not heard:
            continue
        text = heard.replace("?", "[inaudible]").replace("\t", " ")
        lines.append(f"{record['index']:04d}\t{text}")
    return "\n".join(lines)
```

Then in `main`, after the worksheet is written, add the optional export. Add the argument alongside the existing ones:

```python
    ap.add_argument("--references", help="also write a WER reference file from the `heard` lines")
```

and after the `Path(args.out).write_text(...)` call:

```python
    if args.references:
        text = to_references(Path(args.out).read_text(encoding="utf-8"))
        Path(args.references).write_text(text + "\n", encoding="utf-8")
        print(f"-> {args.references} ({len(text.splitlines())} references)")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_make_flight_labels.py -q`
Expected: PASS.

- [ ] **Step 5: Generate the real reference file and eyeball it**

Run:
```bash
py -c "import make_flight_labels as m, pathlib; p=pathlib.Path('flight-labels-2026-09-10.txt'); pathlib.Path('references-airband-2026-09-10.txt').write_text(m.to_references(p.read_text(encoding='utf-8'))+'\n', encoding='utf-8')"
py -c "import pathlib; L=pathlib.Path('references-airband-2026-09-10.txt').read_text(encoding='utf-8').splitlines(); print(len(L),'references'); print('\n'.join(L[:3]))"
```
Expected: **126 references** (136 rows minus the 10 with an empty `heard` line), and the first lines show `0000<TAB>text`.

- [ ] **Step 6: Run the full suite and commit**

Run: `py -m pytest tests/ -q` — expected PASS.

```bash
git add server/make_flight_labels.py server/tests/test_make_flight_labels.py
git commit -m "Export the worksheet's heard lines as a WER reference file"
```

Do **not** commit `references-airband-2026-09-10.txt`; check whether it is gitignored and if not, leave it untracked.

---

### Task 3: Score identification over re-transcribed text

`bench_flight_identify` currently reads the worksheet's own `machine`/`heard` lines. To score an arm it must instead read that arm's transcriptions, joined to the labels by clip id.

**Files:**
- Modify: `server/bench_flight_identify.py`
- Test: `server/tests/test_bench_flight_identify.py`

**Interfaces:**
- Consumes: `bench_flight_identify.score`, `bench_flight_identify.Result` (both already exist)
- Produces:
  - `load_transcripts(path: Path, config: str | None = None) -> dict[str, str]` — clip id to text
  - `score(..., transcripts: dict[str, str] | None = None)` — new keyword argument
  - `Result.missing_transcripts: list[int]` — worksheet indices absent from the arm

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_bench_flight_identify.py`:

```python
class TestScoringAnArmsTranscripts:
    """An arm is scored by replacing the worksheet's machine text with that arm's own
    transcription of the same clip, joined on clip id."""

    def _results_file(self, tmp_path, rows, config="air_shipped"):
        path = tmp_path / "arm.json"
        path.write_text(json.dumps({
            "model_label": "groq-whisper-large-v3",
            "results": {config: [{"clip_id": cid, "text": text, "reference": "", "wer": None}
                                 for cid, text in rows]},
        }), encoding="utf-8")
        return path

    def test_transcripts_load_keyed_by_clip_id(self, tmp_path):
        path = self._results_file(tmp_path, [("0000", "hello"), ("0001", "world")])
        assert bench.load_transcripts(path) == {"0000": "hello", "0001": "world"}

    def test_a_results_file_with_several_configs_needs_the_config_named(self, tmp_path):
        path = tmp_path / "two.json"
        path.write_text(json.dumps({"model_label": None, "results": {
            "air_shipped": [{"clip_id": "0000", "text": "shipped"}],
            "air_both": [{"clip_id": "0000", "text": "both"}],
        }}), encoding="utf-8")
        assert bench.load_transcripts(path, config="air_both") == {"0000": "both"}

    def test_the_arms_text_replaces_the_worksheet_text(self, corpus, tmp_path):
        """Row 0002 is "Port Cremoros three six seven" in the worksheet and extracts nothing.
        An arm that transcribed it as "Orange three six seven" must score as correct."""
        labels, snaps = corpus
        path = self._results_file(tmp_path, [
            ("0002", "Orange three six seven heavy, passing two thousand six hundred.")])
        result = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True,
                             transcripts=bench.load_transcripts(path))
        assert result.rows[2].bucket == bench.CORRECT

    def test_a_clip_missing_from_the_arm_is_excluded_and_named(self, corpus, tmp_path):
        """A dropped clip (a 429, a failed request) must never be scored on stale worksheet
        text -- that would credit one arm with another arm's transcription."""
        labels, snaps = corpus
        path = self._results_file(tmp_path, [("0000", "Delta seven three, New York.")])
        result = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True,
                             transcripts=bench.load_transcripts(path))
        assert result.missing_transcripts == [1, 2, 3, 4]
        assert all(r.bucket == bench.EXCLUDED for r in result.rows[1:])

    def test_transcripts_require_replay(self, corpus):
        labels, snaps = corpus
        with pytest.raises(ValueError):
            bench.score(labels.read_text(encoding="utf-8"), snaps, transcripts={"0000": "x"})
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_bench_flight_identify.py::TestScoringAnArmsTranscripts -q`
Expected: FAIL with `AttributeError: module 'bench_flight_identify' has no attribute 'load_transcripts'`.

- [ ] **Step 3: Write the minimal implementation**

Add to `server/bench_flight_identify.py`, next to `load_snapshots`:

```python
def load_transcripts(path: Path, config: str | None = None) -> dict[str, str]:
    """One arm's transcriptions from a bench-results JSON, keyed by clip id.

    `config` names which arm to read when a results file holds more than one; with a single
    arm it can be omitted, which is the common case since each run writes its own file.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    results = payload["results"]
    if config is None:
        if len(results) != 1:
            raise SystemExit(f"{path} holds {sorted(results)} -- name one with --config")
        config = next(iter(results))
    return {row["clip_id"]: row.get("text") or "" for row in results[config]}
```

Add the field to `Result`, after `excluded`:

```python
    missing_transcripts: list[int]
```

Change the `score` signature to accept the new argument:

```python
def score(worksheet: str, snapshots: list[dict], blank_means: str = LABEL_NONE,
          replay: bool = False, text_source: str = "machine",
          arm: "TailFirst | None" = None,
          transcripts: dict[str, str] | None = None) -> Result:
```

Add to the guard block at the top of `score`, beside the existing arm check:

```python
    if transcripts is not None and not replay:
        raise ValueError("scoring an arm's transcripts is a counterfactual and needs --replay")
```

Inside the per-record loop, in the `if replay:` branch, replace the line that reads the text
(`text = record.get(text_source, "")`) with:

```python
                if transcripts is None:
                    text = record.get(text_source, "")
                else:
                    text = transcripts.get(f"{record['index']:04d}")
```

and immediately after that, before extraction, handle the missing case by forcing the row to
be excluded:

```python
                if text is None:
                    missing.append(record["index"])
                    text, candidate, tagged = "", None, None
                    rows.append(Scored(
                        index=record["index"], timestamp=timestamp, label=LABEL_UNSURE,
                        tagged=None, candidate=None, bucket=EXCLUDED, text="",
                        in_range=False))
                    continue
```

Declare `missing: list[int] = []` beside `rows: list[Scored] = []` at the top of `score`, and
pass `missing_transcripts=missing` into the `Result(...)` constructor.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_bench_flight_identify.py -q`
Expected: PASS.

- [ ] **Step 5: Wire it to the CLI**

In `main`, add:

```python
    ap.add_argument("--transcripts", help="a bench-results JSON to score instead of the "
                                          "worksheet's own machine text (implies --replay)")
    ap.add_argument("--config", help="which arm inside --transcripts to read")
```

After `blank_means` is computed and before `result = score(...)`, add:

```python
    transcripts = (load_transcripts(Path(args.transcripts), args.config)
                   if args.transcripts else None)
```

Pass `transcripts=transcripts` and `replay=args.replay or transcripts is not None` into the
`score(...)` call, then after the summary print:

```python
    if result.missing_transcripts:
        print(f"\n{len(result.missing_transcripts)} clips missing from the arm and excluded: "
              f"{result.missing_transcripts}")
```

- [ ] **Step 6: Run the full suite and commit**

Run: `py -m pytest tests/ -q` — expected PASS.

```bash
git add server/bench_flight_identify.py server/tests/test_bench_flight_identify.py
git commit -m "Score identification over an arm's own transcriptions"
```

---

### Task 4: The word-production counter

The primary metric. It needs no reference text at all, which is why it is trustworthy here: half the reference set is the decoder's own output that the labeller accepted unedited.

**Files:**
- Create: `server/bench_word_production.py`
- Test: `server/tests/test_bench_word_production.py`

**Interfaces:**
- Consumes: `bench_flight_identify.load_transcripts`
- Produces: `count_words(texts: list[str], words: tuple[str, ...]) -> dict[str, int]`

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_bench_word_production.py`:

```python
"""Tests for the reference-free prompt-biasing metric.

Counts how often an arm writes a given word. This is the primary metric for the prompt
measurement precisely because it needs no reference: 51 of the corpus's 136 `heard` lines are
byte-identical to the machine text, so a reference-based metric would partly measure the
decoder agreeing with itself.
"""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import bench_word_production as bwp  # noqa: E402


def test_counts_one_row_per_occurrence_of_each_word():
    counts = bwp.count_words(["QNH one zero one three", "KLM one two bravo"], ("QNH", "KLM"))
    assert counts == {"QNH": 1, "KLM": 1}


def test_a_word_is_counted_once_per_row_not_once_per_repetition():
    """The corpus figures are per-transmission ("27 rows wrote QNH"), so the arms must be
    counted the same way or they are not comparable to the published baseline."""
    assert bwp.count_words(["QNH QNH QNH"], ("QNH",)) == {"QNH": 1}


def test_matching_is_case_insensitive():
    assert bwp.count_words(["klm one two bravo"], ("KLM",)) == {"KLM": 1}


def test_a_word_inside_a_longer_token_does_not_count():
    """Without word boundaries "QNH" would match inside a hyphenated or run-together token
    and quietly inflate the very number the measurement turns on."""
    assert bwp.count_words(["QNHX one zero", "unKLMlike"], ("QNH", "KLM")) == {"QNH": 0, "KLM": 0}


def test_an_empty_corpus_counts_zero_rather_than_failing():
    assert bwp.count_words([], ("QNH",)) == {"QNH": 0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest tests/test_bench_word_production.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'bench_word_production'`.

- [ ] **Step 3: Write the minimal implementation**

Create `server/bench_word_production.py`:

```python
"""Count how often each arm writes a given word. The prompt measurement's primary metric.

Reference-free by design. 51 of the 136 `heard` lines in the 2026-09-10 corpus are
byte-identical to the machine text -- the operator accepted the pre-fill -- so a
reference-based metric would partly measure the decoder agreeing with itself. Counting
production needs no ground truth at all: the question "does this arm write QNH 27 times or 12
times" is answerable from the arm alone, and 12 is what the operator heard.

Usage:
    py bench_word_production.py air_shipped.json air-both.json
    py bench_word_production.py air_shipped.json --words QNH KLM ILS
"""

import argparse
import re
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVER_DIR))

from bench_flight_identify import load_transcripts  # noqa: E402

# What the operator actually heard across the 136-transmission corpus, for comparison.
EAR_COUNTS = {"QNH": 12, "KLM": 15, "ILS": 8}
DEFAULT_WORDS = ("QNH", "KLM", "ILS")


def count_words(texts: list[str], words: tuple[str, ...]) -> dict[str, int]:
    """Rows containing each word, counted once per row.

    Per row rather than per occurrence because the published corpus figures are per
    transmission; counting repetitions would produce a number that cannot be compared to them.
    """
    patterns = {w: re.compile(rf"\b{re.escape(w)}\b", re.I) for w in words}
    return {w: sum(1 for t in texts if p.search(t or "")) for w, p in patterns.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", help="bench-results JSON files, one per arm")
    ap.add_argument("--words", nargs="+", default=list(DEFAULT_WORDS))
    ap.add_argument("--config", help="which arm inside each file to read")
    args = ap.parse_args()

    words = tuple(args.words)
    print(f"{'arm':28} {'clips':>6} " + " ".join(f"{w:>6}" for w in words))
    for path in args.results:
        texts = list(load_transcripts(Path(path), args.config).values())
        counts = count_words(texts, words)
        print(f"  {Path(path).stem:26} {len(texts):>6} "
              + " ".join(f"{counts[w]:>6}" for w in words))
    print(f"  {'(operator heard)':26} {136:>6} "
          + " ".join(f"{EAR_COUNTS.get(w, '?'):>6}" for w in words))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest tests/test_bench_word_production.py -q`
Expected: PASS, 5 tests.

- [ ] **Step 5: Run the full suite and commit**

Run: `py -m pytest tests/ -q` — expected PASS.

```bash
git add server/bench_word_production.py server/tests/test_bench_word_production.py
git commit -m "Add the reference-free word-production metric"
```

---

### Task 5: Run the six arms and publish the result

No new code. This is the measurement the previous four tasks exist to make possible.

**Files:**
- Modify: `docs/superpowers/specs/2026-09-11-airband-prompt-biasing-design.md` (tick the success criteria, add a RESULT section, exactly as `2026-09-10-airband-identification-measurement-design.md` does)

**Interfaces:**
- Consumes: everything from Tasks 1–4

- [ ] **Step 1: Confirm the backend is reachable before spending six runs**

Run:
```bash
py -c "import sys; sys.path.insert(0,'.'); from stt_proxy import backends; print(backends.STT_BACKEND, backends.GROQ_MODEL)"
```
Expected: `groq whisper-large-v3`. If `GROQ_API_KEY` is unset the arms will fail fast; stop and ask the user rather than falling back to another backend, which would make the arms incomparable to production.

- [ ] **Step 2: Run the control arm and its repeat**

```bash
py bench_stt.py --captures "D:\SDR\SdrSharp\Plugins\SttPlugin\captures\2026-09-10" \
                --references references-airband-2026-09-10.txt \
                --prompt air_shipped --config air_shipped --out air_shipped.json
py bench_stt.py --captures "D:\SDR\SdrSharp\Plugins\SttPlugin\captures\2026-09-10" \
                --references references-airband-2026-09-10.txt \
                --prompt air_shipped --config air_shipped --out air_shipped_repeat.json
```

The repeat is the noise floor; without it no delta below a couple of points means anything.
**If either run reports dropped clips, re-run it rather than proceeding** — a partial arm
flatters whichever arm it is compared against.

- [ ] **Step 3: Run the four experimental arms**

```bash
for arm in air_no_qnh air_airlines air_both air_empty; do
  py bench_stt.py --captures "D:\SDR\SdrSharp\Plugins\SttPlugin\captures\2026-09-10" \
                  --references references-airband-2026-09-10.txt \
                  --prompt $arm --config $arm --out $arm.json
done
```

- [ ] **Step 4: The primary metric**

```bash
py bench_word_production.py air_shipped.json air_shipped_repeat.json \
                            air_no_qnh.json air_airlines.json air_both.json air_empty.json
```

Read the control against its repeat first: the gap between those two is the noise floor, and
no experimental arm's movement counts unless it exceeds that gap.

- [ ] **Step 5: The identification metric, per arm**

```bash
for f in air-shipped air-shipped-repeat air_no_qnh air_airlines air_both air_empty; do
  echo "== $f"
  py bench_flight_identify.py --labels flight-labels-2026-09-10.txt --transcripts $f.json
done
```

Compare against the published baseline: recall 43.8%, extraction-miss 15, selection-miss 3,
and precision 100% with `--blank skip`. **Any arm that adds a wrong match is disqualified
whatever it does for recall** — precision is currently perfect and prompting the decoder with
airline names is exactly the change that could invent one. Re-check each candidate arm with
`--blank skip` so the seven labelling-convention artifacts do not mask a real new one.

- [ ] **Step 6: The WER guardrail**

```bash
py bench_prompt_ab.py shipped=air_shipped.json repeat=air_shipped_repeat.json \
                      no_qnh=air_no_qnh.json airlines=air_airlines.json \
                      both=air_both.json empty=air_empty.json --baseline shipped
```

This pairs on clip id and bootstraps a confidence interval on each difference. Only the 85
edited rows carry a real reference; the rest have no reference or a reference identical to the
control's own output, so read this as a regression guard, not a headline.

- [ ] **Step 7: Re-measure the tail-first arm on the winner**

```bash
py bench_flight_identify.py --labels flight-labels-2026-09-10.txt \
                            --transcripts <winning-arm>.json --sweep
```

Four of tail-first's ten recoveries are the QNH→KLM rows. If the winning prompt fixes them at
source, that arm's remaining value must be restated with the overlap removed before anyone
decides whether its precision cost is worth paying.

- [ ] **Step 8: Write the result into the spec and commit**

Add a `## RESULT — <date>` section to the spec with the three metric tables, tick the success
criteria checkboxes, and state plainly which arm wins or that none does. A null result is a
result: the maritime record has three of them, and writing "no arm beat the control" is the
outcome this whole measurement exists to be able to say.

```bash
git add docs/superpowers/specs/2026-09-11-airband-prompt-biasing-design.md
git commit -m "Record the airband prompt-biasing result"
```

Do not edit `DEFAULT_AVIATION_PROMPT` in this task. Changing production is a separate decision
the user makes after reading the result.
