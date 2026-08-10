# Conversation-level correction pass — design

**Date:** 2026-08-10
**Status:** design agreed, not implemented
**Feature:** "Feature 1" of the three queued after the first release

## The problem

Two passes run today. The per-transmission pass (`identify.py`, Claude at `temperature=0`)
extracts the vessel and rewrites that transmission's text, seeing one transmission at a time.
The conversation pass (`conversations.py::resolve_conversation`) then splits a window into
exchanges and decides who was speaking, seeing the whole exchange — but it deliberately does
not touch the text. `_store_resolved` says so ("Text is copied straight from the journal,
never from the resolver") and the conversations page repeats it to the reader.

So nothing in the system corrects a transmission using the rest of its conversation, and that
is where the remaining information is. Two observed cases:

- A vessel opens with a garbled type word or name and the shore station answers with a clean
  one — "Motor Vision" against the station's "Motorvessel". The clean rendition is sitting in
  the next turn and is never used.
- On 2026-08-09 23:28 the per-transmission display matched "Rotterdam Neptune" to the Belgian
  NEPTUNE while the conversation resolver had ADMIRAL NEPTUNE right at high confidence. The
  resolver's verdict never reaches the turns.

This generalises past what the regex table can do. A regex rule fixes a corruption somebody
already observed — `motor vision` was added on 2026-08-10 for exactly that reason. Cross-turn
context fixes corruptions nobody has seen yet.

## Decisions

| decision | choice | why |
|---|---|---|
| Rewrite scope | Rewrite turn text, keep the verbatim original | Readback repair needs rewriting; the audit trail keeps "never silently overwrite" |
| Delivery | Conversations page only | The pass runs after the exchange closes, so it cannot fix what SDR# already displayed. No C# or protocol work |
| Architecture | A second pass after resolution | Identity and transcription are different jobs; keeping them apart protects a measured 85.7%/76.5% identification from a text-prompt change |
| Model | Provider-agnostic, chosen by bake-off | Latency no longer binds off the live path, so the 08-03 reasoning for Haiku does not carry over unexamined |
| Few-shot examples | Benched arm, runtime-loaded, holdout split | Plausible but unproven; cheap to test; must not contaminate the score |

### Alternatives rejected

**Extending the resolver to also return corrected text.** One call instead of two, and it
already has the exchange and the candidate list. Rejected because it reverses that prompt's
rule 9 ("Do NOT return transcriptions") and merges two jobs into one prompt — the 2026-08-03
finding was that the existing chain was "prompted for the wrong job", and a prompt doing two
jobs does both worse. It also destroys attribution: a text regression could move identification
and there would be no way to tell which change caused it.

**Deterministic cross-turn consensus instead of a model.** Cluster near-identical tokens across
turns and promote the best-supported rendition; "Motor Vision"/"Motorvessel" collapses with no
API call, deterministically and testably. Rejected as the primary mechanism because "cleanest
rendition" needs a lexicon to define, the shore station is not always right, and it cannot do
readback repair or the sense-making the feature is for. Its insight survives as prompt rule 1
and as the audit trail.

## Architecture

```
_conversation_reaper
  └─ _take_closed_windows()
       └─ _resolve_window(window)
            ├─ resolve_conversation(window)        -> exchanges (identity)   [unchanged]
            ├─ correct_conversation(turns, vessel) -> per-turn corrections   [NEW]
            └─ _store_resolved(window, exchanges, corrections)
```

Correction runs before storage so the stored record is complete on first write: no second
write path, no partially-corrected rows on disk, and the page stays a pure render of stored
data, which is how it works today.

### Text layers

The record carries two text layers already; the pass adds a third rather than overwriting
either.

| field | meaning |
|---|---|
| `raw` | what Whisper produced |
| `text` | the per-transmission pass |
| `conv` | this pass, using the whole exchange |
| `changes` | `[{from, to, reason}]`, every substitution |

The page renders `conv` when present and falls back to `text`, so rows stored before this
feature keep rendering unchanged.

### New modules

`conversations.py` is 762 lines and the second-largest file in the project, so this does not go
inside it.

- **`stt_proxy/conversation_correct.py`** — the pass. Builds the prompt, calls the model,
  validates the reply, returns corrections. Knows nothing about storage or HTML.
- **`stt_proxy/llm.py`** — provider interface, `complete(system, user, *, model, temperature)
  -> str`, with Anthropic and OpenRouter implementations. `claude.py` becomes one
  implementation behind it. This is what lets the bake-off sweep providers without touching
  the pass.
- **`stt_proxy/fewshot.py`** — loads examples from the gitignored references file at runtime,
  with a small synthetic fallback set in source.

### Configuration

`CONVERSATION_CORRECT` defaults **off** until the bake-off scores it, matching how
`AIS_NAME_FILTER` and `AIS_NAME_WORD_MATCH` are handled in this project. Provider, model and
few-shot are separately configurable so the bake-off can sweep them without code changes:

| variable | default | meaning |
|---|---|---|
| `CONVERSATION_CORRECT` | `off` | the pass runs at all |
| `CONVERSATION_CORRECT_PROVIDER` | `anthropic` | `anthropic` or `openrouter` |
| `CONVERSATION_CORRECT_MODEL` | provider default | model id passed to `llm.complete` |
| `CONVERSATION_CORRECT_FEWSHOT` | `on` | include runtime-loaded examples |
| `CONVERSATION_CORRECT_TIMEOUT_S` | `60` | bound on one call |

## The correction contract

`correct_conversation(turns, vessel)` takes the turns of **one exchange** plus the resolved
vessel name. Per-exchange, not per-window: the premise is that these turns are one
conversation, and a window can hold several unrelated exchanges that must not contaminate each
other.

The reply is JSON keyed by turn id, so a dropped or invented turn is detectable rather than
silently truncating the conversation:

```json
{"turns": [{"id": 3,
            "text": "<corrected>",
            "changes": [{"from": "Motor Vision", "to": "Motorvessel",
                         "reason": "shore station rendition"}]}]}
```

Validation, before anything is stored:

1. Every input id appears exactly once.
2. No id that was not given.
3. **A turn with an empty `changes` list must come back byte-identical to its input.**

Rule 3 is the important one: it makes "rewrote something without declaring it" a hard error
caught at the boundary, rather than something discovered months later while reading a
transcript.

## The prompt

Job statement: correct the transcription of each turn using the rest of the exchange. Not
identity — that is decided already and handed in. Not style — the speaker's English is not
being improved.

It inherits the live prompt's invariants, which were won by measurement and are just as easy to
break here: minimal edit, never remove content, keep the speaker's word order and disfluencies,
`temperature=0`.

Four rules are new, and exist only because this pass has context the live one lacks:

1. **Shore-station rendition wins.** For a vessel name or type word, prefer the shore station's
   version over the vessel's opening call. The station reads it off a screen; the opening call
   is the noisiest turn on the channel.
2. **Propagate the resolved name, but only where a name was spoken.** A turn that named nobody
   must still name nobody afterwards. Mirrors live rule 6; it is the guard against the pass
   quietly attributing turns.
3. **Readbacks align only when garbled.** A readback that is clean but differs is
   operationally real — a vessel getting it wrong is something the operator wants to see. Only
   a garbled readback is aligned to the instruction.
4. **Digit-sequence numbers stay as transcribed.** Spoken digit by digit ("one three zero
   zero") they survive the channel well. A digit may be repaired only when the same value
   appears cleanly elsewhere in the exchange. No reformatting in either direction — the live
   prompt's rule d, restated.

Plus the roster guard: few-shot examples are style demonstrations, **not** a list of ships that
might be speaking. This is the failure mode live rule 5 already guards against for AIS hints.

## Few-shot examples

Examples are loaded at runtime from the gitignored references file, never baked into source.
The CI transcript gate is a list of known filenames, not a content scan, so pasting example
transcripts into a module would pass the gate and still commit received radio traffic to a repo
intended for publication (NL Telecommunicatiewet 18.13 / ITU RR 17.3). Runtime loading also
lets the examples be edited without a code change.

A synthetic fallback set lives in source for when the references file is absent, so the feature
works on a fresh checkout.

Examples are drawn from a corpus disjoint from the one being scored — examples from
`references-2026-07-28`, scoring on `references-2026-08-07-verified`, or a split within one
corpus. Without that split the model is shown the answers to its own exam, the same class of
error as the project's existing "never score a part-draft corpus" rule.

Examples are chosen for structural lessons — shore-station-rendition-wins, readback alignment,
digit-sequence numbers left alone — rather than for the ships they mention.

## Display and audit

The page renders `conv` when present and `text` otherwise. Changed spans carry a class and a
`title` attribute holding the original and the reason, so hovering shows what was replaced:
server-rendered, no JavaScript, matching how the page works today. The existing red `live:`
note stays. Each conversation header gains a change count, so a heavily rewritten conversation
is visible at a glance instead of only on inspection.

A reader can always recover what was actually heard.

## Error handling

Every failure degrades to **storing the conversation uncorrected**: `conv` absent, page falls
back to `text`. A conversation is never lost or left partially rewritten because a model
misbehaved. This covers provider timeout, malformed JSON, and any validation failure above.

Each failure is logged rate-limited, in the style of `_report_unrecognised_frame`, with the
reason. A silent fallback would hide a prompt that has begun failing on every call — the same
class of fault as the AIS feed that failed by going quiet.

The call is bounded by `CONVERSATION_CORRECT_TIMEOUT_S`, default 60 seconds — far longer than
the live path's 15, because nothing is waiting on it, and still short enough that a hung
provider cannot stall the reaper thread indefinitely. Correction runs exactly once, at storage,
so a reaper re-run cannot double-correct.

## Testing and measurement

### Unit tests

Written test-first, against the real functions: validation rejects a dropped turn, an invented
id and an undeclared rewrite; an empty `changes` list forces byte-identical text; malformed
JSON falls back cleanly; a turn naming nobody stays unnamed; the provider interface swaps
without touching the pass. Fixtures stay short and synthetic-flavoured — test files are in git
and received traffic is not.

### Three numbers, not one

- **WER** per turn against verified references, baseline (per-transmission pass only) against
  baseline plus this pass. The bar is the project's measured run-to-run noise: a gap under
  ~1 point is not real.
- **Invented content**, counted separately, in the style of `bench_correct_inspect.py`. The
  central risk of this feature is a fluent wrong answer, which WER barely notices.
- **Identification unmoved**, as a regression guard. The pass runs after resolution, so
  `bench_identify` should not move at all. If it does, something is wired wrong.

Bake-off axes: model × few-shot on/off, scored on the held-out corpus.

### Success criteria

1. WER improves on the baseline by more than ~1 point on a corpus that was not used for
   examples.
2. Invented-content count does not rise against the baseline.
3. `bench_identify` precision and recall are unmoved within their measured spread.
4. Every failure path leaves a readable, uncorrected conversation.

## Risks

**Turn-to-reference mapping is the main one, and is checked first.** Scoring conversation text
against references needs each turn mapped back to its reference clip. Stored conversations
carry timestamps; references are keyed by clip id. `bench_identify` already maps labels to
conversations by time, so the mechanism exists, but it has not been verified as reliable enough
for per-turn WER. If it is not, the benchmark is the hard part of this feature rather than the
pass. This is verified before anything else is built, so it is found cheaply.

**A confident wrong propagation.** Rule 1 makes the shore station authoritative, and the shore
station is not always right. The audit trail makes it visible; the invented-content count makes
it measurable; neither prevents it. Accepted, and the reason the feature ships behind a flag.

## Out of scope

- Any change to the SDR# plugin or its protocol.
- Correcting the live per-transmission display retroactively.
- Non-CH01 maritime and airband correction, still deferred until an external antenna exists.
- Replacing the regex correction table, which keeps its measured value and runs unchanged.
