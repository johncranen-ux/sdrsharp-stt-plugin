# Conversation-Level Correction Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct each transmission's text using the rest of its conversation, so a garbled
opening call is repaired from the shore station's clean rendition, while keeping the verbatim
original visible.

**Architecture:** A second pass that runs after `resolve_conversation` and before storage. It
takes one exchange's turns plus the already-resolved vessel name, asks a model for corrected
text with every substitution declared, validates the reply, and stores it as a third text layer
beside the raw and live-corrected ones. Any failure stores the conversation uncorrected.

**Tech Stack:** Python 3.12+, pytest, `anthropic` SDK (already a dependency), `urllib` for
OpenRouter, `rapidfuzz` (already used elsewhere).

**Spec:** `docs/superpowers/specs/2026-08-10-conversation-correction-design.md`

## Global Constraints

- All new production code lives in `server/stt_proxy/`; bench and diagnostic tools live in `server/`.
- Tests are one file per module in `server/tests/`, matching the existing layout.
- Run tests with `py -m pytest server/tests -v` from the repo root, or `python -m pytest tests -q` from `server/`.
- `temperature=0` on every model call. Sampling noise made a previous A/B unmeasurable.
- **No received radio traffic in git.** Test fixtures are short and invented. Real transcripts load at runtime from gitignored files only.
- `CONVERSATION_CORRECT` defaults `off`. Nothing changes in production until the bake-off scores it.
- Environment variables, exact names and defaults:

  | variable | default | meaning |
  |---|---|---|
  | `CONVERSATION_CORRECT` | `off` | the pass runs at all |
  | `CONVERSATION_CORRECT_PROVIDER` | `anthropic` | `anthropic` or `openrouter` |
  | `CONVERSATION_CORRECT_MODEL` | `claude-haiku-4-5-20251001` | model id |
  | `CONVERSATION_CORRECT_FEWSHOT` | `on` | include runtime-loaded examples |
  | `CONVERSATION_CORRECT_TIMEOUT_S` | `60` | bound on one call |
  | `CONVERSATION_FEWSHOT_FILE` | `` | path to the gitignored examples file |
  | `OPENROUTER_API_KEY` | `` | required only for the openrouter provider |

## File Structure

| file | responsibility |
|---|---|
| `server/stt_proxy/llm.py` (new) | Provider-agnostic `complete()`. Anthropic and OpenRouter behind one signature. |
| `server/stt_proxy/fewshot.py` (new) | Load few-shot examples at runtime; synthetic fallback in source. |
| `server/stt_proxy/conversation_correct.py` (new) | The pass: prompt, call, validation, fallback. Knows nothing of storage or HTML. |
| `server/stt_proxy/conversations.py` (modify) | Call the pass in `_resolve_window`; store `conv`/`changes`; render them. |
| `server/clip_index.py` (new) | Join capture clip ids to timestamps, for the benchmark only. |
| `server/bench_conversation_correct.py` (new) | Score WER and invented content, baseline vs corrected. |
| `server/tests/test_clip_index.py` (new) | |
| `server/tests/test_llm.py` (new) | |
| `server/tests/test_fewshot.py` (new) | |
| `server/tests/test_conversation_correct.py` (new) | |
| `server/tests/test_bench_conversation_correct.py` (new) | |

---

### Task 1: Clip index — join turns to reference clips

The benchmark needs each stored turn mapped to its reference clip. Captures carry
`index.jsonl` with an `index` and a `timestamp` per clip, which is that join. Verified on the
2026-08-07 capture: 152 rows, clip 0000 at `2026-08-07T10:14:15.2151119+02:00`, matching the
stored turn time `10:14:15` exactly.

**Two traps this task exists to absorb:** the file is UTF-8 **with BOM** (plain `utf-8` raises
`Unexpected UTF-8 BOM`), and `index` is a *number* in the JSON while reference keys are
*zero-padded 4-character strings*.

**Files:**
- Create: `server/clip_index.py`
- Test: `server/tests/test_clip_index.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `load_clip_index(day_dir: str | Path) -> dict[str, datetime.datetime]` mapping
  clip id (`"0000"`) to a naive local timestamp; `clip_for_time(index, when, tolerance_s=2.0)
  -> str | None`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_clip_index.py
"""Tests for clip_index.py: joining capture clip ids to timestamps."""

import datetime
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from clip_index import clip_for_time, load_clip_index  # noqa: E402


def _write_index(tmp_path, rows, bom=True):
    text = "\n".join(rows)
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    (tmp_path / "index.jsonl").write_bytes(data)
    return tmp_path


def test_reads_an_index_written_with_a_bom(tmp_path):
    """The plugin writes UTF-8 with a BOM; plain utf-8 decoding raises on the first line."""
    _write_index(tmp_path, [
        '{"index": 0, "timestamp": "2026-08-07T10:14:15.215+02:00", "channel": "160,650"}',
    ])
    assert load_clip_index(tmp_path) == {
        "0000": datetime.datetime(2026, 8, 7, 10, 14, 15, 215000)}


def test_clip_ids_are_zero_padded_to_match_reference_keys(tmp_path):
    """index is a number in the JSON; references are keyed '0007'."""
    _write_index(tmp_path, [
        '{"index": 7, "timestamp": "2026-08-07T10:20:00+02:00"}',
    ])
    assert list(load_clip_index(tmp_path)) == ["0007"]


def test_a_turn_time_finds_its_clip(tmp_path):
    _write_index(tmp_path, [
        '{"index": 0, "timestamp": "2026-08-07T10:14:15+02:00"}',
        '{"index": 1, "timestamp": "2026-08-07T10:14:19+02:00"}',
    ])
    index = load_clip_index(tmp_path)
    when = datetime.datetime(2026, 8, 7, 10, 14, 19)
    assert clip_for_time(index, when) == "0001"


def test_a_time_outside_tolerance_matches_nothing(tmp_path):
    """Better no clip than the wrong clip: a wrong join silently scores one turn's text
    against another turn's reference, which reads as a quality change that never happened."""
    _write_index(tmp_path, [
        '{"index": 0, "timestamp": "2026-08-07T10:14:15+02:00"}',
    ])
    index = load_clip_index(tmp_path)
    assert clip_for_time(index, datetime.datetime(2026, 8, 7, 10, 30, 0)) is None


def test_a_malformed_row_is_skipped_not_fatal(tmp_path):
    _write_index(tmp_path, [
        '{"index": 0, "timestamp": "2026-08-07T10:14:15+02:00"}',
        'not json at all',
        '{"index": 2, "timestamp": "2026-08-07T10:14:31+02:00"}',
    ])
    assert sorted(load_clip_index(tmp_path)) == ["0000", "0002"]


def test_a_missing_index_file_is_an_empty_mapping(tmp_path):
    assert load_clip_index(tmp_path) == {}
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd server && python -m pytest tests/test_clip_index.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'clip_index'`

- [ ] **Step 3: Write the implementation**

```python
# server/clip_index.py
"""Join capture clip ids to wall-clock times.

Stored conversation turns carry HH:MM:SS; reference files are keyed by clip id. The capture
directory's index.jsonl holds both, so it is the authoritative join between a turn and the
reference text it should be scored against.

Two things about that file cost time if you meet them the hard way: it is written UTF-8 with
a BOM, so plain "utf-8" raises on line 1, and its "index" is a number while reference keys are
zero-padded strings.
"""

import datetime
import json
from pathlib import Path

_INDEX_NAME = "index.jsonl"


def load_clip_index(day_dir: str | Path) -> dict[str, datetime.datetime]:
    """{clip_id: naive local timestamp} for one capture day. Missing file -> {}."""
    path = Path(day_dir) / _INDEX_NAME
    if not path.is_file():
        return {}

    out: dict[str, datetime.datetime] = {}
    # utf-8-sig, not utf-8: the plugin writes a BOM.
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                clip = f"{int(row['index']):04d}"
                stamp = datetime.datetime.fromisoformat(row["timestamp"])
            except (ValueError, KeyError, TypeError):
                # One bad row must not cost the whole capture.
                continue
            # Turn times are naive local; drop the offset rather than mixing awareness.
            out[clip] = stamp.replace(tzinfo=None)
    return out


def clip_for_time(index: dict[str, datetime.datetime], when: datetime.datetime,
                  tolerance_s: float = 2.0) -> str | None:
    """The clip whose timestamp is nearest `when`, or None if none is within tolerance.

    None rather than nearest-regardless on purpose: a wrong join scores one turn's text
    against another turn's reference, which shows up as a quality change that never happened.
    """
    best, best_delta = None, None
    for clip, stamp in index.items():
        delta = abs((stamp - when).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_delta = clip, delta
    if best_delta is None or best_delta > tolerance_s:
        return None
    return best
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd server && python -m pytest tests/test_clip_index.py -v`
Expected: 6 passed

- [ ] **Step 5: Verify against the real capture**

Run:
```bash
cd server && python -c "
from clip_index import load_clip_index, clip_for_time
import datetime
ix = load_clip_index(r'D:\SDR\SdrSharp\Plugins\SttPlugin\captures\2026-08-07')
print('clips:', len(ix))
print('0000 ->', ix.get('0000'))
print('turn 10:14:15 ->', clip_for_time(ix, datetime.datetime(2026,8,7,10,14,15)))
"
```
Expected: `clips: 152`, `0000 -> 2026-08-07 10:14:15.215112`, `turn 10:14:15 -> 0000`.
If the count is not 152, stop and report — the benchmark depends on this join.

- [ ] **Step 6: Commit**

```bash
git add server/clip_index.py server/tests/test_clip_index.py
git commit -m "Join capture clip ids to turn timestamps"
```

---

### Task 2: LLM provider interface, Anthropic implementation

**Files:**
- Create: `server/stt_proxy/llm.py`
- Test: `server/tests/test_llm.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `complete(system: str, user: str, *, provider: str, model: str,
  temperature: float = 0.0, timeout_s: float = 60.0, max_tokens: int = 4096) -> str`;
  `strip_code_fence(text: str) -> str`; `class LLMError(Exception)`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_llm.py
"""Tests for llm.py: one signature over several providers."""

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import llm  # noqa: E402


class _FakeMessages:
    def __init__(self, reply, recorder):
        self._reply, self._recorder = reply, recorder

    def create(self, **kwargs):
        self._recorder.update(kwargs)
        return type("R", (), {"content": [type("C", (), {"text": self._reply})()]})()


class _FakeClient:
    def __init__(self, reply, recorder):
        self.messages = _FakeMessages(reply, recorder)


def test_anthropic_returns_the_reply_text(monkeypatch):
    calls = {}
    monkeypatch.setattr(llm, "_anthropic_client",
                        lambda timeout_s: _FakeClient('{"ok": true}', calls))
    assert llm.complete("sys", "usr", provider="anthropic", model="m") == '{"ok": true}'


def test_temperature_is_pinned_to_zero_by_default(monkeypatch):
    """Sampling noise made a previous A/B unmeasurable; nothing here wants sampling."""
    calls = {}
    monkeypatch.setattr(llm, "_anthropic_client",
                        lambda timeout_s: _FakeClient("x", calls))
    llm.complete("sys", "usr", provider="anthropic", model="m")
    assert calls["temperature"] == 0


def test_an_unknown_provider_is_an_error():
    with pytest.raises(llm.LLMError, match="unknown provider"):
        llm.complete("sys", "usr", provider="nope", model="m")


def test_a_provider_failure_is_wrapped_as_llm_error(monkeypatch):
    def boom(timeout_s):
        raise RuntimeError("connection reset")
    monkeypatch.setattr(llm, "_anthropic_client", boom)
    with pytest.raises(llm.LLMError, match="connection reset"):
        llm.complete("sys", "usr", provider="anthropic", model="m")


@pytest.mark.parametrize("raw,expected", [
    ('{"a": 1}', '{"a": 1}'),
    ('```json\n{"a": 1}\n```', '{"a": 1}'),
    ('```\n{"a": 1}\n```', '{"a": 1}'),
    ('here you go:\n```json\n{"a": 1}\n```\n', '{"a": 1}'),
])
def test_code_fences_are_stripped(raw, expected):
    """Models wrap JSON in fences regardless of instructions; the resolver already
    works around this and the new pass must not repeat the workaround."""
    assert llm.strip_code_fence(raw) == expected


class _FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_openrouter_sends_system_and_user_and_returns_content(monkeypatch):
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data.decode("utf-8"))
        seen["timeout"] = timeout
        return _FakeResponse(json.dumps(
            {"choices": [{"message": {"content": '{"ok": 1}'}}]}).encode("utf-8"))

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    out = llm.complete("sys", "usr", provider="openrouter", model="free/model",
                       timeout_s=12.0)
    assert out == '{"ok": 1}'
    assert seen["body"]["messages"][0] == {"role": "system", "content": "sys"}
    assert seen["body"]["messages"][1] == {"role": "user", "content": "usr"}
    assert seen["body"]["temperature"] == 0.0
    assert seen["timeout"] == 12.0


def test_openrouter_sets_a_custom_user_agent(monkeypatch):
    """Cloudflare 403s the default Python-urllib agent (error 1010), which reads as a
    model-specific fault and is not. Cost real time once already."""
    seen = {}

    def fake_urlopen(request, timeout=None):
        seen.update({k.lower(): v for k, v in request.headers.items()})
        return _FakeResponse(json.dumps(
            {"choices": [{"message": {"content": "x"}}]}).encode("utf-8"))

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    llm.complete("s", "u", provider="openrouter", model="m")
    assert "python-urllib" not in seen["user-agent"].lower()


def test_openrouter_without_a_key_is_an_llm_error(monkeypatch):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(llm.LLMError, match="OPENROUTER_API_KEY"):
        llm.complete("s", "u", provider="openrouter", model="m")
```

Add these imports at the top of the same file, beside the existing ones:

```python
import io
import json
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd server && python -m pytest tests/test_llm.py -v`
Expected: FAIL — `ImportError: cannot import name 'llm'`

Every test in this file must be watched failing before `llm.py` exists. Both providers are
tested here, test-first, rather than one being covered after the fact.

- [ ] **Step 3: Write the implementation**

```python
# server/stt_proxy/llm.py
"""One call signature over several providers.

The conversation-correction pass has to be scored across models before one is chosen, and the
2026-08-03 bake-off found free endpoints are a supply problem before a quality one. Putting the
provider behind a signature means the bake-off sweeps configuration rather than code.

claude.py stays as it is: it owns the live path's client, whose 15s timeout is tuned for a call
that blocks a transcription response. This module's callers are off that path.
"""

import json
import os
import re
import urllib.request

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


class LLMError(Exception):
    """Any provider failure, so callers catch one type rather than three SDKs' worth."""


def strip_code_fence(text: str) -> str:
    """The JSON inside a markdown fence, or the text unchanged."""
    match = _FENCE_RE.search(text)
    return match.group(1).strip() if match else text.strip()


def _anthropic_client(timeout_s: float):
    import anthropic
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise LLMError("ANTHROPIC_API_KEY is not set")
    return anthropic.Anthropic(api_key=api_key, timeout=timeout_s, max_retries=1)


def _complete_anthropic(system, user, *, model, temperature, timeout_s, max_tokens):
    client = _anthropic_client(timeout_s)
    message = client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        system=system, messages=[{"role": "user", "content": user}],
    )
    return message.content[0].text.strip()


def _complete_openrouter(system, user, *, model, temperature, timeout_s, max_tokens):
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise LLMError("OPENROUTER_API_KEY is not set")
    payload = json.dumps({
        "model": model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
    }).encode("utf-8")
    request = urllib.request.Request(OPENROUTER_URL, data=payload, method="POST")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Content-Type", "application/json")
    # A custom User-Agent is not optional: Cloudflare 403s the default Python-urllib one,
    # which reads as a model-specific failure and is not.
    request.add_header("User-Agent", "sdrsharp-stt-proxy/1.0")
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        body = json.loads(response.read().decode("utf-8"))
    return body["choices"][0]["message"]["content"].strip()


_PROVIDERS = {"anthropic": _complete_anthropic, "openrouter": _complete_openrouter}


def complete(system: str, user: str, *, provider: str, model: str,
             temperature: float = 0.0, timeout_s: float = 60.0,
             max_tokens: int = 4096) -> str:
    """One completion. Raises LLMError for every failure mode, including a bad provider."""
    fn = _PROVIDERS.get(provider)
    if fn is None:
        raise LLMError(f"unknown provider {provider!r}; known: {sorted(_PROVIDERS)}")
    try:
        return fn(system, user, model=model, temperature=temperature,
                  timeout_s=timeout_s, max_tokens=max_tokens)
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(str(exc)) from exc
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd server && python -m pytest tests/test_llm.py -v`
Expected: 11 passed

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/llm.py server/tests/test_llm.py
git commit -m "Put the model provider behind one call signature"
```

---

### Task 3: MERGED INTO TASK 2 — do not dispatch

Both providers are now tested test-first inside Task 2, so every test in `test_llm.py` is
watched failing before `llm.py` exists. Writing the OpenRouter tests after its implementation
would have produced tests that pass on first run, which proves nothing about whether they can
catch the bug they describe.

Task numbering below is unchanged, so the plan's cross-references stay valid.

---

### Task 4: Few-shot example loading

Examples load at runtime from a gitignored file. They are never baked into source: the CI
transcript gate is a list of filenames rather than a content scan, so an example pasted into a
module would pass the gate and still commit received radio traffic to a repo intended for
publication.

**Files:**
- Create: `server/stt_proxy/fewshot.py`
- Test: `server/tests/test_fewshot.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `load_examples(path: str | None = None) -> list[dict]`, each dict
  `{"vessel": str | None, "turns": [{"id": int, "text": str}],
    "output": {"turns": [{"id": int, "text": str, "changes": list}]}}`;
  `render_examples(examples: list[dict]) -> str`; `SYNTHETIC_EXAMPLES: list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_fewshot.py
"""Tests for fewshot.py: runtime-loaded examples with a synthetic fallback."""

import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import fewshot  # noqa: E402


def test_a_missing_file_falls_back_to_the_synthetic_set():
    """A fresh checkout has no references file and must still work."""
    got = fewshot.load_examples("no/such/file.json")
    assert got == fewshot.SYNTHETIC_EXAMPLES
    assert got, "the synthetic set must not be empty"


def test_no_path_configured_falls_back_to_the_synthetic_set(monkeypatch):
    monkeypatch.delenv("CONVERSATION_FEWSHOT_FILE", raising=False)
    assert fewshot.load_examples() == fewshot.SYNTHETIC_EXAMPLES


def test_examples_load_from_the_configured_file(tmp_path):
    payload = [{
        "vessel": "EXAMPLE TRADER",
        "turns": [{"id": 1, "text": "Maas Approach, motor vision Example Trader."}],
        "output": {"turns": [{"id": 1, "text": "Maas Approach, Motorvessel Example Trader.",
                              "changes": [{"from": "motor vision", "to": "Motorvessel",
                                           "reason": "shore station rendition"}]}]},
    }]
    path = tmp_path / "examples.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    got = fewshot.load_examples(str(path))
    assert got[0]["vessel"] == "EXAMPLE TRADER"
    assert got[0]["output"]["turns"][0]["changes"][0]["to"] == "Motorvessel"


def test_a_malformed_file_falls_back_rather_than_crashing(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert fewshot.load_examples(str(path)) == fewshot.SYNTHETIC_EXAMPLES


def test_the_synthetic_examples_name_no_real_vessel():
    """Examples must teach patterns, not a roster. A real cached name in an example invites
    the model to reach for it elsewhere -- the failure mode the live prompt's rule 5 already
    guards against for AIS hints."""
    for example in fewshot.SYNTHETIC_EXAMPLES:
        assert "EXAMPLE" in (example["vessel"] or "").upper()


def test_rendering_produces_one_block_per_example():
    text = fewshot.render_examples(fewshot.SYNTHETIC_EXAMPLES)
    assert text.count("[EXAMPLE INPUT]") == len(fewshot.SYNTHETIC_EXAMPLES)
    assert text.count("[EXAMPLE OUTPUT]") == len(fewshot.SYNTHETIC_EXAMPLES)


def test_rendering_nothing_is_an_empty_string():
    assert fewshot.render_examples([]) == ""
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd server && python -m pytest tests/test_fewshot.py -v`
Expected: FAIL — `ImportError: cannot import name 'fewshot'`

- [ ] **Step 3: Write the implementation**

```python
# server/stt_proxy/fewshot.py
"""Few-shot examples for the conversation-correction prompt.

Loaded at runtime, never baked into source. The examples that actually teach this task are
real exchanges, and real exchanges are received radio traffic: the repo's CI gate is a list of
known filenames rather than a content scan, so an example pasted into a module would pass the
gate and still put traffic into git permanently (NL Telecommunicatiewet 18.13 / ITU RR 17.3).

The synthetic set below is invented, deliberately names nobody real, and exists so a fresh
checkout works without the operator's private files.
"""

import json
import os

# Invented vessels. A real cached name here would invite the model to reach for it in
# unrelated conversations -- the same failure the live prompt's rule 5 guards against for
# AIS hints.
SYNTHETIC_EXAMPLES = [
    {
        "vessel": "EXAMPLE TRADER",
        "turns": [
            {"id": 1, "text": "Maas Approach, Maas Approach, motor vision Example Traitor."},
            {"id": 2, "text": "Motorvessel Example Trader, Maas Approach, good morning."},
        ],
        "output": {"turns": [
            {"id": 1, "text": "Maas Approach, Maas Approach, Motorvessel Example Trader.",
             "changes": [
                 {"from": "motor vision", "to": "Motorvessel",
                  "reason": "shore station rendition of the type word"},
                 {"from": "Example Traitor", "to": "Example Trader",
                  "reason": "shore station rendition of the name"}]},
            {"id": 2, "text": "Motorvessel Example Trader, Maas Approach, good morning.",
             "changes": []},
        ]},
    },
    {
        "vessel": "EXAMPLE VOYAGER",
        "turns": [
            {"id": 1, "text": "Example Voyager, pilot ladder port side one metre above water."},
            {"id": 2, "text": "Pilot letter part side one metre above water, Example Voyager."},
        ],
        "output": {"turns": [
            {"id": 1, "text": "Example Voyager, pilot ladder port side one metre above water.",
             "changes": []},
            {"id": 2, "text": "Pilot ladder port side one metre above water, Example Voyager.",
             "changes": [
                 {"from": "Pilot letter part side", "to": "Pilot ladder port side",
                  "reason": "garbled readback of the instruction in turn 1"}]},
        ]},
    },
]


def load_examples(path: str | None = None) -> list[dict]:
    """Examples from `path` (or CONVERSATION_FEWSHOT_FILE), else the synthetic set.

    Every failure falls back rather than raising: a missing or hand-edited examples file must
    never stop the pass from running.
    """
    path = path or os.environ.get("CONVERSATION_FEWSHOT_FILE", "")
    if not path:
        return SYNTHETIC_EXAMPLES
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            loaded = json.load(fh)
    except (OSError, ValueError):
        return SYNTHETIC_EXAMPLES
    if not isinstance(loaded, list) or not loaded:
        return SYNTHETIC_EXAMPLES
    return loaded


def render_examples(examples: list[dict]) -> str:
    """The examples as prompt text. Empty string for no examples, so the caller can concatenate."""
    blocks = []
    for example in examples:
        lines = ["[EXAMPLE INPUT]", f"vessel: {example.get('vessel') or 'unidentified'}"]
        for turn in example.get("turns", []):
            lines.append(f"  {turn['id']}. {turn['text']}")
        lines.append("[EXAMPLE OUTPUT]")
        lines.append(json.dumps(example.get("output", {}), ensure_ascii=False))
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd server && python -m pytest tests/test_fewshot.py -v`
Expected: 7 passed

- [ ] **Step 5: Add the examples file to .gitignore**

Append to `.gitignore`:
```
# Few-shot examples are built from real exchanges, which are received radio traffic.
server/conversation-fewshot*.json
```

- [ ] **Step 6: Commit**

```bash
git add server/stt_proxy/fewshot.py server/tests/test_fewshot.py .gitignore
git commit -m "Load correction examples at runtime, never from source"
```

---

### Task 5: The validation contract

The pass's safety rests here. Written and tested before any prompt exists, because this is what
makes a misbehaving model detectable rather than silently destructive.

**Files:**
- Create: `server/stt_proxy/conversation_correct.py`
- Test: `server/tests/test_conversation_correct.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `validate_reply(payload: dict, turns: list[dict]) -> dict[int, dict]` returning
  `{turn_id: {"text": str, "changes": list[dict]}}`; `class CorrectionRejected(Exception)`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_conversation_correct.py
"""Tests for conversation_correct.py: the pass that repairs a turn from its conversation."""

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import conversation_correct as cc  # noqa: E402


TURNS = [
    {"id": 1, "corrected": "Maas Approach, motor vision Example Trader."},
    {"id": 2, "corrected": "Motorvessel Example Trader, Maas Approach."},
]


def _reply(turns):
    return {"turns": turns}


def test_a_well_formed_reply_maps_id_to_text_and_changes():
    got = cc.validate_reply(_reply([
        {"id": 1, "text": "Maas Approach, Motorvessel Example Trader.",
         "changes": [{"from": "motor vision", "to": "Motorvessel", "reason": "shore station"}]},
        {"id": 2, "text": "Motorvessel Example Trader, Maas Approach.", "changes": []},
    ]), TURNS)
    assert got[1]["text"] == "Maas Approach, Motorvessel Example Trader."
    assert got[2]["changes"] == []


def test_a_dropped_turn_is_rejected():
    """Losing a turn silently truncates a conversation the operator is reading."""
    with pytest.raises(cc.CorrectionRejected, match="missing"):
        cc.validate_reply(_reply([
            {"id": 1, "text": "x", "changes": [{"from": "a", "to": "x", "reason": "r"}]},
        ]), TURNS)


def test_an_invented_turn_id_is_rejected():
    with pytest.raises(cc.CorrectionRejected, match="unknown id"):
        cc.validate_reply(_reply([
            {"id": 1, "text": TURNS[0]["corrected"], "changes": []},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
            {"id": 99, "text": "invented", "changes": []},
        ]), TURNS)


def test_a_duplicated_turn_id_is_rejected():
    with pytest.raises(cc.CorrectionRejected, match="twice"):
        cc.validate_reply(_reply([
            {"id": 1, "text": TURNS[0]["corrected"], "changes": []},
            {"id": 1, "text": "again", "changes": []},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_an_undeclared_rewrite_is_rejected():
    """The whole audit trail rests on this: no changes declared means nothing changed.
    Without it, a rewrite with an empty changes list is invisible forever."""
    with pytest.raises(cc.CorrectionRejected, match="undeclared"):
        cc.validate_reply(_reply([
            {"id": 1, "text": "something completely different", "changes": []},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_a_declared_change_with_identical_text_is_rejected():
    """The mirror case: claiming a change that was not made makes the audit trail lie."""
    with pytest.raises(cc.CorrectionRejected, match="declared"):
        cc.validate_reply(_reply([
            {"id": 1, "text": TURNS[0]["corrected"],
             "changes": [{"from": "motor vision", "to": "Motorvessel", "reason": "r"}]},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_a_missing_turns_key_is_rejected():
    with pytest.raises(cc.CorrectionRejected, match="no turns"):
        cc.validate_reply({"result": "ok"}, TURNS)


def test_empty_text_is_rejected():
    """Never remove content: an emptied turn is content removal in its purest form."""
    with pytest.raises(cc.CorrectionRejected, match="empty"):
        cc.validate_reply(_reply([
            {"id": 1, "text": "", "changes": [{"from": "a", "to": "", "reason": "r"}]},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd server && python -m pytest tests/test_conversation_correct.py -v`
Expected: FAIL — `ImportError: cannot import name 'conversation_correct'`

- [ ] **Step 3: Write the implementation**

```python
# server/stt_proxy/conversation_correct.py
"""Correct each transmission using the rest of its conversation.

Runs after resolve_conversation, which has already decided who was speaking, and before
storage. Where the per-transmission pass sees one transmission, this one sees the exchange --
so a garbled opening call can be repaired from the shore station's clean answer in the very
next turn, which is information nothing in the system used before.

Every failure returns None and the conversation is stored uncorrected. A conversation is never
lost or half-rewritten because a model misbehaved.
"""


class CorrectionRejected(Exception):
    """The reply did not honour the contract, so none of it is used."""


def validate_reply(payload: dict, turns: list[dict]) -> dict[int, dict]:
    """{turn_id: {"text", "changes"}}, or raise.

    All-or-nothing on purpose. A reply that got one turn wrong has demonstrated it is not
    following the contract, and picking the good parts out of it is how a half-corrected
    conversation reaches the page.
    """
    rows = payload.get("turns") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise CorrectionRejected("reply has no turns")

    original = {t["id"]: (t.get("corrected") or t.get("text") or "") for t in turns}
    seen: dict[int, dict] = {}

    for row in rows:
        if not isinstance(row, dict):
            raise CorrectionRejected(f"turn entry is not an object: {row!r}")
        turn_id = row.get("id")
        if turn_id not in original:
            raise CorrectionRejected(f"unknown id {turn_id!r}")
        if turn_id in seen:
            raise CorrectionRejected(f"id {turn_id!r} appears twice")

        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise CorrectionRejected(f"id {turn_id!r} has empty text")

        changes = row.get("changes")
        if not isinstance(changes, list):
            raise CorrectionRejected(f"id {turn_id!r} has no changes list")

        unchanged = text.strip() == original[turn_id].strip()
        if unchanged and changes:
            raise CorrectionRejected(f"id {turn_id!r} declared a change it did not make")
        if not unchanged and not changes:
            raise CorrectionRejected(f"id {turn_id!r} is an undeclared rewrite")

        seen[turn_id] = {"text": text.strip(), "changes": changes}

    missing = sorted(set(original) - set(seen))
    if missing:
        raise CorrectionRejected(f"missing turns: {missing}")
    return seen
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd server && python -m pytest tests/test_conversation_correct.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/conversation_correct.py server/tests/test_conversation_correct.py
git commit -m "Reject a correction reply that breaks its contract"
```

---

### Task 6: The prompt and the pass

**Files:**
- Modify: `server/stt_proxy/conversation_correct.py`
- Modify: `server/tests/test_conversation_correct.py`

**Interfaces:**
- Consumes: `llm.complete`, `llm.strip_code_fence`, `llm.LLMError` (Task 2);
  `fewshot.load_examples`, `fewshot.render_examples` (Task 4); `validate_reply` (Task 5).
- Produces: `SYSTEM_PROMPT: str`; `render_input(turns: list[dict], vessel: str | None) -> str`;
  `correct_conversation(turns: list[dict], vessel: str | None) -> dict[int, dict] | None`.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_conversation_correct.py
from stt_proxy import llm  # noqa: E402


def test_the_input_lists_turns_with_ids_and_the_resolved_vessel():
    text = cc.render_input(TURNS, "EXAMPLE TRADER")
    assert "1. Maas Approach, motor vision Example Trader." in text
    assert "EXAMPLE TRADER" in text


def test_the_input_says_so_when_nobody_was_identified():
    text = cc.render_input(TURNS, None)
    assert "unidentified" in text


def test_correct_conversation_returns_validated_corrections(monkeypatch):
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k: (
        '{"turns": [{"id": 1, "text": "Maas Approach, Motorvessel Example Trader.",'
        ' "changes": [{"from": "motor vision", "to": "Motorvessel", "reason": "shore"}]},'
        ' {"id": 2, "text": "Motorvessel Example Trader, Maas Approach.", "changes": []}]}'))
    got = cc.correct_conversation(TURNS, "EXAMPLE TRADER")
    assert got[1]["text"] == "Maas Approach, Motorvessel Example Trader."


def test_a_fenced_reply_is_still_accepted(monkeypatch):
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k: (
        '```json\n{"turns": [{"id": 1, "text": "Maas Approach, motor vision Example Trader.",'
        ' "changes": []}, {"id": 2, "text": "Motorvessel Example Trader, Maas Approach.",'
        ' "changes": []}]}\n```'))
    assert cc.correct_conversation(TURNS, None) is not None


def test_a_provider_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise llm.LLMError("timeout")
    monkeypatch.setattr(cc.llm, "complete", boom)
    assert cc.correct_conversation(TURNS, "EXAMPLE TRADER") is None


def test_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k: "not json at all")
    assert cc.correct_conversation(TURNS, None) is None


def test_a_contract_violation_returns_none(monkeypatch):
    """Rejected means the conversation is stored uncorrected, not partly corrected."""
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k:
                        '{"turns": [{"id": 1, "text": "x", "changes": []}]}')
    assert cc.correct_conversation(TURNS, None) is None


def test_no_turns_needs_no_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the model for an empty exchange")
    monkeypatch.setattr(cc.llm, "complete", boom)
    assert cc.correct_conversation([], None) is None


def test_the_prompt_forbids_naming_a_turn_that_named_nobody():
    assert "named nobody" in cc.SYSTEM_PROMPT.lower()


def test_the_prompt_keeps_digit_sequences_as_transcribed():
    assert "one three zero zero" in cc.SYSTEM_PROMPT.lower()
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd server && python -m pytest tests/test_conversation_correct.py -v`
Expected: FAIL — `AttributeError: module 'stt_proxy.conversation_correct' has no attribute 'render_input'`

- [ ] **Step 3: Write the implementation**

Add to the top of `server/stt_proxy/conversation_correct.py`, below the docstring:

```python
import json
import os

from stt_proxy import fewshot, llm

CONVERSATION_CORRECT = os.environ.get("CONVERSATION_CORRECT", "off").strip().lower() == "on"
CONVERSATION_CORRECT_PROVIDER = os.environ.get("CONVERSATION_CORRECT_PROVIDER", "anthropic").strip()
CONVERSATION_CORRECT_MODEL = os.environ.get("CONVERSATION_CORRECT_MODEL",
                                            "claude-haiku-4-5-20251001").strip()
CONVERSATION_CORRECT_FEWSHOT = os.environ.get("CONVERSATION_CORRECT_FEWSHOT", "on").strip().lower() != "off"
CONVERSATION_CORRECT_TIMEOUT_S = float(os.environ.get("CONVERSATION_CORRECT_TIMEOUT_S", "60"))

_failures_logged = 0
_FAILURE_LOG_LIMIT = 3


SYSTEM_PROMPT = """\
You are given the transmissions of ONE VHF radio exchange near Rotterdam (Maas Approach /
Rotterdam VTS), in time order, already transcribed, together with the vessel that has already
been identified for this exchange.

Correct the transcription of each turn using the rest of the exchange. You are NOT identifying
anybody -- that is decided already. You are NOT improving anyone's English.

Return ONLY raw JSON, no markdown:
{"turns": [{"id": <id>, "text": "<corrected>",
            "changes": [{"from": "<original>", "to": "<replacement>", "reason": "<short>"}]}]}

Contract:
- Every id you were given appears exactly once. Never invent an id.
- If you change nothing in a turn, return its text byte-identical and "changes": [].
- If you change anything, every substitution must appear in "changes". An undeclared
  rewrite is rejected and the whole reply is discarded.

Rules:
1. The shore station's rendition wins. For a vessel name or a type word, prefer the shore
   station's version over the vessel's own opening call: the station reads it off a screen,
   while the opening call is the noisiest turn on the channel. "motor vision" answered by
   "Motorvessel" means the opening call said Motorvessel.
2. Propagate the identified vessel's name into turns that name the vessel -- but ONLY where a
   name was actually spoken. A turn that named nobody must still name nobody. Never add a name
   to a turn that did not have one.
3. Align a readback ONLY when it is garbled. A readback that is clean but different is a real
   disagreement -- a vessel getting it wrong -- and the operator needs to see it. Never edit a
   clean readback into agreement.
4. Numbers spoken digit by digit ("one three zero zero") survive the channel well and stay
   exactly as transcribed. Repair a digit only when the same value appears cleanly elsewhere
   in this exchange. Never reformat in either direction: not "one three zero zero" into
   "1300", not "4.7" into "four point seven".
5. Make the smallest edit that fixes a clear error. If a word is merely unusual, or you are
   unsure what was meant, leave it exactly as it is.
6. Never remove content. Every utterance must survive into the corrected text, even if it is
   garbled, redundant or a filler.
7. Keep the speaker's own words, word order, grammar and disfluencies.
8. Examples, when given, demonstrate the style of correction. They are NOT a list of ships
   that might be speaking. Never take a vessel name from an example.
"""
```

Then append the rendering and the pass:

```python
def render_input(turns: list[dict], vessel: str | None) -> str:
    lines = [f"[VESSEL] {vessel or 'unidentified'}", "", "[TRANSMISSIONS]"]
    for turn in turns:
        text = turn.get("corrected") or turn.get("text") or ""
        lines.append(f"  {turn['id']}. {text}")
    return "\n".join(lines)


def _log_failure(reason: str) -> None:
    """Rate-limited, but never silent: a prompt that has started failing on every call must
    be visible. Same shape as _report_unrecognised_frame."""
    global _failures_logged
    if _failures_logged < _FAILURE_LOG_LIMIT:
        _failures_logged += 1
        suffix = " (further failures suppressed)" if _failures_logged == _FAILURE_LOG_LIMIT else ""
        print(f"  [conv-correct] not applied: {reason}{suffix}", flush=True)


def correct_conversation(turns: list[dict], vessel: str | None) -> dict[int, dict] | None:
    """{turn_id: {"text", "changes"}} for one exchange, or None if it could not be corrected."""
    if not turns:
        return None

    system = SYSTEM_PROMPT
    if CONVERSATION_CORRECT_FEWSHOT:
        rendered = fewshot.render_examples(fewshot.load_examples())
        if rendered:
            system = f"{system}\n\n{rendered}\n"

    try:
        reply = llm.complete(
            system, render_input(turns, vessel),
            provider=CONVERSATION_CORRECT_PROVIDER,
            model=CONVERSATION_CORRECT_MODEL,
            temperature=0,
            timeout_s=CONVERSATION_CORRECT_TIMEOUT_S,
        )
        payload = json.loads(llm.strip_code_fence(reply))
    except (llm.LLMError, ValueError) as exc:
        _log_failure(str(exc))
        return None

    try:
        return validate_reply(payload, turns)
    except CorrectionRejected as exc:
        _log_failure(str(exc))
        return None
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd server && python -m pytest tests/test_conversation_correct.py -v`
Expected: 18 passed

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/conversation_correct.py server/tests/test_conversation_correct.py
git commit -m "Correct a transmission from the conversation around it"
```

---

### Task 7: Wire the pass into storage, behind the flag

**Files:**
- Modify: `server/stt_proxy/conversations.py` (`_store_resolved`, `_resolve_window`)
- Modify: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: `conversation_correct.correct_conversation`, `conversation_correct.CONVERSATION_CORRECT`.
- Produces: stored turn dicts gain optional `conv: str` and `changes: list[dict]`.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_whisper_proxy.py
from stt_proxy import conversation_correct as cc  # noqa: E402


def _window(when):
    return [
        {"id": 1, "time": when, "channel": "160,650",
         "text": "raw one", "corrected": "Maas Approach, motor vision Example Trader.",
         "live_vessel": None},
        {"id": 2, "time": when, "channel": "160,650",
         "text": "raw two", "corrected": "Motorvessel Example Trader, Maas Approach.",
         "live_vessel": None},
    ]


def test_storage_keeps_the_verbatim_text_beside_the_correction(monkeypatch, tmp_path):
    """The audit trail is the whole basis for allowing a rewrite at all."""
    when = datetime.datetime(2026, 8, 7, 10, 14, 15)
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    corrections = {1: {"text": "Maas Approach, Motorvessel Example Trader.",
                       "changes": [{"from": "motor vision", "to": "Motorvessel",
                                    "reason": "shore station"}]}}
    conversations._store_resolved(
        _window(when),
        [{"chunk_ids": [1, 2], "vessel": "EXAMPLE TRADER", "mmsi": "1",
          "evidence": "e", "confidence": "high"}],
        corrections)
    turns = conversations._resolved[0]["turns"]
    assert turns[0]["text"] == "Maas Approach, motor vision Example Trader."
    assert turns[0]["conv"] == "Maas Approach, Motorvessel Example Trader."
    assert turns[0]["changes"][0]["to"] == "Motorvessel"
    assert "conv" not in turns[1], "an uncorrected turn stores no conv field"


def test_storage_without_corrections_is_unchanged(monkeypatch):
    when = datetime.datetime(2026, 8, 7, 10, 14, 15)
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    conversations._store_resolved(
        _window(when),
        [{"chunk_ids": [1, 2], "vessel": None, "mmsi": None,
          "evidence": "e", "confidence": "low"}],
        None)
    turns = conversations._resolved[0]["turns"]
    assert "conv" not in turns[0]
    assert turns[0]["text"] == "Maas Approach, motor vision Example Trader."


def test_the_pass_does_not_run_while_the_flag_is_off(monkeypatch):
    """Default off: production behaviour must be byte-identical until the bake-off scores it."""
    def boom(*a, **k):
        raise AssertionError("correct_conversation must not be called with the flag off")
    monkeypatch.setattr(cc, "CONVERSATION_CORRECT", False)
    monkeypatch.setattr(cc, "correct_conversation", boom)
    monkeypatch.setattr(conversations, "resolve_conversation",
                        lambda w: [{"chunk_ids": [1, 2], "vessel": None, "mmsi": None,
                                    "evidence": "e", "confidence": "low"}])
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    conversations._resolve_window(_window(datetime.datetime(2026, 8, 7, 10, 14, 15)))
    assert "conv" not in conversations._resolved[0]["turns"][0]


def test_a_failed_correction_still_stores_the_conversation(monkeypatch):
    """Never lose a conversation because a model misbehaved."""
    monkeypatch.setattr(cc, "CONVERSATION_CORRECT", True)
    monkeypatch.setattr(cc, "correct_conversation", lambda turns, vessel: None)
    monkeypatch.setattr(conversations, "resolve_conversation",
                        lambda w: [{"chunk_ids": [1, 2], "vessel": None, "mmsi": None,
                                    "evidence": "e", "confidence": "low"}])
    monkeypatch.setattr(conversations, "_resolved", [])
    monkeypatch.setattr(conversations, "_save_conversations", lambda: None)
    conversations._resolve_window(_window(datetime.datetime(2026, 8, 7, 10, 14, 15)))
    assert len(conversations._resolved) == 1
    assert conversations._resolved[0]["turns"][0]["text"]
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd server && python -m pytest tests/test_whisper_proxy.py -v -k "verbatim or without_corrections or flag_is_off or failed_correction"`
Expected: FAIL — `TypeError: _store_resolved() takes 2 positional arguments but 3 were given`

- [ ] **Step 3: Modify `_store_resolved`**

Change the signature and the turn construction in `server/stt_proxy/conversations.py`:

```python
def _store_resolved(window: list[dict], exchanges: list[dict],
                    corrections: dict[int, dict] | None = None) -> None:
    """Record resolved exchanges together with the transmissions they cover, verbatim.

    `corrections` maps chunk id to the conversation pass's output. It is stored ALONGSIDE the
    verbatim text, never over it: a reader must always be able to recover what was heard.
    """
    corrections = corrections or {}
    by_id = {c["id"]: c for c in window}
    rows = []
    for ex in exchanges:
        turns = [by_id[i] for i in ex["chunk_ids"] if i in by_id]
        if not turns:
            continue
        stored_turns = []
        for t in turns:
            row = {"time": t["time"].strftime("%H:%M:%S"),
                   "text": t.get("corrected") or t.get("text", ""),
                   "raw": t.get("text", ""),
                   "live_vessel": t.get("live_vessel")}
            fix = corrections.get(t["id"])
            # Absent rather than equal-to-text when nothing was corrected, so the page can
            # tell "not corrected" from "corrected to the same thing".
            if fix and fix.get("changes"):
                row["conv"] = fix["text"]
                row["changes"] = fix["changes"]
            stored_turns.append(row)
        rows.append({
            **{k: v for k, v in ex.items() if k != "chunk_ids"},
            "channel": turns[0]["channel"],
            "start": turns[0]["time"].strftime("%Y-%m-%d %H:%M:%S"),
            "end":   turns[-1]["time"].strftime("%Y-%m-%d %H:%M:%S"),
            "turns": stored_turns,
        })
    if not rows:
        return
    with _resolved_lock:
        _resolved.extend(rows)
        del _resolved[:-CONVERSATIONS_KEEP]
    _save_conversations()
```

- [ ] **Step 4: Modify `_resolve_window`**

```python
def _resolve_window(window: list[dict]) -> None:
    exchanges = resolve_conversation(window)

    # Correction runs per EXCHANGE, not per window: a window can hold several unrelated
    # exchanges, and letting one conversation's context edit another's turns is the failure
    # this split exists to prevent.
    corrections: dict[int, dict] = {}
    if conversation_correct.CONVERSATION_CORRECT:
        by_id = {c["id"]: c for c in window}
        for ex in exchanges:
            turns = [by_id[i] for i in ex["chunk_ids"] if i in by_id]
            fixed = conversation_correct.correct_conversation(turns, ex.get("vessel"))
            if fixed:
                corrections.update(fixed)

    _store_resolved(window, exchanges, corrections)
    ts = datetime.datetime.now().strftime("%H:%M:%S")
    for ex in exchanges:
        who = ex.get("vessel") or "unidentified"
        via = " via callsign" if ex.get("via_callsign") else ""
        print(f"[{ts}] [conv] {len(ex['chunk_ids'])} turns -> {who}{via} ({ex.get('confidence')})", flush=True)
```

Add the import at the top of `conversations.py`, beside the existing `stt_proxy` imports:

```python
from stt_proxy import conversation_correct
```

- [ ] **Step 5: Run the full suite**

Run: `cd server && python -m pytest tests -q`
Expected: all pass, including the 424 that passed before.

- [ ] **Step 6: Commit**

```bash
git add server/stt_proxy/conversations.py server/tests/test_whisper_proxy.py
git commit -m "Store the conversation correction beside the verbatim text"
```

---

### Task 8: Show corrections on the conversations page

**Files:**
- Modify: `server/stt_proxy/conversations.py` (`render_conversations_page`)
- Modify: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: stored turns with optional `conv` and `changes` (Task 7).
- Produces: nothing other tasks depend on.

- [ ] **Step 1: Write the failing tests**

```python
# append to server/tests/test_whisper_proxy.py
def _row_with_correction():
    return [{
        "vessel": "EXAMPLE TRADER", "mmsi": "1", "confidence": "high", "evidence": "e",
        "channel": "160,650", "start": "2026-08-07 10:14:15", "end": "2026-08-07 10:14:19",
        "turns": [
            {"time": "10:14:15", "text": "Maas Approach, motor vision Example Trader.",
             "raw": "r", "live_vessel": None,
             "conv": "Maas Approach, Motorvessel Example Trader.",
             "changes": [{"from": "motor vision", "to": "Motorvessel",
                          "reason": "shore station rendition"}]},
            {"time": "10:14:19", "text": "Motorvessel Example Trader, Maas Approach.",
             "raw": "r", "live_vessel": None},
        ],
    }]


def test_the_page_shows_the_corrected_text():
    html = conversations.render_conversations_page(_row_with_correction())
    assert "Maas Approach, Motorvessel Example Trader." in html


def test_the_page_keeps_the_original_recoverable():
    """The rewrite was allowed on the condition that nothing is silently overwritten."""
    html = conversations.render_conversations_page(_row_with_correction())
    assert "motor vision" in html
    assert "shore station rendition" in html


def test_the_page_counts_the_corrections():
    html = conversations.render_conversations_page(_row_with_correction())
    assert "1 corrected" in html


def test_an_uncorrected_conversation_shows_no_badge_and_no_marked_text():
    """Assert on the badge and the marker class, NOT on the word 'corrected': the page's
    static explanatory paragraph contains that word on every render."""
    rows = _row_with_correction()
    for turn in rows[0]["turns"]:
        turn.pop("conv", None)
        turn.pop("changes", None)
    html = conversations.render_conversations_page(rows)
    assert "fixedcount" not in html
    assert 'class="fixed"' not in html
    assert "Maas Approach, motor vision Example Trader." in html
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd server && python -m pytest tests/test_whisper_proxy.py -v -k "corrected_text or recoverable or counts_the or uncorrected_conversation"`
Expected: FAIL — the corrected text is not in the HTML.

- [ ] **Step 3: Modify the turn loop in `render_conversations_page`**

Replace the existing `for t in row.get("turns", []):` block with:

```python
        turns = []
        corrected_count = 0
        for t in row.get("turns", []):
            live = t.get("live_vessel")
            note = (f'<span class="was">live: {_html_escape(live)}</span>'
                    if live and live != vessel else "")

            shown = t.get("conv") or t.get("text", "")
            changes = t.get("changes") or []
            if t.get("conv"):
                corrected_count += 1
                # title= rather than a second line: the original stays one hover away without
                # doubling the length of every conversation on the page.
                detail = "; ".join(
                    f'{c.get("from","")} -> {c.get("to","")} ({c.get("reason","")})'
                    for c in changes)
                body = (f'<span class="fixed" title="was: {_html_escape(t.get("text",""))}'
                        f' &#10;{_html_escape(detail)}">{_html_escape(shown)}</span>')
            else:
                body = _html_escape(shown)

            turns.append(f'<li><span class="t">{_html_escape(t.get("time",""))}</span> '
                         f'{body} {note}</li>')

        fixed_badge = (f'<span class="badge fixedcount">{corrected_count} corrected</span>'
                       if corrected_count else "")
```

Add `{fixed_badge}` to the header block, directly after the existing confidence badge:

```python
        <span class="badge {_html_escape(conf)}">{badge}</span>{fixed_badge}
```

Add to the `<style>` block:

```css
 .fixed {{ border-bottom: 1px dotted #2c7; cursor: help; }}
 .badge.fixedcount {{ background: #d4edda; }}
```

Change the explanatory paragraph, which currently promises the text is never rewritten:

```python
<p style="color:#666;font-size:.9em">Identity is decided after each exchange ends, from the whole
exchange rather than one transmission. Text marked with a dotted underline was corrected using
the rest of the conversation &mdash; hover it to see what was heard and why it changed.</p>
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd server && python -m pytest tests/test_whisper_proxy.py -q`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/conversations.py server/tests/test_whisper_proxy.py
git commit -m "Show what the conversation pass changed, and what was heard"
```

---

### Task 9: The benchmark

**Files:**
- Create: `server/bench_conversation_correct.py`
- Test: `server/tests/test_bench_conversation_correct.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `clip_index.load_clip_index`, `clip_index.clip_for_time` (Task 1);
  `bench._normalize`, `bench.load_references` (existing).
- Produces: `score_turns(rows: list[dict], references: dict[str, str], index: dict,
  use_conv: bool) -> dict` with keys `wer`, `errors`, `ref_words`, `scored`, `unmatched`,
  `invented`.

- [ ] **Step 1: Write the failing tests**

```python
# server/tests/test_bench_conversation_correct.py
"""Tests for bench_conversation_correct.py: scoring the pass on three numbers, not one."""

import datetime
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from bench_conversation_correct import score_turns, wer_counts  # noqa: E402


INDEX = {"0000": datetime.datetime(2026, 8, 7, 10, 14, 15),
         "0001": datetime.datetime(2026, 8, 7, 10, 14, 19)}
REFERENCES = {"0000": "Maas Approach, Motorvessel Example Trader.",
              "0001": "Motorvessel Example Trader, Maas Approach."}


def _rows(conv=None):
    turn = {"time": "10:14:15", "text": "Maas Approach, motor vision Example Trader."}
    if conv:
        turn = {**turn, "conv": conv, "changes": [{"from": "motor vision", "to": "Motorvessel",
                                                   "reason": "shore"}]}
    return [{"start": "2026-08-07 10:14:15", "turns": [
        turn,
        {"time": "10:14:19", "text": "Motorvessel Example Trader, Maas Approach."},
    ]}]


def test_wer_counts_substitutions_insertions_and_deletions():
    assert wer_counts(["a", "b", "c"], ["a", "b", "c"]) == (0, 3)
    assert wer_counts(["a", "b", "c"], ["a", "x", "c"]) == (1, 3)
    assert wer_counts(["a", "b", "c"], ["a", "c"]) == (1, 3)
    assert wer_counts(["a", "b"], ["a", "b", "c"]) == (1, 2)


def test_the_baseline_scores_the_live_text():
    got = score_turns(_rows(), REFERENCES, INDEX, use_conv=False)
    assert got["scored"] == 2
    assert got["errors"] == 2      # "motor vision" against "Motorvessel"
    assert got["unmatched"] == 0


def test_the_corrected_arm_scores_the_conv_text():
    got = score_turns(_rows(conv="Maas Approach, Motorvessel Example Trader."),
                      REFERENCES, INDEX, use_conv=True)
    assert got["errors"] == 0
    assert got["wer"] == 0.0


def test_a_turn_with_no_clip_is_reported_not_silently_dropped():
    """A turn scored against the wrong clip reads as a quality change that never happened."""
    rows = [{"start": "2026-08-07 10:14:15",
             "turns": [{"time": "23:59:59", "text": "orphan"}]}]
    got = score_turns(rows, REFERENCES, INDEX, use_conv=False)
    assert got["unmatched"] == 1
    assert got["scored"] == 0


def test_invented_words_are_counted_separately():
    """WER barely notices a fluent wrong answer, which is this feature's central risk."""
    got = score_turns(_rows(conv="Maas Approach, Motorvessel Example Trader proceeding inbound."),
                      REFERENCES, INDEX, use_conv=True)
    assert got["invented"] >= 2   # "proceeding", "inbound"


def test_the_corrected_arm_falls_back_to_live_text_when_a_turn_was_not_corrected():
    got = score_turns(_rows(), REFERENCES, INDEX, use_conv=True)
    assert got["scored"] == 2
```

- [ ] **Step 2: Run the tests and verify they fail**

Run: `cd server && python -m pytest tests/test_bench_conversation_correct.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bench_conversation_correct'`

- [ ] **Step 3: Write the implementation**

```python
# server/bench_conversation_correct.py
"""Score the conversation-correction pass: WER, invented content, and what could not be joined.

Three numbers, not one. WER alone cannot see this feature's central risk, which is a fluent
wrong answer -- a readback confidently rewritten into agreement scores well and is exactly the
failure the operator must not be handed silently.

Usage:
    py bench_conversation_correct.py --conversations stt_proxy/conversations.json \\
        --references references-2026-08-07-verified.txt \\
        --captures "D:/SDR/SdrSharp/Plugins/SttPlugin/captures/2026-08-07"
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench
from clip_index import clip_for_time, load_clip_index

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def wer_counts(reference: list[str], hypothesis: list[str]) -> tuple[int, int]:
    """(edit distance, reference length) over word tokens."""
    n, m = len(reference), len(hypothesis)
    previous = list(range(m + 1))
    for i in range(1, n + 1):
        current = [i] + [0] * m
        for j in range(1, m + 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1,
                             previous[j - 1] + (reference[i - 1] != hypothesis[j - 1]))
        previous = current
    return previous[m], n


def _turn_time(row: dict, turn: dict) -> datetime.datetime | None:
    try:
        day = datetime.datetime.strptime(row.get("start", ""), _TS_FMT).date()
        clock = datetime.datetime.strptime(turn.get("time", ""), "%H:%M:%S").time()
    except ValueError:
        return None
    return datetime.datetime.combine(day, clock)


def score_turns(rows: list[dict], references: dict[str, str], index: dict,
                use_conv: bool) -> dict:
    """Pooled WER plus invented-word count for one arm."""
    errors = ref_words = scored = unmatched = invented = 0

    for row in rows:
        for turn in row.get("turns", []):
            when = _turn_time(row, turn)
            clip = clip_for_time(index, when) if when else None
            if clip is None or clip not in references:
                unmatched += 1
                continue
            text = (turn.get("conv") or turn.get("text", "")) if use_conv else turn.get("text", "")
            reference = bench._normalize(references[clip])
            hypothesis = bench._normalize(text)
            e, n = wer_counts(reference, hypothesis)
            errors += e
            ref_words += n
            scored += 1
            # Words present in the hypothesis and absent from the reference. Crude, and
            # deliberately so: it over-counts a correct synonym and that is the safe
            # direction for a risk metric.
            invented += sum(1 for w in hypothesis if w not in set(reference))

    return {"wer": (100.0 * errors / ref_words) if ref_words else 0.0,
            "errors": errors, "ref_words": ref_words, "scored": scored,
            "unmatched": unmatched, "invented": invented}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversations", required=True)
    ap.add_argument("--references", required=True)
    ap.add_argument("--captures", required=True, help="one capture DAY directory")
    args = ap.parse_args(argv)

    rows = json.loads(Path(args.conversations).read_text(encoding="utf-8"))
    references = bench.load_references(Path(args.references))
    index = load_clip_index(args.captures)
    if not index:
        print(f"no index.jsonl under {args.captures}", file=sys.stderr)
        return 1

    print(f"{'arm':>12} {'WER':>8} {'errors':>8} {'scored':>8} {'unmatched':>10} {'invented':>9}")
    print("-" * 60)
    for label, use_conv in (("baseline", False), ("corrected", True)):
        s = score_turns(rows, references, index, use_conv)
        print(f"{label:>12} {s['wer']:>7.2f}% {s['errors']:>8} {s['scored']:>8} "
              f"{s['unmatched']:>10} {s['invented']:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests and verify they pass**

Run: `cd server && python -m pytest tests/test_bench_conversation_correct.py -v`
Expected: 6 passed

- [ ] **Step 5: Gitignore the results**

Append to `.gitignore`:
```
server/bench-conv-correct-*.json
```

- [ ] **Step 6: Commit**

```bash
git add server/bench_conversation_correct.py server/tests/test_bench_conversation_correct.py .gitignore
git commit -m "Score the conversation pass on WER, invented content and join failures"
```

---

### Task 10: Run the bake-off and decide the default

The only task that spends API credit. Ask before running it — it uses the operator's keys.

**Files:**
- Modify: `server/stt_proxy/conversation_correct.py` (record the result in the flag's comment)
- Modify: `docs/superpowers/specs/2026-08-10-conversation-correction-design.md` (record the outcome)

**Interfaces:**
- Consumes: everything above.
- Produces: a decision on `CONVERSATION_CORRECT`'s default.

- [ ] **Step 1: Confirm the baseline join is clean**

Run:
```bash
cd server && python bench_conversation_correct.py \
  --conversations stt_proxy/conversations.json \
  --references references-2026-08-07-verified.txt \
  --captures "D:/SDR/SdrSharp/Plugins/SttPlugin/captures/2026-08-07"
```
Expected: `unmatched` is small relative to `scored`. If most turns are unmatched, stop: the
join is wrong and every number after this is meaningless.

- [ ] **Step 2: Re-resolve the corpus once per arm**

For each combination of `CONVERSATION_CORRECT_MODEL` in
{`claude-haiku-4-5-20251001`, `claude-sonnet-5`} and `CONVERSATION_CORRECT_FEWSHOT` in
{`on`, `off`}, with `CONVERSATION_CORRECT=on`, regenerate the stored conversations and score
them. Examples must come from a corpus disjoint from the one being scored: set
`CONVERSATION_FEWSHOT_FILE` to examples built from `references-2026-07-28`, and score against
`references-2026-08-07-verified`.

- [ ] **Step 3: Check identification did not move**

Run:
```bash
cd server && python bench_identify.py --labels identification-labels-2026-08-07-verified.txt \
  --resolve --repeats 3
```
Expected: precision and recall within the spread already recorded (85.7% / 76.5%, precision
spread 2.9 points). This pass runs after resolution, so any movement means it is wired wrong.

- [ ] **Step 4: Decide and record**

Turn the default on only if all four success criteria from the spec hold: WER improves by more
than ~1 point on the held-out corpus, invented content does not rise, identification is
unmoved, and every failure path leaves a readable conversation. Otherwise leave it off and
record why, in the style of the `AIS_NAME_WORD_MATCH` comment.

- [ ] **Step 5: Commit**

```bash
git add server/stt_proxy/conversation_correct.py docs/superpowers/specs/2026-08-10-conversation-correction-design.md
git commit -m "Record the bake-off result for the conversation correction pass"
```

---

## Self-review notes

**Spec coverage.** Architecture and data flow → Task 7. Text layers → Tasks 7, 8. New modules →
Tasks 2, 4, 5, 6. Configuration table → Task 6. Correction contract → Task 5. Prompt rules →
Task 6. Few-shot, runtime loading and holdout → Tasks 4, 10. Display and audit → Task 8. Error
handling → Tasks 6, 7. Testing and the three numbers → Tasks 5–9. The turn-to-reference mapping
risk → Task 1, first, as the spec requires.

**One spec item deliberately deferred.** The spec's few-shot section describes examples drawn
from the references corpus; Task 4 builds the loader and the file format but does not build a
generator that assembles examples from `references-*` plus `bench-results-*`. That is a
curation step, done once by hand into the gitignored file, and building a generator before
knowing whether examples help at all would be work ahead of evidence. Task 10 covers the
holdout discipline either way.
