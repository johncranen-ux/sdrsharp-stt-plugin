# Airband prompt biasing — design

**Date:** 2026-09-11
**Status:** MEASURED 2026-09-11 -- see RESULT. Both airline arms DISQUALIFIED (they fabricate callsigns); the QNH arm is a null. Nothing shipped.
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

- [x] QNH over-production falls and KLM under-production closes, both beyond the repeat arm's
      spread. **Both achieved, and both overshoot.**
- [x] The identification four-way split is reported per arm against the published baseline.
- [x] No arm is adopted that adds a wrong match. **This is what disqualified the two arms that
      improved recall.**
- [x] WER over the edited rows does not regress beyond the repeat arm's spread. **Failed by
      every arm:** the noise floor is 0.0 points and every edit cost 6-12.
- [ ] The tail-first arm is re-measured on the winning arm's transcripts. **Not applicable --
      there is no winning arm.** Re-measure it on the control instead when that work resumes.

## RESULT — 2026-09-11

Seven arms, 136 clips each, 952 transcriptions, zero dropped clips. Groq `whisper-large-v3`.

| arm | prompt actually sent | QNH | KLM | ILS | recall | wrong | WER |
|---|---|---|---|---|---|---|---|
| `air_shipped` | aviation (control) | 29 | 5 | 23 | 43.8% | 7 | 27.2% |
| `air_shipped` repeat | identical | 28 | 5 | 23 | 43.8% | 7 | 27.2% |
| `air_no_qnh` | control minus "QNH" | **1** | 5 | 26 | 43.8% | 6 | 33.2% |
| `air_airlines` | control plus operators | 11 | **27** | 16 | 68.8% | 9 | 38.7% |
| `air_both` | both edits | **0** | **31** | 15 | **71.9%** | 10 | 39.3% |
| `air_empty` **INVALID** | the MARITIME prompt | 0 | 6 | 4 | 40.6% | 6 | 52.9% |
| `air_noprompt` | genuinely nothing | 0 | 3 | 6 | 37.5% | 3 | **90.4%** |
| *(operator heard)* | | *12* | *15* | *8* | | | |

**The noise floor is zero.** The repeat arm matched the control on recall, every bucket, and
pooled WER to the decimal. Every delta above is real.

### 1. The prompt is load-bearing, far more than anyone had measured

With no prompt at all the decoder collapses to **90.4% WER** — clip 0000 transcribes as `...`
and nothing else. The shipped prompt is worth ~63 WER points. Nothing in this project had
established that before; the question had never been asked.

### 2. The mechanism is confirmed, causally

Removing one token takes QNH production from 29 to 1. Adding airline phraseology takes KLM
from 5 to 31. The prompt decides what the decoder writes, exactly as the operator suspected
from reading transcripts.

### 3. But removing QNH buys nothing

`air_no_qnh` cuts QNH production by 97% and leaves identification **completely unchanged** —
43.8% recall, 15 extraction misses, the same three selection misses. The decoder does not start
writing KLM in the freed slot; it writes some other garble. **Over-priming was not the problem.
Absence was.** This retires the most intuitive reading of the original evidence.

### 4. The airline arms buy recall by hallucinating, and are disqualified

`air_both` is the largest recall movement this project has produced: 43.8% -> 71.9%, extraction
misses 15 -> 3. It is also unshippable. KLM production overshoots to 31 against the operator's
15, and three of the new identifications are fabricated outright — confirmed against the
operator's own ear transcriptions, not inferred:

| row | operator heard | `air_both` wrote | tagged |
|---|---|---|---|
| 0027 | `??` (unintelligible) | "...JetBlue three two." | JBU32 |
| 0108 | "Level five, ?" | "Level five, JetBlue three two." | JBU32 |
| 0035 | "Continue pressed heading, **kilo hotel bravo**" | "...**KLM one two bravo**" | KLM12B |

Row 0035 is the one to remember: the decoder turned phonetic letters — plausibly a runway or
taxi instruction — into a callsign, and the matcher then attached a real in-range aircraft to
it with full confidence. That is the BERGE TOWNSEND failure class, manufactured by the prompt.
The prompt-echo filter cannot catch any of these, for the reason given earlier in this spec:
they are single substituted words inside otherwise-real speech.

WER confirms it independently: +11.5 and +12.1 points against a 0.0 noise floor.

### 5. A measurement defect, found and corrected mid-run

The `air_empty` arm **did not test what it claimed**. `bench_stt.py` calls
`backends.transcribe(...)` without a `mode`, so mode defaults to `"maritime"`, and
`_effective_prompt` returns `client_prompt or <default>` — an empty string is falsy, so the arm
silently sent the **maritime** prompt ("Maas Approach, Maas Aanloop, this is the inbound
motortanker...") to airband audio. It was caught because its errors were full of "Rotterdam",
"Maas Center buoy" and "over". Re-run as `air_noprompt` with `WHISPER_PROMPT=""` exported, which
`os.environ.get` returns verbatim. The original row is kept above, marked INVALID, rather than
deleted: 52.9% is a real and interesting number — the *wrong* prompt still beats no prompt by 37
points — it just is not the number the arm was designed to produce.

## Conclusion

**Nothing ships.** The static prompt edit fails as specified: the one arm that is safe
(`air_no_qnh`) is a null, and the two arms that work are disqualified for fabricating
identifications.

What the arm did establish, none of it previously known:

- the prompt carries ~63 WER points, so it must not be removed or weakened casually
- prompt content causally determines callsign transcription, confirming the operator's read
- the failure is absence of airline vocabulary, not over-priming of QNH
- naive injection of that vocabulary overshoots into hallucination at the exact point where the
  audio is unintelligible — which is precisely where a fabricated callsign is most dangerous

### Where this leaves arm A

The dynamic per-transmission version now looks **worse**, not better, than when it was deferred.
It injects a much larger and more specific vocabulary than these eight operators, and rows
0027/0035/0108 show what the decoder does with unintelligible audio when a callsign is available
in the prompt: it uses it. Any future attempt must be scored on this corpus against those three
rows as standing negative cases, and must carry a defence the echo filter does not have — one
that works on a single substituted word inside real speech.

A calibrated middle remains untested: these arms crammed eight operators into one dense
sentence, which is maximally priming and unlike real speech. A gentler variant might buy part of
the recall without the fabrication. That is a hypothesis, not a result, and it needs its own arm.

## Deferred

- Dynamic per-transmission callsign injection — the original arm A, pending this result.
- Arm B (session identity register) and arm C (physical corroboration), unchanged from the
  measurement spec: 3 rows and 0 rows respectively.
- Airband-specific `_ECHO_GENERIC_WORDS`. The echo filter's vocabulary is maritime, so it is
  mis-tuned on this channel, but it is not implicated in the QNH failure and no evidence yet
  says it costs anything here.
