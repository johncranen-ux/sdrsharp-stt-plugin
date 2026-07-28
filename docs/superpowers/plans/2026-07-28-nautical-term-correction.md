# Nautical-term Correction Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add regex corrections to `whisper-proxy.py`'s `_apply_sttt_corrections` for recurring, evidence-backed nautical-term mis-transcriptions, found by diffing Whisper's output against the 49-clip hand-transcribed reference set.

**Architecture:** Extend `bench.py`'s existing Levenshtein-based WER scorer to also return the alignment operations (not just the edit count), so real substitution pairs can be extracted. Aggregate them across all 49 clips in a small analysis script, hand-pick the recurring/safe ones, add them to the existing correction-rule list, and verify with a before/after benchmark run.

**Tech Stack:** Python 3, pytest, whisper.cpp server (WSL2/ROCm), `server/bench.py`.

## Global Constraints

- New correction rules must be word-boundary regex, case-insensitive, following the exact style of existing rules in `_apply_sttt_corrections` (see `server/whisper-proxy.py`).
- Every new rule needs one pytest case in `server/tests/test_whisper_proxy.py`'s existing `test_apply_sttt_corrections` parametrize list.
- Only add a rule if the substitution pattern appears **2 or more times** in the 49-clip data, in a **consistent direction** (Whisper always says X when the reference says Y, never the reverse), and is **nautical/maritime-specific** (not generic grammar noise like article/preposition swaps).
- Acceptance is measured, not assumed: pooled WER on the 49-clip set after the change must be ≤ the baseline measured in Task 3.

---

### Task 1: Add alignment-operation extraction to `bench.py`

**Files:**
- Modify: `server/bench.py` (add a new function near `word_error_counts`, ~line 138)
- Test: `server/tests/test_bench.py`

**Interfaces:**
- Produces: `word_alignment_ops(reference: str, hypothesis: str) -> list[tuple[str, str | None, str | None]] | None`
  Returns `None` if there's no usable reference (same rule as `word_error_counts`/`word_error_rate`).
  Otherwise returns a list of `(op, ref_word, hyp_word)` tuples in left-to-right order, where `op` is one of:
  - `"match"` — `ref_word == hyp_word`, both set
  - `"sub"` — substitution, both set, different words
  - `"ins"` — hypothesis word not present in reference; `ref_word` is `None`
  - `"del"` — reference word missing from hypothesis; `hyp_word` is `None`
  Words are the same normalized tokens `_normalize()` already produces (lowercase, punctuation-stripped) — same normalization `word_error_counts` uses, so results are directly comparable.

- [ ] **Step 1: Write the failing tests**

Add to `server/tests/test_bench.py`:

```python
def test_word_alignment_ops_identifies_substitution():
    ops = bench.word_alignment_ops("mass approach over", "maas approach over")
    subs = [(o[1], o[2]) for o in ops if o[0] == "sub"]
    assert ("mass", "maas") in subs


def test_word_alignment_ops_identifies_insertion_and_deletion():
    ins_ops = bench.word_alignment_ops("roger copy over", "roger copy over standing by")
    assert sum(1 for o in ins_ops if o[0] == "ins") == 2

    del_ops = bench.word_alignment_ops("roger copy over standing by", "roger copy over")
    assert sum(1 for o in del_ops if o[0] == "del") == 2


def test_word_alignment_ops_matches_are_marked():
    ops = bench.word_alignment_ops("roger copy over", "roger copy over")
    assert all(o[0] == "match" for o in ops)
    assert len(ops) == 3


def test_word_alignment_ops_no_reference_returns_none():
    assert bench.word_alignment_ops("", "anything") is None


def test_word_alignment_ops_edit_count_matches_word_error_counts():
    ref, hyp = "maas approach this is neptune over", "mass approach this is fjordstrom over standing by"
    ops = bench.word_alignment_ops(ref, hyp)
    non_matches = sum(1 for o in ops if o[0] != "match")
    edits, _ = bench.word_error_counts(ref, hyp)
    assert non_matches == edits
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && py -m pytest tests/test_bench.py -k word_alignment_ops -v`
Expected: FAIL with `AttributeError: module 'bench' has no attribute 'word_alignment_ops'`

- [ ] **Step 3: Implement `word_alignment_ops`**

Add this function to `server/bench.py` directly after `word_error_counts` (after line 138):

```python
def word_alignment_ops(reference: str, hypothesis: str) -> list[tuple[str, str | None, str | None]] | None:
    """Returns the Levenshtein alignment between reference and hypothesis as a list of
    (op, ref_word, hyp_word) tuples, op in {"match", "sub", "ins", "del"}. Unlike
    word_error_counts (which only returns the edit count), this exposes the actual
    substitution pairs so real recurring mis-transcription patterns can be found instead
    of just measured. Same normalization and no-reference behavior as word_error_counts.
    """
    ref = _normalize(reference)
    hyp = _normalize(hypothesis)
    if not ref:
        return None

    n, m = len(ref), len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )

    ops: list[tuple[str, str | None, str | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1] and dp[i][j] == dp[i - 1][j - 1]:
            ops.append(("match", ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            ops.append(("sub", ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif j > 0 and dp[i][j] == dp[i][j - 1] + 1:
            ops.append(("ins", None, hyp[j - 1]))
            j -= 1
        else:
            ops.append(("del", ref[i - 1], None))
            i -= 1
    ops.reverse()
    return ops
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && py -m pytest tests/test_bench.py -v`
Expected: all PASS, including the 5 new tests and the pre-existing ones.

- [ ] **Step 5: Commit**

```bash
git add server/bench.py server/tests/test_bench.py
git commit -m "Add word-level alignment extraction to bench.py for error analysis"
```

---

### Task 2: Add a substitution-frequency analysis script

**Files:**
- Create: `server/analyze_errors.py`
- Test: `server/tests/test_analyze_errors.py`

**Interfaces:**
- Consumes: `bench.word_alignment_ops` from Task 1; a `bench.py`-produced JSON file shaped like `{"model_label": ..., "results": {"<config_name>": [{"clip_id", "text", "reference", ...}, ...]}}` (see `write_json` in `bench.py`).
- Produces: `aggregate_substitutions(results: dict) -> list[tuple[tuple[str, str], int]]` — a pure function, list of `((hyp_word, ref_word), count)` sorted by count descending, then alphabetically for ties. Direction is `hyp_word` first because that's "what Whisper actually said" — the thing a correction regex needs to match on.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_analyze_errors.py`:

```python
"""Tests for analyze_errors.py's substitution-frequency aggregation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyze_errors


def test_aggregate_substitutions_counts_recurring_pattern():
    results = {
        "beam5_prompt": [
            {"clip_id": "0001", "reference": "maas approach", "text": "mass approach"},
            {"clip_id": "0002", "reference": "maas control", "text": "mass control"},
            {"clip_id": "0003", "reference": "roger copy", "text": "roger copy"},
        ]
    }
    counts = analyze_errors.aggregate_substitutions(results)
    assert (("mass", "maas"), 2) in counts


def test_aggregate_substitutions_skips_rows_without_reference():
    results = {
        "beam5_prompt": [
            {"clip_id": "0001", "reference": None, "text": "anything"},
        ]
    }
    assert analyze_errors.aggregate_substitutions(results) == []


def test_aggregate_substitutions_sorted_by_count_descending():
    results = {
        "beam5_prompt": [
            {"clip_id": "0001", "reference": "buoy one", "text": "boy one"},
            {"clip_id": "0002", "reference": "buoy two", "text": "boy two"},
            {"clip_id": "0003", "reference": "buoy three", "text": "boy three"},
            {"clip_id": "0004", "reference": "ladder down", "text": "letter down"},
        ]
    }
    counts = analyze_errors.aggregate_substitutions(results)
    assert counts[0] == (("boy", "buoy"), 3)
    assert (("letter", "ladder"), 1) in counts
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_analyze_errors.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'analyze_errors'`

- [ ] **Step 3: Implement `server/analyze_errors.py`**

```python
"""Aggregates recurring word-substitution patterns from a bench.py JSON results file,
to find real, evidence-backed candidates for whisper-proxy.py's _apply_sttt_corrections
list -- rather than guessing at nautical-term correction rules.

Usage:
    py analyze_errors.py bench-results.json [--top 40]
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import bench


def aggregate_substitutions(results: dict) -> list[tuple[tuple[str, str], int]]:
    """Returns [((hyp_word, ref_word), count), ...] sorted by count desc, then
    alphabetically. hyp_word first: that's what Whisper actually produced, the thing a
    correction regex matches on; ref_word is what it should have said.
    """
    counter: Counter[tuple[str, str]] = Counter()
    for rows in results.values():
        for row in rows:
            reference = row.get("reference")
            if not reference:
                continue
            ops = bench.word_alignment_ops(reference, row.get("text", ""))
            if ops is None:
                continue
            for op, ref_word, hyp_word in ops:
                if op == "sub":
                    counter[(hyp_word, ref_word)] += 1
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", help="Path to a bench.py --out-json results file")
    parser.add_argument("--top", type=int, default=40, help="How many rows to print")
    args = parser.parse_args()

    payload = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    counts = aggregate_substitutions(payload["results"])

    print(f"{'hyp -> ref':<40}{'count':>6}")
    print("-" * 46)
    for (hyp_word, ref_word), count in counts[: args.top]:
        print(f"{hyp_word:<18} -> {ref_word:<18}{count:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && py -m pytest tests/test_analyze_errors.py -v`
Expected: all 3 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add server/analyze_errors.py server/tests/test_analyze_errors.py
git commit -m "Add substitution-frequency analysis script for correction-rule discovery"
```

---

### Task 3: Run the baseline benchmark and capture the substitution report

This task produces data used to decide Task 4's corrections — no new code.

**Files:**
- Produces (gitignored, not committed): `server/bench-results-baseline.json`, `server/bench-report-baseline.html`

- [ ] **Step 1: Confirm the whisper-server and proxy are healthy**

```bash
curl -s -m 5 http://localhost:8080/ -o /dev/null -w "backend: %{http_code}\n"
curl -s -m 5 http://localhost:9000/ -o /dev/null -w "proxy: %{http_code}\n"
```
Expected: both `200`. If not, restart via `wsl -d Ubuntu-22.04 -- bash -l -c "~/start-whisper-server.sh"` (backend) and the proxy via `start-all.bat`/manual `py whisper-proxy.py`.

- [ ] **Step 2: Run bench.py against the 49-clip reference set**

```bash
cd server
py bench.py \
  --captures "D:\SDR\SDRSharp\Plugins\SttPlugin\captures\2026-07-27" \
  --references references.txt \
  --matrix beam5_prompt \
  --host localhost --port 8080 \
  --out-json bench-results-baseline.json \
  --out-html bench-report-baseline.html \
  --model-label "large-v3, no-flash-attn (2026-07-28 baseline)"
```

Expected: prints per-clip WER as it runs, ends with a summary table showing pooled WER
for `beam5_prompt`. **Write down this pooled WER number — it's the baseline Task 6
compares against.** If any request hangs for 25+ seconds, the watchdog in
`whisper-proxy.py` will auto-restart the backend (only relevant if running this through
the proxy on :9000 instead of :8080 directly — running straight against :8080 as shown
here bypasses the watchdog, so if a request hangs, manually restart `whisper-server` and
re-run).

- [ ] **Step 3: Generate the substitution-frequency report**

```bash
py analyze_errors.py bench-results-baseline.json --top 50
```

Expected: a table of `hyp -> ref` substitution pairs sorted by frequency. Save this
output (copy the terminal output, or redirect to a file) — Task 4 reviews it directly.

---

### Task 4: Select and add new correction rules

**Files:**
- Modify: `server/whisper-proxy.py` — `_apply_sttt_corrections`, ~line 456
- Modify: `server/tests/test_whisper_proxy.py` — the `test_apply_sttt_corrections` parametrize list, ~line 56

- [ ] **Step 1: Apply the selection rule to Task 3's output**

From the printed substitution table, select every `(hyp_word, ref_word)` pair where
**all** of these hold:
1. Count ≥ 2.
2. The pair is maritime/nautical-specific vocabulary (vessel/navigation/radio terms),
   not generic English (articles, prepositions, common verbs, filler words).
3. Substituting `hyp_word` → `ref_word` is safe in general maritime VHF context — i.e.
   `hyp_word` isn't itself a legitimate word that would sometimes appear correctly (skip
   ambiguous pairs; a wrong correction is worse than a missed one).

Discard everything else. It's fine — expected, even — if this yields zero, a few, or
many rules; the count is determined by what's actually in the data, not a target to hit.

- [ ] **Step 2: Write the failing tests for each selected rule**

For each selected `(hyp_word, ref_word)` pair, add one line to the `@pytest.mark.parametrize`
list in `server/tests/test_whisper_proxy.py` (~line 56), following the exact existing
style — a short realistic sentence containing `hyp_word`, and the expected substring
containing `ref_word`. Example shape (using the two illustrative pairs from the design
doc — replace with whatever Task 3 actually surfaces):

```python
    ("watch out for the letter", "watch out for the ladder"),
```

- [ ] **Step 3: Run tests to verify the new cases fail**

Run: `cd server && py -m pytest tests/test_whisper_proxy.py -k test_apply_sttt_corrections -v`
Expected: the new parametrized cases FAIL (no matching rule yet); pre-existing cases still PASS.

- [ ] **Step 4: Add the corrections**

Add one tuple per selected pair to the `corrections` list inside `_apply_sttt_corrections`
in `server/whisper-proxy.py` (~line 457), matching the exact existing pattern:

```python
        (r'\bletter\b', 'ladder', re.IGNORECASE),
```

Use `\b...\b` word-boundary matching and `re.IGNORECASE`, same as every existing rule.
If `ref_word` is capitalized as a proper term in existing rules (e.g. `Maas`, `Callsign`,
`Motortanker`), match that capitalization style; otherwise lowercase is fine, matching
`draught`/`buoys`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && py -m pytest tests/test_whisper_proxy.py -v`
Expected: all PASS, including new cases.

- [ ] **Step 6: Commit**

```bash
git add server/whisper-proxy.py server/tests/test_whisper_proxy.py
git commit -m "Add evidence-backed nautical-term correction rules"
```

---

### Task 5: Verify improvement and deploy

**Files:**
- Modify: `README.md` — the "Current configuration" WER table / "Known limitations", if the number moves meaningfully
- Produces (gitignored): `server/bench-results-after.json`, `server/bench-report-after.html`

- [ ] **Step 1: Re-run the same benchmark**

```bash
cd server
py bench.py \
  --captures "D:\SDR\SDRSharp\Plugins\SttPlugin\captures\2026-07-27" \
  --references references.txt \
  --matrix beam5_prompt \
  --host localhost --port 8080 \
  --out-json bench-results-after.json \
  --out-html bench-report-after.html \
  --model-label "large-v3, no-flash-attn, + nautical corrections (2026-07-28)"
```

Note: `_apply_sttt_corrections` runs in `whisper-proxy.py`, not in `whisper-server`
itself — `bench.py` here talks directly to `:8080` (the raw backend), so **this
comparison must instead go through the proxy on `:9000`** to actually exercise the new
corrections. Repeat with `--port 9000` and `--host localhost` (the proxy translates
`/inference` internally). Confirm the proxy is running first (Task 3, Step 1).

- [ ] **Step 2: Compare pooled WER**

Compare the pooled WER printed in this run's summary table against Task 3 Step 2's
baseline number.

- **If it improved or stayed the same:** proceed to Step 3.
- **If it regressed:** the new rule(s) are firing somewhere they shouldn't (a false
  positive on a word that was sometimes correct as-is). Check the per-clip diffs in
  `bench-report-after.html` for clips that got worse, identify which rule caused it, and
  either narrow that rule's regex (e.g. add more context to the pattern) or drop it.
  Re-run this task's Step 1 after any fix.

- [ ] **Step 3: Update README if the number moved**

If pooled WER changed by more than rounding noise, update the WER figure in the
"Current configuration" section of `README.md` (~line 25, the model comparison table)
and/or add a line noting the correction pass and its measured effect.

- [ ] **Step 4: Restart the proxy to deploy**

```bash
# Find and stop the running proxy process, then restart it the same way it was
# started before (see start-all.bat or the manual Start-Process pattern used this
# session) so it picks up the new whisper-proxy.py code.
```

Verify with a real request through `:9000/v1/audio/transcriptions` that the proxy is
healthy and applying corrections (check the returned text against a known correction
case if a matching real chunk is available).

- [ ] **Step 5: Commit**

```bash
git add README.md
git commit -m "Update WER figures after nautical-term correction pass"
```
