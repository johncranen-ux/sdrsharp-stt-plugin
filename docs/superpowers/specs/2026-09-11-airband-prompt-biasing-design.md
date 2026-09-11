# Airband prompt biasing — design

**Date:** 2026-09-11
**Status:** design agreed, not implemented
**Related:** this is arm A from
`2026-09-10-airband-identification-measurement-design.md`, narrowed to its static form after
that arm's baseline named it as the binding constraint. Scored with the harness that spec
built (`server/bench_flight_identify.py`).

## The problem

The airband decoding prompt over-produces its own vocabulary and under-produces the one word
that matters most on this channel. Measured across the 136 hand-labelled transmissions of the
2026-09-10 corpus, comparing what the decoder wrote against what the operator heard:

| word | times in prompt | machine wrote | operator heard | |
|---|---|---|---|---|
| **QNH** | ×1 | 27 | 12 | **over by 15** |
| ILS | ×3 | 17 | 8 | over by 9 |
| wilco | ×1 | 2 | 0 | over by 2 |
| Schiphol | ×1 | 1 | 1 | — |
| Tower | ×2 | 3 | 3 | — |
| **KLM** | **×0** | 5 | 15 | **under by 10** |

`DEFAULT_AVIATION_PROMPT` (`server/stt_proxy/backends.py`) contains **no airline name at all**,
on a channel where KLM is the most frequently spoken word.

One confound, named rather than hidden: the operator marked unintelligible words with `?`,
which mechanically inflates "over-production" for every prompt word. The same effect would
inflate KLM too, and KLM is *under*-produced by 10, so the asymmetry survives it.

### The damage is not diffuse

In four transmissions the decoder wrote QNH in the exact slot where KLM was spoken, and all
four are labelled KLM aircraft that consequently went unidentified:

```
0028  "Descend flight level seven zero, QNH one two bravo."     -> KLM12B
0051  "Park, good morning, QNH six two seven, ..."              -> KLM627
0064  "Aperture, good day, QNH one four zero six."              -> KLM1406
0097  "One eight four five, QNH seven six seven."               -> KLM767
```

QNH and KLM are both three-letter initialisms spoken in the same cadence, and only one of
them is primed.

### Why the existing echo filter does not catch this

`corrections._is_prompt_echo` requires **every** word of a transmission to come from the
prompt, plus either six or more words or a prompt-distinctive word. It is built to catch a
whole transmission that is the prompt read back. A single substituted word inside otherwise
real speech is structurally outside its reach, and its `_ECHO_GENERIC_WORDS` list is maritime
vocabulary besides. Nothing currently defends against this failure.

## Decisions

1. **Static prompt edit only.** Feeding live in-range callsigns into each request (the original
   arm A) is deferred until this is measured. The QNH evidence suggests a string constant may
   capture most of the available gain, and the dynamic version cannot be designed sensibly
   until that number exists.
2. **The primary metric needs no reference text.** Count how often each arm writes QNH and KLM.
   This measures the exact defect with zero dependence on reference quality, which matters
   because the reference set is partly contaminated (below).
3. **WER is a guardrail, not a headline**, and is computed over the **85 edited rows only**. The
   other 51 `heard` lines are byte-identical to the machine text — the operator accepted the
   pre-fill — so WER there is 0 by construction and would dilute any real movement. Restricting
   to edited rows biases toward hard clips, so the figure is directional.
4. **Ship a repeat arm as the noise floor.** Groq at temperature 0 is not bit-deterministic and
   `bench.py` already records ~1 point of pooled-WER movement between byte-identical runs. A
   bare delta of a point or two carries no information without it.
5. **Include an empty-prompt control.** The maritime record has a prompt variant (`v3_phrases`)
   that measured *worse* than what shipped. "Does the prompt help at all" is a real question and
   is nearly free to answer here.
6. **Re-measure the tail-first arm afterwards.** Four of its ten recoveries are exactly the
   QNH→KLM rows above. If the prompt fixes them at source, that arm's value shrinks and its
   precision cost stops being worth paying.

### Alternatives rejected

- **Go straight to dynamic callsign injection.** Strongest version, but it needs ADS-B plumbed
  into the request path and a selection rule for which of ~80 in-range aircraft fit in the
  prompt budget. If a one-line constant captures most of the gain, that work is never justified.
- **Score WER as the primary metric.** Half the reference set is the decoder's own output, so
  the headline would partly measure agreement with itself.
- **Measure on the local whisper.cpp backend.** `config.json` has `STT_BACKEND=groq` with
  `GROQ_MODEL=whisper-large-v3`; that is what runs in production and therefore what to measure.
  Note Groq truncates prompts to a documented word cap (`_truncate_prompt`), so any variant
  must fit inside it.
- **Drop the QNH line and ship it.** It is one edit and it looks obviously right, which is what
  was said about the three maritime matching changes that all measured as nulls.

## Architecture

### 1. The arms

Six runs over the same 136 clips, all through `bench_stt.py`, which calls
`backends.transcribe` directly — the same function the proxy calls — and captures raw text
before any correction pass.

| arm | prompt | question |
|---|---|---|
| `air_shipped` | today's `DEFAULT_AVIATION_PROMPT` | control |
| `air_shipped` (repeat) | identical | run-to-run noise floor |
| `air_no_qnh` | shipped minus the QNH clause | is over-priming the cause? |
| `air_airlines` | shipped plus real airline names | is absence the cause? |
| `air_both` | both edits | do they compose? |
| `air_empty` | `""` | does the prompt help at all? |

`air_airlines` uses only operators actually observed on this channel — KLM, Transavia, Delta,
American, United, JetBlue, Orange, Speedbird, Shamrock — in natural phraseology rather than as
a word list, since the prompt is read as prior speech.

### 2. Metrics

1. **Mechanism (primary, reference-free):** per-arm counts of `QNH` and `KLM`, against the
   operator's 12 and 15.
2. **Identification:** `bench_flight_identify`'s four-way split re-run over each arm's
   transcripts. Directly comparable to the published baseline (recall 43.8%,
   extraction-miss 15, selection-miss 3, and precision 100% once the labelling-convention
   artifact is removed with `--blank skip`; the raw figure prints as 66.7%).
3. **WER (guardrail):** `bench_prompt_ab.py` over the 85 edited rows, which pairs on clip id,
   drops clips that failed in any arm rather than absorbing them, and bootstraps a confidence
   interval on the difference.

### 3. New code

- **Airband entries in `bench.PROMPTS`.** The registry is maritime-only today. `bench_stt.py`
  takes `--prompt` from it and passes the text explicitly, which overrides mode selection, so
  no other change to that script is needed.
- **`worksheet → references.txt`.** The worksheet's `heard` lines become a reference file in
  `bench.load_references`'s `clip_id<TAB>text` format. `?` markers are emitted as
  `[inaudible]`, which `bench._normalize` already strips; the 10 empty `heard` lines emit no
  reference and are excluded from WER by existing behaviour.
- **`bench_flight_identify --transcripts <results.json>`.** Score identification over
  re-transcribed text instead of the worksheet's `machine` lines, joining on clip id.
- **A QNH/KLM production counter**, reading any results JSON.

## Testing

Ordinary modules under `server/tests/`, TDD as usual:

- Reference conversion: a `?`-marked line becomes `[inaudible]`; an empty `heard` emits no
  reference; a line containing a tab or colon does not corrupt the format.
- `--transcripts` join: a clip present in the results replaces the worksheet text; a clip
  missing from the results is reported and excluded, never silently scored on stale text.
- The production counter: word-boundary matching, so "QNH" inside another token does not count.
- Prompt registry: every airband variant fits inside Groq's word cap, asserted rather than
  assumed — an over-long prompt is a hard 400 and would cost a whole arm.

## Rollout

1. Build the four pieces above and their tests.
2. Generate the reference file from the worksheet.
3. Run the six arms. Report drops honestly; a rate-limited arm is re-run, not patched.
4. Publish the three metrics per arm against the control.
5. Only then decide whether to change `DEFAULT_AVIATION_PROMPT`, and re-measure tail-first.

## Success criteria

- [ ] QNH over-production falls and KLM under-production closes, both beyond the repeat arm's
      spread.
- [ ] The identification four-way split is reported per arm against the published baseline.
- [ ] No arm is adopted that adds a wrong match — precision is currently 100% and prompting the
      decoder with airline names is exactly the change that could invent one.
- [ ] WER over the edited rows does not regress beyond the repeat arm's spread.
- [ ] The tail-first arm is re-measured on the winning arm's transcripts, and its verdict is
      restated with the overlap removed.

## Deferred

- Dynamic per-transmission callsign injection — the original arm A, pending this result.
- Arm B (session identity register) and arm C (physical corroboration), unchanged from the
  measurement spec: 3 rows and 0 rows respectively.
- Airband-specific `_ECHO_GENERIC_WORDS`. The echo filter's vocabulary is maritime, so it is
  mis-tuned on this channel, but it is not implicated in the QNH failure and no evidence yet
  says it costs anything here.
