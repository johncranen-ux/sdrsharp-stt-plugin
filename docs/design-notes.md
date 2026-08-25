# Design notes and measurements

Why this project is built the way it is, and what the numbers behind those choices were.
Everything here was measured on real received traffic; each section says how.

This is the engineering record, not documentation. For installing and running the project
see [the user manual](user-manual.md); for a short overview see the
[README](../README.md).

---

## STT backends

| `STT_BACKEND` | What it uses | Notes |
|---|---|---|
| `groq` *(default)* | Groq hosted `whisper-large-v3` over HTTPS | No GPU involved. Same model weights as the local backend, but Groq's OpenAI-compatible endpoint exposes no decoder tuning. |
| `whisper_cpp` | Local `whisper-server` on port 8080 in WSL2 Ubuntu-22.04, ROCm-accelerated | Full decoder control (beam search, VAD, prompt carry). Subject to the GPU driver hang described under "Known limitations"; the watchdog arms automatically for this backend only. |

**Rollback:** set `STT_BACKEND=whisper_cpp` in `server/start-all.bat` and restart the proxy.
That is the entire procedure — both code paths are maintained and covered by tests.

Groq requires `GROQ_API_KEY`. Its free tier allows 20 req/min, 2,000 req/day, and 7,200
audio-seconds/hour; measured busy-channel traffic is ~105 requests and ~507 audio-seconds
per hour, so requests/day is the only limit with any realistic chance of binding.

The daily cap is a continuously refilling token bucket, not a midnight reset:
`x-ratelimit-reset-requests` comes back as `43.2s`, exactly 86400/2000. Exhausting it
therefore throttles you to ~1 request per 43 s rather than blocking outright — degraded
service, not a blackout, but still most of a busy channel's traffic dropped. To get warning
before that, the proxy watches the `x-ratelimit-remaining-requests` header and logs
`[quota] Groq daily requests remaining: N` once the balance falls below
`GROQ_QUOTA_WARN_AT` (default 200), repeating every `GROQ_QUOTA_WARN_STEP` (default 50)
after that. Requests/minute is deliberately *not* throttled: the plugin's send loop is
serial and its queue is bounded drop-oldest, so pacing sends would convert a visible 429 on
the newest chunk into a silent discard of the oldest one — usually the transmission that
names the vessel. A 429 with a short `Retry-After` is waited out and retried once; a long
one is surfaced immediately rather than stalling every chunk queued behind it.

**Trade-off:** Groq accepts only `model`, `language`, `prompt`, `temperature` and
`response_format`. The `beam_size=5` / `best_of=5` / `carry_initial_prompt` /
`suppress_nst` tuning documented below applies to the local backend only and has no
equivalent on Groq.

### Groq vs whisper.cpp, measured 2026-07-30

Same 61-clip / 49-reference set, same prompt, same script
(`--matrix groq_prompt --port 9000 --path /v1/audio/transcriptions`):

| Run | Pooled WER |
|---|---|
| whisper.cpp `beam5_prompt`, raw (`:8080`, no corrections) | 0.416 |
| **Groq `whisper-large-v3` (`:9000`, corrections applied)** | **0.411** |
| whisper.cpp `beam5_prompt` (`:9000`, corrections applied) | 0.359 |

**Losing beam search costs essentially nothing.** Groq at 0.411 matches whisper.cpp's
0.416 raw baseline, so whatever decoding Groq runs server-side is worth about as much as
`beam_size=5`/`best_of=5` was.

The remaining ~5-point gap to 0.359 is **not** a model-quality difference — it is the
correction pass being backend-specific. `_apply_sttt_corrections` targets whisper.cpp's
error patterns (`mass`, `mars`, `march`), but Groq misspells "Maas" differently: 27
instances across 13 spellings (`Aas`, `AAS`, `Aps`, `A.M.A.S.S.`, `MAAAS`, `Ameas`,
`Master`, `Moth`, `MOTR`, …), none of which the existing rules match. Applying
Groq-shaped rules to the same outputs recovers most of it — ~0.369 — though that figure is
in-sample (rules derived from the set they are scored on) and so optimistic, the same
caveat that applies to the original correction work.

Latency is comparable or slightly better: median 2.83 s end-to-end including network,
p95 6.20 s, against a 3.55 s mean local decode. That is where the LPU shows up — in speed,
not accuracy; the weights and therefore the achievable quality are the same.

### Correcting Groq's errors: fuzzy, not regex (2026-07-30)

Closing that gap with more hand-written rules **does not work**, and the split-half
experiment is worth keeping. Deriving `Maas` rules from 25 clips and scoring them on a
held-out 24:

| | Derivation (in-sample) | Hold-out (out-of-sample) |
|---|---|---|
| Raw Groq, no corrections | 0.401 | 0.431 |
| + existing regex corrections | 0.372 | 0.412 |
| + new hand-written Groq rules | 0.356 (−0.016) | 0.409 (**−0.003**) |
| + fuzzy `<x> Approach` → `Maas Approach` | 0.365 (−0.007) | 0.375 (**−0.037**) |

Hand-written rules were worth **0.3 points held out against 1.6 in-sample** — almost
entirely overfitting. The cause is the shape of the error distribution: whisper.cpp got
"Maas" wrong *consistently* (`mass`/`mars`/`march`), which regexes capture well, while
Groq gets it wrong *diversely* — 13 spellings over 27 instances — so a rule learned from
one sample rarely fires on the next. Similarity matching generalises to spellings never
seen during derivation and is worth **3.7 points held out**.

`_correct_maas_before_approach` therefore replaces any token preceding an "approach"-like
word whose `rapidfuzz.ratio` to "maas" is ≥ `MAAS_FUZZ_THRESHOLD`. That threshold was 70
when this was written, on the reasoning that the ambiguous real words (`Marsh`, `last`,
`are`) fell below it and were "correctly left alone" — which a larger corpus showed was
wrong for `Marsh` and `last`, both of which are genuinely "Maas Approach" in the references.
It is now 50; see "The fuzzy Maas rule was firing on well under half the cases" below.

Corrections are now **mode-scoped**: `_apply_sttt_corrections(text, mode=...)` applies
shared rules (`Callsign`) on any band but restricts the maritime set, including the fuzzy
Maas rule, to maritime traffic. Previously `draft`→`draught` and `mass`→`Maas` fired on
airband too; on the aviation band that would rewrite "final approach" as "Maas Approach".

**Caveat on all Groq WER figures:** Groq is *not* deterministic at `temperature=0` — 10 of
61 clips returned different text across two runs of the same audio. Differences under
~2 points between separate runs are noise. The hold-out figures above are paired
comparisons on identical text, so they are unaffected.

## Current configuration (chosen on real data, 2026-07-27)

Decoder rows below are `STT_BACKEND=whisper_cpp` settings. The prompt and the nautical-term
correction pass apply to both backends; the model, beam-search, VAD and token-suppression
rows have no equivalent on Groq.

| Setting | Value | Why |
|---|---|---|
| Model | `ggml-large-v3.bin` | Benchmarked against `large-v3-turbo` on 49 real, hand-transcribed Rotterdam VHF clips: 38.9% pooled WER vs 40.8%. Costs ~33% more decode time (mean 3.55s vs 2.66s) and 1.5GB more VRAM — trivial on a 24GB card, and both models decode well under real-time (aggregate RTF 0.57x vs 0.43x), so no throughput risk. |
| Beam search | `beam_size=5`, `best_of=5` | ~1 point better than greedy once the prompt is fixed. |
| Maritime prompt | Fluent example transmissions (see `DEFAULT_MARITIME_PROMPT` in `stt_proxy/backends.py`) | The single largest lever found: ~9-10 points of WER improvement over no prompt. A keyword-list-style prompt was tried first and rejected — it primes Whisper to echo the list back verbatim on noisy/silent audio. **That ~9-10 point figure was measured against a different prompt than the one shipped** — see "The prompt was never the one being measured" below. |
| Server-side Silero VAD | **Off** (`WHISPER_VAD=false`) | Measured no WER benefit over VAD-off at the same decoder settings (48.5%/41.8% vs 40.8% pooled), and whisper.cpp's VAD+beam combination has its own flakiness (intermittent HTTP 500s, and one full server wedge observed). The plugin's own client-side VAD already does this job. |
| Suppress non-speech tokens | On | Reduces hallucinated fillers. |
| Nautical-term corrections | Regex pass (`_apply_sttt_corrections` in `whisper-proxy.py`), applied to every non-CH01 maritime/airband response | Evidence-backed rules (Mass/Mars/March Approach → Maas Approach, call sign → Callsign, motor tanker → Motortanker, draft → draught, boys/boy → buoys/buoy) derived from substitution-frequency analysis of the baseline benchmark. Measured on the same 61-clip/49-reference set, `beam5_prompt`: pooled WER 41.6% direct against whisper.cpp (`:8080`, no corrections) → 35.9% through the proxy (`:9000`, corrections applied), a ~5.7-point improvement — but most of that predates this pass: ~4.2 points come from the 7 correction rules that already existed (Mass/March Approach, bare "mass", cosine, call sign, motor tanker, draft, boys), and only ~1.3 points from the 3 rules added in this pass (Mars Approach, bare "mars", boy). 2026-07-28. |

> **Note on WER comparability:** the 38.9% figure above (Model row) is a raw-decoder
> measurement from an older whisper.cpp build, taken 2026-07-27. The 41.6%/35.9% figures
> (Nautical-term-corrections row) are raw-decoder-vs-proxy-path measurements from the
> current build — which added `--no-flash-attn` as a GPU-hang mitigation — taken
> 2026-07-28. The 38.9%→41.6% gap is a build/flag difference, not a regression; each pair
> (38.9 vs 40.8, and 41.6 vs 35.9) is comparable only within itself, not across rows.

All of the above are env-overridable in `whisper-proxy.py` (`WHISPER_BEAM_SIZE`,
`WHISPER_VAD`, `WHISPER_PROMPT`, etc.) without touching code.

Full per-clip results: `server/bench-report.html` (turbo, full config matrix) and
`server/bench-report-large-v3.html` (large-v3, winning config).

### The prompt was never the one being measured (2026-08-06)

> **Follow-up, 2026-08-07: nor was the deployed one.** The section below fixed the *bench*
> copy. The *plugin* held a third copy, and it beat them both. `PluginSettings.Prompt`
> defaulted to a prompt naming an invented vessel, "Motortanker Neptune", and
> `WhisperClient` sent it as the `prompt` form field on every request — while
> `_effective_prompt` resolves to `client_prompt or DEFAULT_MARITIME_PROMPT`, so any
> non-empty client prompt shadows the server's entirely. The v2 prompt measured on
> 2026-08-06 therefore never ran in production, and the phantom name — which matches a real
> AIS entry at score 100 — kept being echoed into transcripts and resolved to a real MMSI.
> `PluginSettings.Prompt` now defaults to `""`, so the proxy owns the prompt as the
> proxy-owned-params design in `backends.py` always intended; the textbox remains as a
> deliberate per-site override. Pinned by `PluginSettingsTests`. **Note that the
> deployed `SDRSharp.SttPlugin.xml` persists the old value and must be cleared too — the
> DLL default only applies where no settings file exists yet.**

`bench.py` defined its own `MARITIME_PROMPT` (24 words) while the proxy sent
`DEFAULT_MARITIME_PROMPT` (40 words). Every prompt figure above, and the Groq/whisper.cpp
comparison, was therefore measured against text production has never sent. `bench.py` now
imports the shipped constant — one prompt, no copy to drift — and `tests/test_bench.py` pins
them together so it cannot come back. `bench_stt.py --prompt {shipped,legacy}` selects
between them, and results files record which was used.

Measured over 244 hand-referenced clips from `captures/2026-07-28`, `STT_BACKEND=groq`,
via `bench_prompt_ab.py` (paired on clip id; 5,000 bootstrap resamples), after the nine
contaminated references described below were corrected by ear:

| Arm | Pooled WER | Δ vs shipped | 95% CI on Δ |
|---|---|---|---|
| **shipped** (40 words, in production) | **29.0%** | — | — |
| legacy (24 words, what the numbers above describe) | 31.1% | +2.1% | [−0.5%, +4.8%] |
| shipped, re-run | 29.0% | +0.0% | [+0.0%, +0.0%] |

**The shipped prompt is the better of the two, but not by a margin this clip set can
resolve.** The pooled-WER interval spans zero; the sign test is what carries the result —
legacy is worse on 73 clips and better on 50, two-sided *p* = 0.047. The two disagree
because a handful of long clips dominate pooled WER. Read together: direction established,
magnitude not. The practical consequence is only that the recorded figures above slightly
*understate* what production does, so nothing shipped needs changing.

**Scored through the deployed path (`--echo-filter`), the gap is wider.** bench.py measures
the raw decoder — right for decoder settings, wrong for a prompt comparison, because the
prompt-echo filter is downstream of the prompt and keyed to it. Four shipped-arm clips (0068,
0134, 0188, 0225) came back as verbatim prompt fragments that `_is_prompt_echo` suppresses in
production, so the raw score charges the shipped prompt 28 edits for text no user ever sees,
against legacy's 18:

| Arm | Pooled WER (deployed path) | Δ | 95% CI | Sign test |
|---|---|---|---|---|
| shipped | **28.4%** | — | — | — |
| legacy | 31.0% | +2.6% | [−0.04%, +5.18%] | 74 worse / 47 better, *p* = 0.018 |

The interval still grazes zero, so the honest reading is unchanged — but both the effect and
its significance improve once the measurement matches what is actually deployed. A prompt
that echoes more is otherwise penalised twice: once in the text it emits, and again in the
WER of text production already discards.

**Groq's decoder is effectively deterministic at `temperature=0`**, which the third arm
exists to establish: 242 of 243 clips came back byte-identical across two runs an hour
apart. The noise floor is therefore ~0.2 points, so the 2.6-point gap is not sampling
scatter — the uncertainty in it is which clips are in the set, not what the API returns.
This also retires a standing worry: unlike the Claude calls (`identify.py`, where two
identical runs scored 38.8% and 39.7%), STT runs need no repetition to be trusted.

Two things fell out of the per-clip diffs, both arguing against the *content* of the
shipped prompt even though its overall score is better:

* **`callsign PABC` appears to prime letter-by-letter spelling.** On two short clips the
  shipped arm returned `M-A-S-A-P-P-R-O-A-A-L-L-O-S` and `M-A-S-P-O-A-R-T-E-R-A` where the
  legacy arm returned "Aas Approach, Excel." and "Maas approach, over." Only the shipped
  prompt contains a spelled callsign, only the shipped arm produced this failure (2 clips vs
  0), and it emitted `PABC` on 4 clips against legacy's 0. Two clips is not proof, but the
  mechanism is plausible and the failure is severe where it lands.
* **Nine references were themselves wrong, several of them prompt echoes.**
  `make_references.py` pre-fills references from the plugin's own prompted output for
  hand-correction, so an echo the labeller missed becomes "ground truth" — and rewards
  whichever arm hallucinates it. All nine have since been re-transcribed by ear and
  corrected; the table above is the post-correction measurement.

### A prompt cannot absorb the phrases it is meant to fix (2026-08-17)

The user reported residual errors on standard phraseology — pilot ladder, pilot boarding,
"stand by zero one, one six", starboard side, "two metres above the waterline" — and asked
whether anything was left worth doing. Measurement first said: not much. Of 516 error
operations over the 235 verified English clips of 2026-08-14, **vessel names are 49.6% of all
errors but only 8.5% of the corpus, while formulaic phrases are 9.9% of errors and 1.7% of the
corpus.** Perfect phrase handling was therefore worth at most ~1.7 points of pooled WER.

Some of those errors were nonetheless *caused by the shipped prompt*, which writes `portside`
as one word and pairs "Maas Approach" with "Maas Aanloop" in a single breath. Measured
consequences: `side`→`portside` ×2, `port`→`portside` ×1, `side`→**`starportside`** ×1 (a blend
of the spoken "starboard side" with the prompt's spelling), and `approach`→`aanloop` ×6.

`v3_phrases` (in `bench.py`) fixed all of that, with every addition derived from n-gram counts
over the verified references rather than invented — `"Maas Approach, Maas Approach"` is the most
common trigram in real traffic (30×), `"pilot boarding time"` 10×, `"stand by zero one"` 9×.

**It still lost, and was rejected.**

| Arm | Pooled WER | Macro | Exact | Δ | 95% CI on Δ | Sign test |
|---|---|---|---|---|---|---|
| shipped | **17.14%** | 25.0% | 30.2% | — | — | — |
| `v3_phrases` | 18.66% | 29.1% | 26.8% | +1.53 | [+0.04%, +3.00%] | 34 better / 51 worse / 150 tied, p=0.082 |

Every targeted substitution improved (`approach`→`aanloop` 6→1, all three `portside` variants
→0; errors in clips saying "pilot boarding" 55→42, "starboard/port side" 32→20, "above the
water" 22→14). The regression came from somewhere else entirely:

> **A phrase in the initial prompt can silence a clip whose entire content is that phrase.**

Five clips went newly empty — 0045 `"Understood, proceed, standby two one."` (the shipped
prompt transcribed it perfectly), 0105, 0163, 0164, 0201 — contributing **+25 of the +46 net
error words**. All five are 5–7 reference words against a corpus median of 10, and all five
closely match sentences added to the prompt. This is what made "errors in clips saying channel
numbers" go 38→57: not hallucinated numbers, silence.

On a channel where much of the traffic is five-word acknowledgements, that caps how much
phraseology a prompt can usefully absorb — and it is the opposite of the failure mode the
prompt-echo filter was built for.

The trade runs both ways, which is why the prompt is kept rather than deleted: `v3_phrases`
also **fixed three of the four empty outputs** the shipped prompt produced (0196, 0238, 0271),
two of them call-ups carrying a vessel name. Net empties 4 → 6.

Indicated but never run: a v4 keeping only `starboard side` and the doubled call-up, dropping
the "stand by zero one, one six" sentence that collides with acknowledgement traffic. Expected
value is low — roughly 11 error words of 516, about 0.3 points.

**Groq determinism, re-measured:** two runs of the identical prompt gave byte-identical text on
278 of 280 clips (99.3%), consistent with the 242/243 figure above and much better than the
10-of-61 in the Groq caveat. But **the aggregate WER matching to three decimals was luck**: both
differing clips happened to be unscored ones carrying no reference. Do not read an identical
aggregate as proof of determinism. One of the two differences was itself a verbatim prompt echo
(`"…ETA at the Maas Center buoy one four four five, over."`), the other the classic
`"U.S. Department of Defense"` hallucination.

### Finding contaminated ground truth (2026-08-06)

Three screens found all nine, and only the last needed a human:

1. **Prompt-distinctive tokens.** Flag references containing a prompt word outside
   `_ECHO_GENERIC_WORDS`. Cheap, but noisy — "callsign" and "proceed" are ordinary radio
   vocabulary, so 16 of 24 hits were false alarms.
2. **Speech rate.** Reference word count over clip duration. The corpus median is 1.95
   words/sec; clip 0148 implied 6.3 and 0253 implied 5.4. **Nobody says 13 words in 2.1
   seconds** — that is contamination proven by arithmetic, no listening required.
3. **An unprompted re-decode.** A reference drafted from prompted output cannot be checked
   against more prompted output. Re-decoding with `prompt=""` gives an independent witness
   that has no way to know the prompt's vessel name or callsign exists. Where it diverges
   sharply, the reference is the prompt talking.

What the audio actually said, versus what the contaminated references claimed:

| Clip | Reference claimed | Actually said |
|---|---|---|
| 0148 | "Rotterdam VTS, be advised we are standing by on channel one six, over." | "Check, Standby zero one" |
| 0251 | "Motortanker Neptune, Maas Approach, roger." | "Multratug 18 malala?" |
| 0253 | "Motortanker Neptune, be advised we are standing by…" | "Multratug 18 in service?" |
| 0170 | "…Motortanker Neptune, coming over." | "…Motorvessel la? Veronica B, come in over" |
| 0212 | "Neptune is by the end of the way" | "next report on the way" |
| 0177 | "(PABC1330) PABC one three three zero" | "Our, best ETA one three three zero" |
| 0112 | "Callsign PABC" | "Callsign Papa Bravo Oscar Uniform" |

**Every invented vessel name was the prompt's own.** `Motortanker Neptune` appeared in four
references and was in the audio of none of them.

**Contamination distorts comparisons more than it distorts absolute scores.** Correcting nine
of 244 references moved the headline WER by 0.2 points (29.2% → 29.0%) but moved the
shipped-vs-legacy delta by 0.5 (+2.6% → +2.1%) — because the contamination was not random,
it favoured the arm that hallucinates the prompt. Any future prompt comparison must sweep the
reference set first, and *especially* one testing a prompt that removes the invented names,
since these references would then score against the better prompt.

**Two clips were Dutch** (0251, 0253 — both the tug *Multratug 18*), which the pipeline cannot
transcribe because `language` is pinned to `en` (`backends.py:85`, `:132`). Forced-English
decoding of Dutch produced German-flavoured invention: *"Mötter, Röck, Achtung, Maranatha"*.
That text then flows into vessel identification and AIS matching, which is exactly how a
phantom vessel with a real MMSI gets manufactured.

### Why the language stays pinned to English (2026-08-06)

The obvious response to those two clips — stop forcing `en`, let Whisper detect — was tried
and **rejected on measurement**. `sweep_language.py --language ""` on a 4-clip smoke test
labelled two plainly English transmissions *"Modern Greek"* and transcribed them into Greek
script:

| Clip | Reference | Unprompted, auto-detected |
|---|---|---|
| 0003 | "Maas Approach, Maas Approach, this is MSC Athens, Callsign five Lima Kilo Victor Five." | Μας προσέξετε, μας προσέξετε, αυτό είναι το ΜΣΥ Αθένα, κόλτ σάιν 5… |

Language ID on a few seconds of noisy VHF is not trustworthy, and the failure is not
symmetric: forcing `en` costs the occasional genuinely-Dutch transmission, while unforcing it
risks mangling English — which is the overwhelming majority of this traffic — into whatever
the detector guesses. **The pin stays.** Handling Dutch properly needs something better than
per-chunk auto-detection (longer context, or a detector run over a whole conversation rather
than one 5-second chunk); it is not a one-line change.

This also killed the idea of a language census: with a false-positive rate that high on
English clips, an auto-detect sweep cannot say how much Dutch is really in the set. The
honest current answer is that two clips are known Dutch, by ear, and the true figure is
unmeasured. (When a language *is* forced, the API echoes it back, so `sweep_language.py`
suppresses the census in that mode rather than reporting a meaningless "100% English".)

### The sweep found no further contamination (2026-08-06)

`sweep_language.py` over all 244 referenced clips — unprompted, English forced, so the prompt
is the only variable removed. **The nine corrected references were the whole of it**, as far
as this screen can see.

Fifteen references still contain a prompt-distinctive word, and all fifteen are ordinary
radio vocabulary: `callsign` (13), `proceed` (2), `permission` (1). Each is corroborated by
the unprompted decode independently producing the same word — 0060's reference reads
"Serenade, what is your Callsign?" and the unprompted decode hears "Serenade, what is your
call sign?". **A decoder that was never told the prompt exists cannot echo it**, so agreement
is proof the word was really spoken.

The highest-divergence references are hard audio, not contamination — 0205 ("Maas Approach,
roger." vs *"Patricia is there, I'm asking you."*), 0137 ("Mind Polar" vs *"mine's fuller"*).
None of the top 20 by divergence contains a prompt-distinctive word.

Two vocabulary findings worth carrying into any future prompt: the station is addressed as
**"Maas Aanloop"** (Dutch) as well as "Maas Approach" — reference 0086 uses it and the
unprompted decode hears it too — and **Multraship/Multratug** towage is recurring traffic
(0038, 0251, 0253).

### Identification, measured against verified labels (2026-08-06)

The first honest identification numbers. 59 hand-verified conversations
(`identification-labels-verified.txt`, the prefix of the labels file checked by ear), 267
transmissions:

| | Verified labels | Self-scored whole file |
|---|---|---|
| Precision | **68.0%** | 98.5% |
| Recall | **51.7%** | 97.7% |

The self-scored figures are meaningless and should never be quoted: field 3 of a drafted line
is pre-filled with the resolver's own verdict, so an unverified file scores the resolver
against itself. Against ground truth it names the wrong vessel on 65 of 203 transmissions it
names at all, and stays silent on 64 it should have identified.

Over-segmentation is confirmed as a real effect: 1.24 exchanges per conversation, with 11 of
59 conversations split across more than one.

### Multi-word vessel names are unfindable (2026-08-06)

`_hint_probes` generates single words and **adjacent pairs only**, so a three-word name is
never probed whole. Combined with the whole-string `fuzz.ratio` cutoff of 85 — which was the
right fix for `WRatio`'s substring inflation — this systematically loses long names to short
ones. For *SANTA ISABEL MAERSK* (which is in the cache):

| Probe | Against | Score | |
|---|---|---|---|
| `SANTA ISABEL` | SANTA ISABEL MAERSK | 77 | below cutoff |
| `ISABEL MAERSK` | SANTA ISABEL MAERSK | 81 | below cutoff |
| `SANTA ISABEL MAERSK` | — | — | **never generated** |
| `ISABEL` | ISABEL (a different vessel) | **100** | returned |

So the correct vessel cannot be matched at any probe length available, while an unrelated
one matches perfectly. Reproduced live: the hints for that transmission are `ROTTERDAM`,
`MAAS`, `ISABEL` — three wrong vessels and not the right one.

The fix needs both halves: generate 3-word (probably 4-word) probes, *and* prefer the
longest matching probe rather than the first, or the spurious short match still wins on
probe order. Not yet implemented — and see below for why it is worth much less than it looks.

### Why identification actually fails (2026-08-06)

Diagnosed per **conversation**, not per transmission: identity belongs to the exchange, so a
mid-conversation turn that names nobody ("Okay, thank you, next call when underway") is
*supposed* to yield no hints. Over the 59 verified conversations:

| | n | |
|---|---|---|
| Identified correctly | 31 | 53% |
| **Expected vessel not reachable from any turn** | **24** | **41%** |
| Reachable, but the resolver did not pick it | 4 | 7% |

**86% of failures are upstream of the resolver.** The Claude call is very nearly never the
problem — it picks correctly whenever the right vessel is in front of it. Effort spent on the
resolver prompt would have been effort wasted.

**But longer probes recover almost none of it.** Simulated over the same 24, everything else
held constant:

| Probe variant | Conversations recovered |
|---|---|
| n-grams ≤ 2, cutoff 85 *(current)* | 0 |
| n-grams ≤ 3, cutoff 85 | 3 |
| n-grams ≤ 4, cutoff 85 | 3 |
| n-grams ≤ 4, cutoff 80 | 5 |
| n-grams ≤ 4, cutoff 75 | 7 |

Only **3 of 24** — SANTA ISABEL MAERSK and MSC MARIA PIA. The word-count correlation above
(every multi-word failure being a hint failure) invited the conclusion that probe length was
the cause; it is not. For most long names the *transcription itself* is too corrupted for a
whole-name probe to reach 85 either.

**The real dominant cause is STT mangling the vessel name past orthographic reach:**

| Heard | Actually |
|---|---|
| "Oasun", "O'Razon" | ORASUND |
| "Haltizeus" | THESEUS |
| "Vista Heisberger" | BIRTHE ESSBERGER |
| "telepathy" | TULIPA SEAWAYS |
| "Yeki Borg" | JEKERBORG |
| "Mid-Huff", "Huff" | BITHAV |

These are *phonetically* close and orthographically far, which is precisely what whole-string
`fuzz.ratio` cannot see. A phonetic matcher (Double Metaphone, or a phoneme-level distance)
is the tool that fits the failure, not a longer n-gram.

One case is unfixable by matching at all: ELENORE was predicted as **ELENORA**, a different
real vessel one character away, three times. Separating those needs position — which is the
AIS staleness limitation already recorded under Known limitations.

Dropping the cutoff to 75 recovers 7 rather than 3, but that is the knob whose tightening
cut spurious probe→vessel pairs from 2,334 to 101. Any cutoff change has to be measured for
false positives *and* recall, never recall alone.

### Phonetic matching does not pay (2026-08-06)

The failures above are phonetic, so a phonetic matcher looked like the obvious tool. Sized
with `jellyfish.metaphone` before writing anything, scoring recovery **and** the precision
proxy together — distinct probe→vessel pairs over all 718 stored transcripts:

| Variant | Recovered (of 24) | Probe→vessel pairs |
|---|---|---|
| ortho ≥ 85, n ≤ 2 *(current)* | 0 | 216 |
| **ortho ≥ 85, n ≤ 4** | **3** | **233** |
| phonetic = 100, n ≤ 4 | 1 | 505 |
| phonetic ≥ 90, n ≤ 4 | 3 | 610 |
| either ≥ 90, n ≤ 4 | 5 | 624 |
| either ≥ 85, n ≤ 4 | 6 | 1200 |

**Phonetic matching costs 2.3×–5.6× more spurious pairs to recover at most 6 of 24.** That
is not a wash, it is actively harmful: `_find_ais_hints` caps the hint list at 5, so flooding
it pushes the correct vessel *out* of the list the resolver ever sees. Metaphone also cannot
separate the case that motivated it — ELEANOR keys to `ELNR` against both ELENORE and
ELENORA, exactly as `fuzz.ratio` does at 86.

**Longer probes alone are the only change here that pays**: +3 recovered for +17 pairs (+8%).
Checked for regression on the top-5 hint list across all identifiable conversations: 29 keep
the correct vessel, 2 newly gain it, **0 are crowded out**. (Two rather than three, because
one recovered vessel does not make the top 5.)

`jellyfish` was installed for this experiment and removed again; it earned no permanent
place. What is left for the remaining ~21 unreachable conversations is not a better string
matcher — it is position filtering, which is the AIS staleness item under Known limitations.

### Static AIS messages were erasing positions (2026-08-06)

The two ingest branches in `_process_ais` were asymmetric. `PositionReport` merged into the
existing entry; `ShipStaticData` **assigned a fresh dict**, and that dict has no
`latitude`/`longitude` keys at all. So any vessel that reported its position and then
broadcast static data lost the position — and static messages repeat roughly every 6 minutes,
so this fired continuously for every vessel sitting in the box.

That is very likely the bulk of the **25% of vessels in the labelled conversations that had
no position at all**, which is what made the distance data unusable. Now merges. Prediction
worth checking after the proxy has run for a day: that 25% should fall substantially. It will
not repair the existing cache — a position already overwritten is gone until that vessel is
seen again.

### `last_seen`, and why a position needs one (2026-08-06)

Every cache write now stamps `last_seen`. It is **rolling, not an entry time**: AIS transmits
position every 2–10 seconds underway, so a vessel in the box has it rewritten constantly, and
it freezes only once the vessel leaves, stops transmitting, or the proxy stops running.

Without it a cached position cannot be interpreted at all. The cache is reloaded from disk at
startup and entries never expire, so "48 km from Maas Center" might be from forty seconds ago
or from three weeks ago, and nothing in the data distinguishes them. Concretely: on
2026-08-06 the on-disk cache had last been written 2026-08-04 23:59, making every position in
it at least 46 hours old, with no way to tell which were far older.

It cannot be backfilled — entries written before this have no timestamp and never will, so a
missing `last_seen` means *unknown age*, not *recent*.

**Why this is the prerequisite for distance filtering.** Measured over the 59 verified
conversations, a hard distance gate is unusable: only 46% of correct vessels are within 50 km
and 25% have no position, so a 50 km gate would reject over half the right answers. The
bounding box (`ROTTERDAM_BBOX`, roughly 205 × 215 km) is simply large, and vessels calling
Maas Approach are routinely 40–100 km out — being far away is normal, not suspicious. And in
the ELENORE/ELENORA case, the *correct* vessel has no position while the wrong one sits at
48 km, so "prefer the nearer" would have actively confirmed the error.

Distance is therefore worth having as a recency-weighted prior, not a gate — and its real
value is buying headroom to loosen the fuzzy cutoff (which recovered 7 of 24 unreachable
conversations but inflated spurious pairs 5.6×) without flooding the 5-slot hint list.

**On evicting vessels that leave the box:** aisstream only sends messages for ships inside
the box, so a departure is never announced — it is indistinguishable from a vessel going
quiet, or from the proxy being down. Eviction can only be TTL-based, which needs this same
field. Weighting is preferable to deleting: evicting the vessel that is about to call loses
the identification entirely, while keeping a stale one costs one extra candidate among
thousands. A generous TTL (30 days) is still worth having as housekeeping, since the cache
currently grows without bound.

### What the deployed prompt actually cost (2026-08-07)

The plugin had been shadowing the server's prompt with `v1_names` (see the follow-up note
above). Measured for the first time on a **clean** corpus — 99 hand-verified clips from
`captures/2026-08-07`, references checked by ear the same day, `STT_BACKEND=groq`, paired on
clip id with 5,000 bootstrap resamples:

| arm | pooled WER | Δ vs shipped | 95% CI on Δ |
|---|---|---|---|
| **shipped** (v2, 93 words — never actually ran in production) | **25.1%** | — | — |
| `v1_names` (40 words — what production really used) | 36.4% | **+11.3%** | **[+7.8%, +15.2%]** |

51 of 99 clips are worse under `v1_names`, 11 better, 37 unchanged. With `--echo-filter` the
gap holds at +10.0% [+6.4%, +13.8%], so it is not an artefact of a single echoed clip.

**Three mechanisms are visible in the movers**, and they explain the size of the gap:

- **Spoken digits collapse to numerals.** `v1_names` returns "1330" where the transmission
  says, and the reference records, "one three three zero" (clips 0060, 0041). Radio procedure
  spells digits out; the v2 prompt's spelled-out numbers carry that into the decoder.
- **Key terms degrade**: "Maaas Approach", "Maaas Centervoe" on 0067.
- **Verbatim prompt echo.** On 0038, against audio saying "Tug, Panda, Motorvessel", `v1_names`
  returned *"Rotterdam VTS, be advised we are standing by on channel one six, over."* — a
  sentence lifted straight out of its own prompt. Caught by the echo filter, but it is the
  failure mode that motivated removing invented names in the first place.

**The methodological point is the bigger one.** The 2026-08-06 measurement put the shipped
prompt only 2.1 points ahead with a CI spanning zero, and the v2 work at 3.7 points. Both were
measured on a corpus **66% draft pre-fill from whisper.cpp output**. The clean corpus puts the
same comparison at 11.3 points with a CI nowhere near zero. Draft references do not merely add
noise — they **systematically understate any change that makes output diverge from whisper.cpp**,
because the draft *is* whisper.cpp. Prompt figures measured on part-draft ground truth should
be treated as lower bounds, not estimates.

One honest caveat: the echo filter suppressed **3** clips in the shipped arm (0058, 0073, 0086)
against **1** in `v1_names`. The longer v2 prompt gives more text to echo. The filter catches
them, but that is worth watching.

**Dutch clips, and why the annotation must use square brackets.** Eight of the 99 clips are
Dutch (0053–0057, 0085–0088) and were annotated by hand. `_normalize` strips `[...]` only —
`_BRACKET_RE` is `\[[^\]]*\]` — so a `(dutch)` marker survives as the literal token `dutch`,
a word no arm can produce, costing one guaranteed edit per marked clip in *every* arm. The
markers were converted to `[dutch]`. Use square brackets for any hand annotation.

The conclusion survives all three treatments, which is the point of recording them:

| treatment | shipped | `v1_names` | Δ | 95% CI |
|---|---|---|---|---|
| `(dutch)` counted as a word (the bug) | 25.1% | 36.4% | +11.3% | [+7.8%, +15.2%] |
| `[dutch]` stripped, clips still scored | 24.8% | 36.1% | +11.2% | [+7.7%, +15.1%] |
| Dutch clips excluded entirely | 22.2% | 33.9% | +11.6% | [+7.7%, +15.9%] |

The delta is stable at ~11 points throughout; only the absolute WERs move. Note that the eight
Dutch clips (8% of the corpus) carry **2.6 points of absolute WER** in both arms — expected,
since the language is pinned to `en` (see "Why the language stays pinned to English"). The
Dutch-inclusive figure is the production-realistic one, since Dutch transmissions genuinely
occur on this channel; the excluded figure is the English-only decoder performance.

### The AIS feed fails by going quiet (2026-08-07)

The feed delivered nothing for a whole session while reporting itself healthy. `[AIS]
connected` printed at 08:59:54, the TCP connection to aisstream stayed established for the
next 31 minutes, and in that time the cache did not change by one byte: 8,642 vessels,
identical md5, **0% carrying `last_seen`**. No error, no close, no exception — so the
reconnect handler in `_ais_loop` never fired, because from its point of view nothing had gone
wrong. Every lookup meanwhile matched happily against a cache loaded from disk.

**The cause is external, and was pinned down by elimination rather than assumed.** With the
proxy stopped, so exactly one connection held the key:

| test | frames in 30s |
|---|---|
| original key, sole connection | 0 |
| freshly issued key | 0 |
| Rotterdam bbox, no message-type filter | 0 |
| **whole-world bbox, no filters** | **0** |

A world bounding box returning nothing eliminates the subscription shape, the bounding box,
the filter, the key, and concurrency throttling. The subscription matches the documented
format field for field. This is [aisstream/aisstream#15](https://github.com/aisstream/aisstream/issues/15),
open with no resolution. Nothing in this repo can fix it.

**What is fixed here is the silence.** Two blind spots let a dead feed pass for a live one:

- `_process_ais` returned without logging on any frame lacking an MMSI — which is exactly
  the shape of aisstream's `{"error": "..."}` frames. The most diagnostic thing the server
  can say was being discarded. Now logged, rate-limited to
  `_UNKNOWN_FRAME_LOG_LIMIT` so a persistent fault cannot flood the console.
- Nothing watched the clock. `_watch_silence` now runs alongside the read loop and reports a
  connected feed that has gone quiet for `AIS_SILENCE_WARN_SEC` (0 disables; **the default
  became 0 on 2026-08-11** — see "The silence watchdog is muted, not removed" below).
  It distinguishes *went quiet mid-stream* from *never sent anything*, because those point at
  different causes. It watches from a separate task rather than wrapping `recv()` in a
  timeout: cancelling a `recv()` mid-frame is a way to lose messages, and all that is needed
  is a periodic look at when the last frame arrived.

The decision logic is `_silence_report`, kept pure so it can be tested without a websocket or
a clock.

**Confirmed independently, and dated.** The community uptime monitor at
`https://aisuptime.buttermilkgreen.fyi/api/v1/status` (unofficial — aisstream publishes no
status page of its own) reported, at the time of writing:

```json
{"state": "Silent Failure", "websocketConnected": true,
 "lastMessageReceived": "2026-08-05T13:31:30.210Z"}
```

`websocketConnected: true` alongside `Silent Failure` is precisely the shape diagnosed here,
observed by a third party against their own keys. All 48 samples in its rolling 24-hour
window are `Service Outage`, with no healthy sample. Other users report the same on the
issue tracker, one of them having already run the same elimination —
"[Zero messages on global bounding box since 2026-08-05 13:31 UTC — valid key, new key, and
second IP all affected](https://github.com/aisstream/issues/issues/257)".

So the outage began **2026-08-05 13:31 UTC** and has run continuously since. Our cache last
gained content at 2026-08-04 23:59 only because that is when the proxy last ran; it was never
up during the healthy window on 08-05.

Beware of `aistreams.statuspage.io` (plural), which shows all-green: its components are
"Proxy's and Api / Management Portal / Streams" and it belongs to a different service. It is
not evidence about aisstream.io.

### Longer probes, shipped and measured (2026-08-06)

`_hint_probes` now emits contiguous spans of 1–4 words (`AIS_HINT_MAX_NGRAM`, default 4)
instead of single words and adjacent pairs. `AIS_HINT_FILTER=off` still restores the original
behaviour exactly, via a separate `_legacy_hint_probes` that must not inherit improvements —
otherwise the revert is not a revert.

Scored with `--resolve` over the 59 verified conversations. **The default mode cannot measure
this**: it scores the *stored* verdicts, which a matcher change cannot retroactively alter,
and dutifully reports identical numbers before and after.

| Run | Precision | Recall | correct / wrong / missed |
|---|---|---|---|
| `AIS_HINT_MAX_NGRAM=2` (previous) | 74.8% | 60.6% | 163 / 55 / 51 |
| **`=4` (shipped)** | **77.4%** | **63.6%** | 171 / 50 / 48 |
| `=4`, repeat run | 77.3% | 63.2% | 170 / 50 / 49 |

**+2.6 precision, +3.0 recall**, against a noise floor of ~0.4 points established by the
repeat run. Eight more transmissions correct, five fewer wrong, three fewer missed — the
change helps both axes rather than trading one for the other.

Note the re-resolved baseline (74.8/60.6) is well above the stored verdicts (68.0/51.7).
That gap is *not* attributable to anything measured here: `--resolve` replays the stored
transcript text through today's code, so it reflects resolver and matcher work already in the
tree since those verdicts were written, and excludes any STT change.

### Prompt v2: the vocabulary is the lever, not the names (2026-08-06)

Two candidates against the shipped prompt, run as separate arms so the causes stay separable
(`bench.py` `NO_NAMES_PROMPT`, `VOCAB_PROMPT`). 244 clips, paired, `--echo-filter` figures in
the "deployed" column:

| Arm | Raw | Deployed | Δ deployed | 95% CI | Sign test |
|---|---|---|---|---|---|
| shipped | 29.0% | 28.4% | — | — | — |
| `no_names` (names removed, nothing else) | 27.8% | 27.6% | −0.8% | [−3.1%, +1.4%] | 37 / 36, *p* = 1.00 |
| **`vocab`** (names removed + observed vocabulary) | **24.5%** | **24.7%** | **−3.7%** | **[−6.7%, −1.0%]** | 97 / 55, ***p* = 0.0008** |

**Removing the invented names does nothing for WER.** `no_names` is a dead heat. Had both
changes been bundled into one arm the whole gain would have been credited to the wrong cause.
The case for dropping `Motortanker Neptune` and `callsign PABC` is **identification safety,
not accuracy** — a different axis, and worth keeping straight.

That safety case is visible in what the echo filter suppresses. In the shipped arm, clip 0068
(reference "Inaudible") returned `MOTORTANKER NEPTUNE, roger, over.` and 0188 ("Go ahead,
Sam.") returned `MOTORTANKER NEPTUNE` — the prompt inventing a vessel that matches AIS at 100
on audio that says nothing of the sort. Both disappear once the name leaves the prompt.

**Caveat on the corpus, and the check it prompted.** Only clips 0000–0099 of
`references-2026-07-28.txt` are hand-verified; the rest is still draft pre-fill from
whisper.cpp output, and the file's own header warns that "any backend comparison built on
them will flatter it". **66% of that corpus by word count is draft.** Re-scored on the
verified 89 clips alone:

| Arm | Pooled WER | Δ | 95% CI |
|---|---|---|---|
| shipped (v1) | 32.0% | — | — |
| `no_names` | 30.5% | −1.5% | [−6.2%, +2.2%] |
| **`vocab`** | **25.5%** | **−6.5%** | **[−11.7%, −1.9%]** |

The win is *larger* on clean ground truth, and still clears zero — as the direction of the
bias predicts, since drafts derived from v1-prompt whisper.cpp output resemble the v1 arm
more than they resemble `vocab`. The mixed-corpus figure of −3.7 was conservative, and
`no_names` remains a wash on either corpus.

**Held out on the 2026-07-27 set** (49 references, not used to derive the vocabulary):

| Arm | Raw | Deployed | Δ deployed | 95% CI | Sign test |
|---|---|---|---|---|---|
| shipped | 41.6% | 40.0% | — | — | — |
| `vocab` | 35.6% | 35.6% | −4.4% | [−9.9%, +1.7%] | 19 / 14, *p* = 0.49 |

**The effect did not shrink out of sample** — the point estimate is if anything larger than
in-sample (−4.4 vs −3.7 deployed). That is the test that matters here, because overfitting
predicts collapse toward zero, which is exactly what happened to the hand-written correction
rules (1.6 points in-sample, 0.3 held out). At *n* = 49 this set cannot certify significance
on its own; the significance comes from the 244-clip run, and this run's job was to check the
effect replicates on data it was not built from. It does. One caveat on the hold-out's
purity: "Deepwater route" appears in the 07-27 references and was seen while building the
prompt; the rest of the vocabulary traces to the 07-28 set.

**Known cost.** A richer prompt widens `_is_prompt_echo`'s distinctive-word set (8 tokens for
the shipped prompt, 29 for `vocab`), making false suppression more likely. Clip 0226 —
`"ETA, roger, one one six."` — looks like exactly that, and 0028 is lost by both new arms
because dropping "Neptune" leaves `motortanker` as a bare prompt word in a 6-word all-prompt
transmission. Suppression counts did not rise overall (3 for `vocab` against 4 for shipped
in-sample; 0 against 1 held out), so this is a cost to watch rather than a blocker.

## Vessel identification on CH01 (2026-07-30)

A single exchange between Maas Approach and *Wilson Durness* was reported showing three
different vessels — `[NEPTUNE]`, `[GOOD WAY]`, `[Mettank]` — each with a real MMSI attached.
Investigation found three independent causes, all now addressed, each behind its own switch:

| Env var | Default | Turns off |
|---|---|---|
| `AIS_HINT_FILTER` | `on` | Hint tightening (restores the original matching exactly) |
| `PROMPT_ECHO_FILTER` | `on` | Prompt-echo suppression |
| `CONVERSATION_RESOLVER` | `on` | Retrospective conversation resolution + `/conversations` |

**1. AIS hints were manufacturing vessels.** `_find_ais_hints` probed every word and
word-pair against 7,313 AIS names with `WRatio` at `score_cutoff=65`. `WRatio` falls back to
*partial* matching when lengths differ, so a short ordinary word scores ~90 against any long
name containing it. Measured over 307 real transcripts it produced **2,334** distinct
spurious probe→vessel pairs:

```
'GOOD DAY' -> 'GOOD WAY'   88     'THE'  -> 'SYNTHESE 11'   90
'AND'      -> 'ALEXANDER-M' 90    'THIS' -> 'AMETHIST'      90
```

Those were handed to Claude with a rule telling it to *use them to correct vessel names* —
so the pipeline suggested the phantoms rather than Claude inventing them. Now uses
`fuzz.ratio` (whole-string, no substring reward) at `AIS_HINT_MIN_SCORE` (85), 4-character
minimum tokens, and a stopword guard that skips probes made entirely of ordinary speech,
numbers or NATO phonetics. **2,334 → 101 pairs (23×), with all 15 known real vessel names
still probed.** Raising the old cutoff alone would not have worked — many spurious matches
score exactly 90.

**2. The decoding prompt was being transcribed back.** `DEFAULT_MARITIME_PROMPT` contains
*"Motortanker Neptune, Maas Approach, roger…"*, and the reported `[NEPTUNE]` chunk is a
literal substring of it — with "Neptune" scoring 100 against a real AIS vessel. Similarity
alone cannot detect this (real traffic genuinely says "Maas Approach"; the 95th percentile of
`partial_ratio` against the prompt across real transcripts is 91). `_is_prompt_echo` instead
requires that *every* word came from the prompt **and** either ≥6 words or a word distinctive
to the prompt. Flags **9 of 307** transcripts, all verifiably verbatim prompt fragments,
while leaving real short transmissions such as *"This is Maas Approach."* alone.

**3. Each chunk was identified in isolation** — so a garbled opening call was identified from
the worst evidence available and never revisited, even when the shore station repeated the
name clearly two turns later.

Feeding prior turns into the *same* Claude call that produces the transcription was tried and
**removed**: over 249 real chunks it nearly doubled fabrication (18 → 32 chunks returning
words nobody said — `"Copy that, thank you."` came back as `"Gungor Star one three one five,
correct."`) and could propagate a wrong identity across a whole exchange. Context in the
transcription call bleeds into the transcription; two rounds of prompt tightening reduced but
never stopped it.

Identity is now resolved **after** an exchange ends, by a separate pass
(`resolve_conversation`) whose **output schema has no text field at all**. It cannot rewrite a
transcription — the failure mode is impossible by construction rather than discouraged by
instruction. It also makes late evidence retroactive: a callsign spelled out in turn 4
resolves turns 1–3 through the exact `match_by_callsign` lookup.

A background reaper closes a window when its channel has been quiet for `CONVERSATION_GAP_S`
(60 s) or it reaches `CONVERSATION_MAX_CHUNKS` (40). A window is a **container, not a
conversation** — measured on the 07-28 session, a 120 s gap gives a median window of 11 chunks
over 116 s and a longest of 45 chunks over 10 minutes, because CH01 is shared and Maas works
many vessels back-to-back. So the resolver segments the window into exchanges *by content*,
which no gap rule can do, and picks each vessel **from an AIS candidate list or returns null**
— never naming one freely.

Results appear at **`localhost:9000/conversations`**, not in the plugin: the proxy answers per
request, so holding a response until an exchange ends would stall the plugin's serial send
loop, and at exchange end no request is in flight to answer. The live transcript is unchanged.

**Judging it:** `server/replay_sessions.py` replays a capture directory in original order and
timing with the filters on and off and prints the diff. It deliberately reports **no accuracy
score** — there is no ground truth for vessel identity — only what changed, for review:

```
py server/replay_sessions.py --captures "D:\SDR\...\captures\2026-07-28" --compare
```

## Vessel identification, second pass (2026-07-31)

One report — a transmission naming *Motortanker Orason* displayed as `[RA]` — turned up four
independent faults on the same path. Each is behind its own switch where it changes matching.

| Env var | Default | Turns off |
|---|---|---|
| `AIS_NAME_FILTER` | `on` | Name-match tightening (restores `WRatio` at cutoff 80 exactly) |
| `AIS_PARTIAL_CALLSIGN` | `on` | Partial-callsign corroboration |

**1. `WRatio` again, one layer down.** The 07-30 work moved `_find_ais_hints` off `WRatio`
but left `match_by_name` on it, where it failed identically: `WRatio` falls back to
`partial_ratio * 0.9` when lengths differ by 1.5×–8×, so a two-letter cache name scores 90
against any longer name containing it. `RA` is a substring of o-**RA**-son and beat `ORASUND`
— the ship actually being called, cached the whole time — which scored 76.9. The same path
reached `RA` from `MARATHON`, `GRACE` and `RADAR`; the live cache holds around a hundred names
of three characters or fewer at any time, each a substring landmine.

Now `fuzz.ratio` at cutoff 76, with names of ≤3 characters accepted only on equality. Measured
end-to-end over the live cache by corrupting real names the way STT does:

| | one edit (n=3000) | two edits (n=2893) |
|---|---|---|
| `WRatio` 80 (before) | 84.6% right, 14.8% wrong | 63.0% right, 30.1% wrong |
| `ratio` 76 + guard | **91.9% right, 6.7% wrong** | **80.9% right, 11.1% wrong** |

76 is the floor, not a preference: 80→76 gains 163 correct for 13 wrong on the two-edit
corpus, and the next step to 75 costs 42 wrong for 23 right while the one-edit corpus turns
bad at the same point.

**2. An unknown phonetic word splits the run.** `Oscar Whiskey Gulf Juliet two` decoded to
`['OW', 'J2']` rather than `['OWGJ2']`, because `gulf` was not in the table — and an unreadable
word does not merely fail, it breaks the run and loses the letters either side. MONA SWAN
(MMSI 219624000, cs OWGJ2) went unidentified with its callsign spelled out twice. `X-ray` was
broken identically by its hyphen. Only these two spellings were added: every addition widens
the guard, and no fuzzy threshold separates `gulf`/`golf` from `the`/`three`, which score the
same (75) against the corpus.

**3. The resolver had never run.** Splitting `whisper-proxy.py` into `stt_proxy/` left five
names used but never imported. `re` sits on the fenced-reply branch of `resolve_conversation`,
and Haiku fences its JSON every time, so *every* conversation raised `NameError`, was
swallowed by the broad `except Exception`, and surfaced as the innocuous-looking "resolver
unavailable". `rf_fuzz` and the `/api/ais-cache` globals sat on branches no test reached.
`datetime` in `identify.py` is an import-time error on Python ≤3.13 but invisible on 3.14,
where PEP 649 defers annotation evaluation — so CI could not collect the suite at all while
everything looked green locally.

Two lessons, both now enforced: `resolve_conversation` itself had no test (only the helpers
either side of it), and CI has no linter. Both fixed — the resolver is covered across bare,
fenced and prose-wrapped replies, and CI fails on any `pyflakes` **undefined name**. This was
the third time the split shipped a missing name; the route tests were themselves added after
it broke `/conversations`.

**4. Partial-callsign corroboration.** MSC TEMA VIII spelled `5LRK9` as *five Lima Romeo Kilo
nine*; Whisper heard *five DEMA Romeo, clear nine*. `match_by_callsign` is an exact dictionary
lookup, so two wrong characters of five meant no match and the vessel went unidentified —
`_resolver_candidates` returned an empty list, leaving the resolver no answer but null.

The surviving characters still carry information: anchored on the word "callsign", they decode
to the pattern `5.R.9`, which fits exactly one of the 7,000-odd cached callsigns. Anchoring is
what makes it safe — scanning the whole transmission picks up the `eight` in "MSC DEMA eight"
and yields `8.5.R.9`, which is wrong.

Uniqueness alone is *not* sufficient. Measured by garbling real callsigns at 20% per spoken
character (n=2000): unique-match-only gives 916 right / 1 wrong, but fires on an unrelated
ship **8.0%** of the time when the true vessel is not in the callsign table at all — roughly
500 cached vessels carry none. That is a confident false identity, the failure this pipeline
weighs most heavily. Requiring the vessel's *name* to corroborate independently
(`fuzz.ratio ≥ 60` against the `_hint_probes` of the window) takes wrong matches to **0** and
the uncached case to **0.0%**. The threshold is 60 because the reported transmission scores
66.7 (`MSC DEMA` vs `MSC TEMA VIII`) and 75 would have rejected it.

It runs last of the three candidate passes, never displaces a stronger match, and is marked in
the prompt so the resolver ranks it below an exact callsign and above name resemblance. It
adds a *candidate*; Claude still adjudicates.

Design and measurement method: `docs/superpowers/specs/2026-07-31-partial-callsign-corroboration-design.md`.

### The resolver ignored what the live pass already knew (2026-07-31)

*Santa Isabel Maas* was resolved as **ISABEL** — a 90 m Dutch coaster — while the live pass
had already matched **SANTA ISABEL MAERSK** correctly. The resolver, whose purpose is to
*correct* unreliable live guesses, replaced a right answer with a wrong one.

`_hint_probes` generates only unigrams and bigrams, so a three-word name cannot be probed
whole:

| probe | vs `ISABEL` | vs `SANTA ISABEL MAERSK` |
|---|---|---|
| `ISABEL` | **100.0** | 48.0 |
| `SANTA ISABEL` | 66.7 | 77.4 (cutoff is 85) |

The real ship never entered the candidate list; `ISABEL` matched one substring word exactly.
Prompt rule 2 forbids choosing a vessel that is not on the list, so Claude picked the only
vessel-shaped option and marked it high confidence. It was right to — the list was wrong.

`_resolver_candidates` built its list from callsigns and hint probes and **never read
`live_mmsi`**, which the journal stores on every chunk. The live pass runs `match_by_name`
over the whole cache with the complete extracted name — strictly more information than a
bigram probe. It now seeds the candidate list, ahead of hints and behind exact callsigns.

Measured over 24 stored conversations that had a live match, that vessel was absent from the
candidate list in **9**: 7 resolved to nobody, 2 to a different ship. (Counted after
discarding live values of ≤3 characters, which are artifacts of the `WRatio` bug fixed the
same day.) It adds a candidate, never a verdict — a live guess is often wrong on a garbled
opening call, which is the whole reason this pass exists, so it is marked as a lead.
Switch: `RESOLVER_LIVE_CANDIDATES`.

**Trigram probes were measured and not adopted.** Over 366 real transmissions they add 20
distinct probe→vessel pairs (131 → 151, +15%) and make only 4 additional three-word names
reachable. They do fix this case (`SANTA ISABEL MAAS` → `SANTA ISABEL MAERSK` scores 88.9)
and produce some genuine corrections (`JOHN P ESBERGER` → `JOHN T. ESSBERGER`,
`LADY MARY FISHER` → `LADY MARIA FISHER`), but also clear errors — `COSTCO SHIPPING GEMINI`
→ `COSCO SHIPPING SEINE` is a different ship. Seeding from the live match fixes the same case
with no new fuzzy surface, so it was preferred. Trigrams remain a reasonable second step if
three-word names keep being missed, but would need the widening measured properly first.

### Both pages link out (2026-07-31)

`/conversations` and `/identified-vessels` render an identified vessel as a link to
VesselFinder, keyed on **MMSI** rather than name — names are neither unique nor reliably
heard, and the MMSI is what the AIS match actually established. A vessel with no MMSI renders
as plain text rather than a link that would go nowhere.

Escaping and the link live in `stt_proxy/markup.py`, shared by both pages because
`vessel_log.py` is presentation-only and must not import the resolver to reach a helper. That
module previously interpolated every field into HTML unescaped, which matters more than it
looks: AIS static data is broadcast in the clear, so a vessel name is attacker-controllable by
anyone with a transmitter in the Rotterdam box.

## Identification is now measurable (2026-08-04)

Transcription has had `bench.py` and a pooled WER figure since the beginning, and every
change to it was argued with numbers — split-half validated, in-sample figures marked as
such. Identification had none of that. The AIS matcher, the hint filter, the resolver and
its prompt were all changed on the strength of one-off scripts written to chase whatever had
just gone wrong, then thrown away.

Two bugs found by hand on the same day made the case. **PECHORA STAR** spelled its callsign
out cleanly and resolved to nobody, because a fuzzy name match at 76.9 pre-empted an exact
callsign lookup. **THULELAND** held one 5-turn conversation that came back as three
exchanges naming three different ships. Both were diagnosed with throwaway code, and neither
would have shown up in any number the project tracked.

`server/bench_identify.py` scores identification the way `bench.py` scores transcription.

**Scored per transmission, not per stored exchange**, because over-segmentation is one of the
failures being measured. A per-exchange score calls the THULELAND case "one right, two
wrong"; per transmission it is 1 of 5, plus a conversation split three ways — which is what
actually happened. Run against the two known failures:

```
  transmissions scored   9
    correct              1
    wrong                4
    missed               4   (identifiable, named nobody)
  precision              20.0%
  recall                 11.1%
  exchanges/conversation 2.00   (1 conversation(s) split across more than one)
```

Three distinctions the metric keeps apart, because they have different consequences on
screen: a **wrong** name is a confident false identity, a **miss** is an honest "unidentified",
and **correctly naming nobody** is a success that a naive accuracy figure would punish. A
label of `-` asserts that naming *anyone* is wrong, which is what stops the benchmark
rewarding bold guessing — the specific failure mode the hint filter and the callsign guard
were both built against.

**`--resolve` is the mode that makes a change measurable.** It re-runs the resolver over the
same conversations and scores that, so a prompt edit can be A/B'd instead of argued about.
Conversations are handed back whole rather than as their stored exchanges — otherwise a
rerun inherits the very segmentation it is being measured on. It costs API calls, so it is
opt-in; the default mode reads the store and is free.

Labels are bootstrapped from the resolver's own verdicts (`--make-labels`), the same way
`make_references.py` drafts transcripts from the plugin's output: correcting a draft while
listening beats typing from scratch. The header says to check every line, because a draft
accepted unread scores the resolver against itself and reports 100%. `identification-labels*.txt`
is gitignored — the note field carries transcript text and falls under the same restriction
as `references*.txt`.

## The live pages were cacheable (2026-08-04)

`/conversations` self-refreshes with `<meta http-equiv="refresh" content="30">`, and it looked
like the refresh was not firing: the page sat on 156 exchanges while the server, queried at
the same moment, answered **157** — with the newer exchange already rendered. The refresh was
firing on schedule and the browser was handing it a cached copy, which from the outside is
indistinguishable from a page that never reloads.

None of the live routes sent a single cache directive — no `Cache-Control`, no `Expires`, no
`ETag`, no `Last-Modified`:

```
HTTP/1.0 200 OK
Content-Type: text/html; charset=utf-8
Content-Length: 169695
```

A response carrying no freshness information at all may be cached heuristically, and a meta
refresh is an ordinary navigation, so it consults the HTTP cache like any other. All four
live routes (`/conversations`, `/api/conversations`, `/identified-vessels`, `/api/ais-cache`)
now send `Cache-Control: no-store, must-revalidate` plus `Pragma: no-cache`, the last because
this server still speaks HTTP/1.0. Asserted by route tests rather than by inspection, since
the failure is invisible from inside the process — the server was serving correct, fresh
bytes the entire time.

**Worth noting how this presented**, because two unrelated things looked identical to it. The
same "page is not updating" symptom was also produced by the proxy still running pre-restart
code, and by a busy channel keeping a conversation window open (below). Only comparing what
the server returned against what the browser displayed separated them.

## Vessel particulars on /conversations, and three AIS fields that never parsed (2026-08-04)

`/conversations` is where identity is actually settled, but it showed only the name, MMSI,
callsign and type — while `/identified-vessels` had been rendering dimensions, IMO and a
position all along. The AIS match already carries all of it; it simply never reached the
page that matters most.

**Snapshotted at resolve time, not looked up at render time.** Position, speed and course are
live values. Drawing an hours-old exchange against the ship's *current* position would place
it somewhere it was not when it called, which is worse than showing nothing. `_validate_exchanges`
therefore copies the particulars into the stored row when the exchange resolves, seconds after
it ends. The static fields come along rather than being fetched separately. Rows written
before this — the 104 already on disk — simply omit the line rather than rendering dashes.

**The feature immediately exposed a bug it would otherwise have inherited.** Three fields were
read under the wrong key: `IMO`, `SOG` and `COG`, where aisstream.io sends `ImoNumber`, `Sog`
and `Cog`. Every field name in that feed is PascalCase, and the evidence was unambiguous once
looked at:

| key as read | populated |
|---|---|
| `CallSign`, `Type`, `Latitude`, `TrueHeading` | 83–94% of 8,434 cached vessels |
| `IMO`, `SOG`, `COG` | **0** |

They parsed to `None` on every message ever received, so `/identified-vessels` rendered a dash
for IMO, speed and course from the day it was written and nothing ever failed. `_process_ais`
had **no tests at all**, which is why: the feed is external, the values are optional, and a
missing field is indistinguishable from a ship that did not broadcast one. The message shapes
are now pinned by tests written against the documented schema, since capitalisation is the
whole risk here.

Note that the cached entries only fill in as each ship re-broadcasts its static data, so IMO,
speed and course appear gradually rather than at once.

**Draught and destination are pulled in too**, because they are what CH01 actually asks about
— *"what is your maximum draught"* and where the ship is bound open most exchanges, so having
the broadcast answer next to the transcript is worth more here than the dimensions are.
`MaximumStaticDraught` is metres as a double; `Destination` is free text padded out to its
fixed width with `@`, the null character in AIS's 6-bit alphabet, so everything from the first
one is padding — including the stray trailing character in aisstream's own example,
`"COASTGUARD@@@@@@@@H"`. Destination is also the most attacker-controllable field on the feed,
being free text set by whoever is transmitting, so it is escaped like everything else here.

A full line now reads:

```
IMO 9421663 · 129 × 21 m · draught 8.4 m · → ROTTERDAM · 8.2 kn · 43° · 51.9801, 4.0727
```

## The fuzzy Maas rule was firing on well under half the cases (2026-08-04)

Running the substitution-frequency sweep again — this time over all 636 benchmarked
transmissions carrying a reference, and against **corrected** output so only what is still
broken shows up — put the biggest remaining cluster inside a rule that already existed.
`_correct_maas_before_approach` was missing most of its own target, for two independent
reasons.

**A recognised approach-word is a precondition**, so a spelling the pattern missed took the
Maas correction down with it. `ap+r?oa?ch` cannot match `Aapproach` — the leading double
'a' defeats `ap+` — so *"Aas Aapproach"* was left completely alone even though `Aas` scores
85.7. Seven clips carried `Aapproach` and one `Proach`; none could ever be corrected.

**The threshold recognised only half the variants the references show.** Measured against
"maas": `aps` 57.1, `master` 60.0, `marsh` 66.7, `mots`/`must`/`last`/`mous` 50.0 — all
verifiably "Maas Approach" in the references, all left alone at 70.

What licenses a threshold this loose is positional. Across every reference file the token
before an "approach" **noun** is `maas` **210 times out of 212**, and both exceptions are
comma-separated, which the pattern already refuses to cross. The only other form,
`approaching`, is always ordinary English (*"we are approaching"*, *"I'm approaching"*) — so
the rule is now noun-only, which also fixes a quiet bug: it replaced the whole word including
its suffix, turning *"mass approaching"* into *"Maas Approach"*.

| | pooled WER |
|---|---|
| before | 36.69% |
| widened spelling + threshold 50, noun only | **35.45%** (−1.24) |

54 rows corrected across 27 clips, none damaged. Split-half **−1.04 / −1.51** rather than
collapsing — a similarity rule generalises where a list of spellings does not, which is the
same result the original 2026-07-30 experiment found.

**Going fully positional was measured and rejected.** Replacing *whatever* precedes the noun,
ignoring similarity, scores 35.34% — 0.11 better. Clip 0037 is *"Starfighter, Maas Approach"*
with the comma lost in decoding, and a positional rule rewrites that to *"Maas Approach"*,
deleting the ship. Feeding the identification path a transmission with the vessel name
removed is not worth a tenth of a point. At 50, `Starfighter` scores 13.3 and survives.

Two smaller rules from the same sweep, both clean but on 2 clips each (against 4 for the
ladder rule): fuzzy `Maas` before `Center` — *"Maaf Center, Rekkenbooi"*, read out about as
often as the approach call — worth ~0.08, and `Angkor` → `anchor` worth ~0.10. All three
together: **36.69% → 35.23%**.

Things the sweep surfaced that are deliberately *not* correction rules: vessel-name errors
(`holman`→`kirkeholmen`, `miltrasser`→`multraship`, `mst`→`msc`), which are the AIS matcher's
job, and digit-vs-word differences (`0`→`zero`), which are a benchmark normalisation artifact
rather than a transcription error — the CH01 prompt deliberately preserves the spoken form.

## "ladder" → "letter" / "leather" (2026-08-04)

*Ports are leather two meters above the waterline* — the pilot boarding arrangement is read
out in almost every CH01 exchange, and "ladder" was the single most-mangled word in it after
the place names.

Measured over every benchmarked transmission carrying a reference (636 rows, 293 clips): the
decoder produced `ladder` 38 times, `letter` 14 and `leather` once, while the ground truth
held `ladder` 15 times and `letter` **exactly once** — and that one turned out to be a typo
in the reference (clip 0143, *"pilot  letter port side"*, corrected with this change). In
this traffic the words are never anything but a mis-heard "ladder", which is what makes an
unguarded substitution safe; the existing `boy` → `buoy` rule is the same bet on the same
grounds. Pooled WER **36.84% → 36.67%**, correcting 14 transmissions and damaging none.

The `letter of ...` guard is precautionary rather than measured — no such phrase occurs in
the corpus, but a letter of protest and a letter of credit are real ship's business and
excluding them costs nothing on the cases that do occur. The rule is maritime-only: aircraft
have no pilot ladders and "letter" is ordinary speech on the airband.

**Why the CH01 Claude pass did not already fix it.** Its prompt lists "pilot ladder" in the
maritime vocabulary, but its own rule (a) — *make the smallest edit that fixes a clear error;
if a word is merely unusual, leave it exactly as it is* — holds it back, because "leather" is
a perfectly ordinary English word in a sentence that parses. A deterministic rule runs after
that pass, costs nothing, and does not depend on model behaviour.

**`Ports are` → `Port side` was considered and rejected.** The phrase does not occur once in
the corpus, so there is no evidence to derive a rule from, and `port side` / `portside` both
appear in the ground truth as legitimate forms. Guessing at it is exactly what the
substitution-frequency method exists to avoid.

## A spelled-out callsign outranks name similarity (2026-08-04)

*Motortanker Ikora Star, callsign nine Hotel Alpha two seven eight eight* resolved to
**nobody**, with the vessel — **PECHORA STAR**, callsign `9HA2788` — in the AIS cache the
whole time and its callsign spelled out perfectly. Two defects in `enrich_with_ais`, and the
second is why the retrospective pass could not rescue it.

**Name similarity was tried first.** `match_by_name("Ikora Star")` fell through to the
word-window fallback, probed `IKORA` alone, and reached `VIKTORIA` at **76.9** — one point
over the cutoff. Because it returned *something*, `match_by_callsign("9HA2788")` — an exact
dictionary hit on a callsign already verified as spoken — never ran. The weakest evidence in
the system outranked the strongest.

**The spoken callsign was then overwritten.** Enriching a name match copied the matched
vessel's callsign over the extracted one, so `9HA2788` became `DB6442` (VIKTORIA's).
`_record_chunk` journals this result, so the conversation store held a callsign nobody said.
`_resolver_candidates` then correctly refused `DB6442` — nothing in the transmission reads
that way — and PECHORA STAR was never offered as a candidate at all. The resolver returned
null and its evidence line, *"'Ikora Star' does not match any candidate name"*, was literally
true. The guard was doing its job on data corrupted upstream.

A verified callsign now wins the lookup, and AIS only supplies a callsign when none was
spoken. `raw_text` is what makes the promotion safe — a callsign is preferred only when it
can still be read out of the transmission — so an invented one cannot be laundered into an
identity. Without text there is nothing to verify against and the old order stands.

**The resolver now decodes callsigns from the transmissions, not the journal.** Depending on
`chunk["callsign"]` meant a live pass that recorded the wrong callsign, or none, took the
exact lookup down with it — MONA SWAN (`OWGJ2`) was lost the second way, with the shore
station *asking* for the callsign and the vessel spelling it out. The text is the primary
source and is stored verbatim, so `_spoken_callsign_candidates` reads it directly and the
retrospective pass stops depending on the live guess it exists to second-guess.

Whole runs only, never substrings. Measured over the 435 stored transmissions, whole-run
matching finds all seven real callsigns with nothing spurious — three of them in
transmissions that never say the word "callsign" (*"this is Cosco Hope, nine Victor eight
seven eight six"*), which is why this is not anchored on the keyword the way
`_partial_callsign_pattern` is. Substring search adds no real vessel and opens a hole: 239 of
the 380 runs are times, draughts, channels and positions, and the cache holds all-digit
transponder junk (`2503`, `2603`, `303`) that a long spoken number would eventually hit.
All-digit runs are skipped for the same reason; four characters is the floor because the only
shorter cache entries are junk (`AAA`, `@L<`).

Of the six spelled-out, cache-resolvable callsigns in the 300-conversation store, three were
being lost. Two — PECHORA STAR and ECO ROYALTY (`V7LA9`, lost to **ELKA** on a turn whose
neighbours resolved correctly) — failed on the inverted ordering; the third was MONA SWAN.
Re-running candidate assembly over all 104 stored windows: **0 candidates lost, 6 added**,
across five vessels previously invisible to the resolver.

## LLM transcript correction (2026-08-03)

`CLAUDE.md` proposes a local LLM to clean up poor transcriptions. The local GPU is failing
hardware, so the question became whether a hosted model — free if possible — can do the job,
and how it compares to the regex list already running.

Measured with `server/bench_correct.py`, which re-scores hypotheses already captured by
`bench.py` rather than re-transcribing: no GPU, no SDR#, no audio. Two independent corpora,
the 49 hand-checked clips of 2026-07-27 and 89 newly hand-checked clips (0000–0099) of
2026-07-28. Absolute WER is not comparable between them — different audio, the second set is
easier — so only within-set rankings mean anything.

**Free models are a supply problem before they are a quality problem.** OpenRouter's roster
churns (14 `:free` models the day this was run; trackers listed 15–27 the month before), and
`google/gemma-4-31b-it:free` — the model chosen on paper — answered **3 of 49 attempts in 15
minutes** before being abandoned, hard rate-limited upstream by Google AI Studio. The two
Nemotron endpoints ignored `reasoning: {"exclude": true}` on a minority of clips and returned
raw chain-of-thought, sometimes collapsing into thousands of `<unk>` tokens; one such reply
against a ten-word reference contributes more insertions than the whole corpus has words, and
pooled WER read 877%. Hence `is_malformed()` and a well-formed subset in the report: a model
that answered 45 of 49 clips has no honest pooled WER next to one that answered all 49.

**The prompt outweighed the model.** Every model regressed on `"zero one, one six"` →
`"channel one six"`: Rotterdam works channel 01 and calls on 16, and generic maritime
knowledge overrode local reality. That single error was a third of all regressions. Adding one
rule about it, and delimiting the input so a two-word transmission is not mistaken for an empty
request, moved every model more than the choice between models did:

| | before | after | regressions |
|---|---|---|---|
| gemma-4-26b:free | 39.1% | 34.7% | 10 → 3 |
| haiku-4.5 | 38.5% | 35.6% | 6 → 3 |
| gpt-oss-120b | 38.3% | 37.4% | 11 → 5 |

**Chaining the regex list after an LLM is corpus-dependent, and worth keeping.** On 07-27 it
added ≤0.2 points and looked useless; on 07-28 it added 0.6–1.3 and was the best configuration
for every model. The reason is visible directly: the regex list alone fixes 23 clips on 07-28
against 3 on 07-27, because that set is dense with garbled *Maas Approach* hails. The list is
free, instant, and across both corpora has now regressed **0 of 138 clips** — the only
contender of which that is true.

**What shipped.** Not a new provider: channel 01 already sent every transmission to Claude
Haiku 4.5 in `identify.py` and already applied the regex pass to what came back. The
correction was a by-product of a vessel-extraction prompt, and it showed — it beat plain regex
on one corpus and lost on the other. Giving that same call a correction-focused prompt, and
`temperature=0`:

| | 07-27 (49) | 07-28 (89) |
|---|---|---|
| uncorrected | 41.1% | 33.0% |
| regex only | 39.4% | 27.0% |
| CH01 before | 38.8% / 39.7%* | 27.6% |
| **CH01 after** | **34.9%** | **24.9%** |

\* the same prompt, twice — that spread is what `temperature=0` removed. The call ran at the
API default of 1.0 while rewriting the transcript the plugin displays, so ~1 point of the
gap between any two candidate prompts was sampling noise. Regressions fell 5 → 1 and 10 → 2.

Two defects surfaced only by running the thing against real transmissions, neither caught by
the suite: the new prompt silently **dropped** content (`"Okay, understood. One five zero
zero, Pilot."` lost its opening) because "never add content" had been carried across and
"never remove content" had not; and `vessel_type` came back as the *string* `"null"`, which is
truthy and would have reached the plugin as `[GH NIGHTINGALE/null]`. The second predates this
work — the schema has always said `"<name or null>"` — and was merely made deterministic by
fixing the temperature. `_null_out_placeholders()` now coerces it at the single point every
field passes through.

**Not adopted.** A free model as the correction pass. `gemma-4-26b:free` was the WER leader on
both corpora, but within ~0.5 points of Haiku, which was already in the pipeline and paid for;
it wins partly by guessing boldly (`Marsh Bridgerton` → `Maas Approach` — right; `start a
private airplane` → an invented `stand by on zero one, one six` — wrong), it is 8× slower, and
free endpoints proved to be the least reliable component measured. Extending correction to
non-CH01 maritime and to airband is deferred until there is an external antenna and real
traffic on those channels.

## Known limitations

- **~36% pooled word error rate even in the best configuration** (35.9%, see the
  nautical-term-corrections row in the table above). This is genuinely hard audio —
  accented non-native English, real radio noise, dense maritime jargon, proper nouns not
  in Whisper's vocabulary. Not something further parameter tuning fixes.
- **Nautical-term and vessel-name errors** (the same vessel name transcribed differently
  across nearby clips) are a distinct, known category. "ladder" → "letter"/"leather" was
  the worst instance and is now corrected — see below.
  A first pass of evidence-backed regex corrections now runs in the proxy (see "Current
  configuration" above — a ~5.7-point pooled WER improvement, though ~4.2 of those points
  come from rules that predate this pass and only ~1.3 from the rules added in it);
  fuzzy/LLM-based correction for cases the regex pass can't catch is still planned for a
  later phase per `CLAUDE.md`'s "Additional Features" section (vessel-name AIS matching is
  already built and working, see below).
- **The AIS cache is never pruned, and matching ignores where a vessel actually is**
  (noted 2026-08-04, not yet acted on). `match_by_name`, `match_by_callsign` and
  `_find_ais_hints` all scan every cached entry. Nothing is ever removed, nothing expires,
  and **no entry carries a timestamp**, so staleness is not merely unfiltered — it is not
  even knowable. `ROTTERDAM_BBOX` constrains what *enters* the cache via the aisstream
  subscription, never what is eligible to match, and at `[[51.0, 2.95], [52.85, 6.0]]` it
  spans roughly 205 × 210 km — Amsterdam, the IJsselmeer and most of the Dutch coast.

  Measured over the 8,464-vessel cache, by distance from Maas Center (52.02 N, 3.88 E):
  **505 vessels (6%) within 25 km**, 1,699 (20%) within 50 km, 5,794 (68%) within 100 km,
  and 1,356 (16%) with no position ever received. So ~94% of the candidate pool cannot
  plausibly be talking on this working channel.

  Both misidentifications diagnosed that day picked a distant ship over a near one:
  VIKTORIA (**111 km**) took PECHORA STAR's identity (17 km), and GOOILAND (**141 km**) took
  THULELAND's (35 km). Of 104 stored identifications, 5 named a vessel last seen beyond
  100 km — including, twice, a vessel literally named **MAAS** sitting 164 km away, against
  a channel where "Maas Approach" is said in nearly every transmission.

  Not fixed yet, and deliberately not fixed blind. Three things stop it being a one-line
  distance filter: position is *last known* rather than current, so distance is a
  plausibility proxy and not proof; 16% of entries have no position at all, and excluding
  them would break callsign matches for ships that never broadcast one; and 34% of
  identifications fall in the 50–100 km band, which is genuinely ambiguous because the VTS
  area reaches well offshore and vessels call in while still approaching. A hard cut would
  destroy more than it fixes. This is the first real change queued behind
  `bench_identify.py` having corrected labels. The prerequisite, and pure instrumentation
  that changes no behaviour, is recording a `last_seen` timestamp on every cache write.

- **whisper.cpp/ROCm has a real, unresolved GPU driver hang** on this hardware
  (RX 7900 XTX / gfx1100 / ROCm 6.1.3 under WSL2) — this is why `groq` is now the default
  backend; everything in this bullet applies only under `STT_BACKEND=whisper_cpp`.
  It is a per-request-random race that can
  strike any GPU kernel launch regardless of decode settings, audio content, or timing —
  matches the long-standing, still-open [ROCm/ROCm#2689](https://github.com/ROCm/ROCm/issues/2689)
  (confirmed to affect this exact GPU on bare-metal Linux too, not a whisper.cpp or
  WSL2-specific bug). `--no-flash-attn` measurably reduces frequency but does not
  eliminate it. Not fixable from this repo. Mitigated with a watchdog in
  `whisper-proxy.py`: it tracks in-flight backend requests and auto-restarts
  `whisper-server` if one is stuck past `WHISPER_WATCHDOG_STUCK_S` (default 25s), so a
  hang becomes a ~15-20s automatic recovery (surfaced to the plugin as one connection-reset
  error) instead of a 60s hang or a full Windows GPU-driver-timeout popup.

## What the phonetic callsign anchor is actually worth (2026-08-08)

Measured properly after two earlier attempts got it wrong, and the record of those attempts
matters more than the result.

**The instrument had to be characterised first.** `resolve_conversation` was sampling at the
API default of 1.0 while `identify.py` had pinned `temperature=0` all along, so the first
A/B run compared 85.1% precision against 78.5% and neither figure meant anything. Pinning the
resolver to 0 was necessary but is *not* sufficient on its own: repeat runs are now identical
on 143 of 143 transmissions in both arms, yet one conversation (10:48:19-10:48:30, three
turns) still flips between naming NOORDSTROOM and naming nobody across runs. That single
conversation is worth ~2.8 precision points, which is larger than most changes worth
measuring. **Always repeat a run before believing a difference of a few points.**

**The result, three runs per arm, `AIS_PHONETIC_CALLSIGN` the only variable:**

| arm | precision | recall | correct | wrong | missed | declined |
|-----|-----------|--------|---------|-------|--------|----------|
| off | 82.0%     | 71.6%  | 73      | 16    | 13     | 41       |
| on  | 87.6%     | 76.5%  | 78      | 11    | 13     | 41       |

Per transmission, **exactly 5 of 143 changed, all of them `wrong` -> `correct`, and all five
are the same conversation** -- the BERGE TOWNSEND call of 10:17:50, every turn of which had
been resolving as VISION. Nothing regressed. Correct declines are 41 in all six runs.

**Two claims made earlier the same day were wrong, from single-run measurements:**

- *"Correct declines fell 41 -> 30, so a longer candidate list makes the adjudicator readier
  to name a ship."* No such effect exists. 30 came from one anomalous run and never recurred.
- *"Aggregate precision/recall is a wash-to-slightly-down."* It is +5.6 and +4.9 points.

Both survived because `--out-json` wrote aggregates only. It now emits a row per scored
transmission (time, conversation, label, prediction, outcome), which is what turned a
plausible story about adjudicator behaviour into "five turns of one conversation, all fixed".
Aggregates cannot tell you *which* transmissions moved, and the arithmetic that made the
decline story sound mechanical -- declines down 11 while `wrong` rose only 3 -- was impossible
to begin with, since a turn's bucket family is fixed by its label.

## The resolver's noise floor is now a printed number (2026-08-09)

`bench_identify.py --resolve --repeats N` runs the resolver N times, reports mean and
observed spread per metric, and names the transmissions whose verdict moved between runs.

**Why it has to exist: `temperature=0` does not make the resolver reproducible, and cannot.**
Anthropic's API documentation states that temperature 0 *"never guaranteed identical
outputs"*, and there is **no seed parameter**. Greedy sampling does not make batched GPU
inference bit-reproducible, so a near-tie flips on floating-point noise. That matches what
was observed exactly: 143/143 transmissions repeat identically except one conversation
(10:48:19–10:48:30) that alternates between naming NOORDSTROOM and naming nobody — **worth
~2.8 precision points on its own**, larger than most changes worth measuring.

So a single run per arm cannot separate a real effect from that flip, and the old advice
("repeat every run before believing a difference of a few points") depended on remembering
to do it by hand. Now `spread` is printed next to the mean, and the rule is mechanical: *a
difference between two arms smaller than their spreads is not evidence.*

Three details that are deliberate rather than incidental:

* **A turn that vanishes from one run counts as unstable, not absent.** Segmentation can move
  a transmission out of its label window between runs; dropping it would understate noise on
  precisely the runs least worth trusting.
* **`--repeats` without `--resolve` is refused.** Re-scoring stored verdicts N times re-reads
  the same JSON and would print a spread of zero — false confidence, which is the exact thing
  this flag exists to remove.
* **Every run is kept in `--out-json`, not just the summary.** The per-turn rows are what
  reduced a confident, entirely fictional story about adjudicator behaviour to "five turns of
  one conversation" (see above); keeping them means the next such question is answerable
  without paying for the API calls again.

**Measured on the 08-07 verified corpus, 3 runs (2026-08-09):** precision 87.6 / 87.6 / 84.8
(**spread 2.9 points**), recall 76.5 in all three (**spread 0.0**), 140 of 143 transmissions
stable, and the three that moved are exactly the 10:48:19–10:48:30 turns flipping `nobody` ↔
`NOORDSTROOM`. The long-standing "~2.8 precision points" estimate measures at 2.9.

Three things that run established:

* **Recall is the trustworthy metric at this corpus size; precision carries all the noise.**
  `correct` is 78 in every run — the flip is between two kinds of *wrong* (declining vs
  naming NOORDSTROOM), so recall (`correct/identifiable`) cannot move while precision
  (`correct/named`) drops as soon as three more turns get named. Judge an A/B on recall, and
  treat a precision delta under ~3 points as noise.
* **The flip costs measurement stability, not accuracy** — that conversation scores wrong in
  every run regardless, it only changes bucket.
* **Two repeats would have been actively misleading.** Runs 1 and 2 were identical, so
  `--repeats 2` reports a spread of 0.0. Three is the demonstrated minimum, not a guess.

Ruled out on the way to the provider explanation, so they need not be re-checked: candidate
ordering is deterministic end to end (`_resolver_candidates` builds an insertion-ordered
dict, `_find_ais_hints`' `seen` set is membership-only and never iterated, `_fresh_snapshot`
returns `list(cache.keys())` off a JSON-loaded dict). The one genuine nondeterminism vector
in that path — `cutoff = datetime.datetime.now()` in `_fresh_snapshot` — is inert because
`AIS_MAX_AGE_MIN` defaults to `0` and short-circuits it. That stops being true if the default
ever changes.

## Measuring receiver settings by IQ replay (2026-08-08)

The plugin taps demodulated audio, so every receiver setting is baked in before anything
here sees it, and none had ever been measured. `server/iq_replay.py` replays one raw-IQ
recording through different demodulator settings: identical RF into every arm, one variable
changed, references hand-verified once.

Arms are written as captures-style directories, so `bench.py` and `bench_prompt_ab.py` score
them unchanged — including the bootstrap confidence interval, which matters because a bare
WER delta of a point or two carries no information.

Two things that are easy to get wrong and are pinned by tests:

* **Segmentation is computed once and shared.** Per-arm VAD would move clip boundaries, and
  `bench_prompt_ab.py` pairs on `clip_id`, so it would compare different transmissions under
  the same id and still print a number.
* **The plugin's DSP chain is transcribed, not improved.** `iq/plugin_dsp.py` is pinned
  against real `_raw.wav` -> `_sent.wav` pairs at correlation 1.000000. A first attempt
  scored 0.946 because `Decimator.LinearResample` steps by `(len-1)/(outLen-1)`, not by
  `fromRate/toRate`.

A third thing surfaced only while writing this task's own tests: `segments.cut()` (see
above) now returns exactly one entry per requested segment, including a zero-length array
when a segment lies entirely past a shorter arm's end. `iq_replay.write_clip` never writes
that array as-is — a zero-frame wav is not guaranteed well-formed for whatever reads it next
(`bench.py` posts the raw bytes to the STT server) — and it never drops the clip either,
since that would remove that one arm's id from `bench.discover_clips` while other arms still
have it, desyncing the very pairing `cut()`'s one-entry-per-segment guarantee exists to
protect. It writes a short burst of silence instead, at the same index.

Cannot be measured this way: RF gain (applied before the ADC, so baked into the recording)
and the SDR# audio-NR plugins (downstream of the tap point).

### What the harness actually found: both receiver hypotheses are dead (2026-08-09)

Run against a 61.8 min daytime capture — 97 transmissions, 9.8 min of speech, 15.8% duty.

**Bandwidth: null, and this one does not rest on WER.** A 12.5 kHz channel filter retains a
**median 96.56%** of in-channel power on the real capture (worst 94.81%). Carson's rule
predicted ~16 kHz occupancy, but Carson is a conservative *bound*, not a description of the
signal — independent-phase speech components rarely add constructively enough to drive full
deviation. There is nothing there to recover.

**Squelch: no detectable difference, on a test with poor power.** Squelch-off vs squelch-on at
floor+6 dB, scored on 29–30 hand-verified clips, two independent transcription runs:

| | off | on | delta | 95% CI |
|---|---|---|---|---|
| fresh run | 21.2% | 25.8% | +4.6 pts | [−2.8, +13.8] |
| draft run | 21.3% | 27.1% | +5.8 pts | [−2.0, +16.1] |

The CI spans zero both times. Note what that does and does not license: it rules out
squelch-on being *much* better, but could not have detected a penalty smaller than ~14 points.
"No significant difference" is not "no difference". The ~1.2-point gap between the two runs is
decoder non-determinism plus one clip lost to a 429 — the decoder has its own noise floor,
like the resolver.

**The hypothesis was refuted on mechanism, not just on WER.** "Squelch clips the opening
syllables where the vessel name is" does not reproduce: across 97 real transmissions the gate
opens ~1 ms after carrier-up, always before the first word, and **0 of 97** clips had it close
mid-carrier. The 08-08 figure of "53% of the first 20 ms" came from synthetic *abrupt-onset*
audio where speech starts on the same sample as the carrier. Real operators key up, then speak.

What the arms actually differ by is the segmenter's 300 ms pad, which starts each clip
*before* the carrier — so in the squelch-off arm that region is discriminator hiss at **7.45x
the RMS of the speech that follows**.

**Squelch cannot be switched off regardless**, and this outranks the WER result.
`ReadSquelchOpen()` returns `null` when squelch is disabled and `VoiceActivityDetector` falls
back to an audio-RMS gate — which ends up **stuck wide open**: `NoiseFloor` starts at 0 and is
only updated by `if (!active) UpdateNoiseFloor(rms)`, so once hiss clears the 0.010 absolute
floor no frame is ever inactive, the floor never calibrates, `endOfSpeech` never fires, and
only `MaxSpeechSec` flushes — a 30 s chunk of pure hiss every 30 s, ~120 hallucinated STT
requests an hour. Verified live by the operator: toggling squelch off and back on emits a
chunk transcribed as "Muaah".

This is the same defect as the segmentation bug above, and it **cannot be fixed the same way**:
the plugin is an `IRealProcessor` on demodulated audio and never sees the RF, so it has no
channel-power signal to gate on. **SDR#'s squelch is that gate.** The fallback should refuse
loudly rather than stream noise; making the floor calibrate would only swap stuck-open for
stuck-shut, because the audio domain does not contain the information.

**Neighbouring channels are empty**, so 0.25 MSPS stays. Coast-station channels sit 50 kHz
apart and the capture spans centre ±125 kHz, so Ch 02–05 are already recorded. Measured over
the hour: Ch 01 has a 14.5 dB peak-to-floor gap at 14.1% duty; **Ch 02, 03, 04 and 05 all show
1.0–1.6 dB and 0.0% duty.** No traffic to recover, so there is no case for a higher rate or a
re-centre. (A vessel *was* directed to "channel zero two, pilot Maas" during the hour and Ch 02
still shows nothing — following that traffic would need the ship side at 156.100 MHz, 4.6 MHz
away and out of reach at any RTL sample rate.)

### "Is anyone transmitting?" is an RF question, not an audio one (2026-08-09)

The first version of this harness answered it in the audio domain and was wrong in a way
that looked healthy. On the real hour capture it cut **57.6 of 60.1 minutes into 42 clips**,
three of them over six minutes long, against an independently measured truth of **23
transmissions at 4.8% duty**.

An FM discriminator computes `angle(x[n] * conj(x[n-1]))` — phase only. With no carrier the
phase of successive noise samples is uniformly random, so the discriminator emits
**full-scale hiss**. Measured on synthetic IQ, dead air comes out of it **1.44x louder than
speech**. Both `detect_segments` and `apply_squelch` gated on demodulated-audio amplitude,
where noise and speech are equally loud, so **no threshold value could have worked** — the
measurement was in the wrong domain, and tuning would only have moved which minutes were
wrong. `plugin_dsp.normalize` then peak-normalised every clip to −1 dBFS, so the RMS of the
garbage looked perfectly healthy (median 0.191).

This is what a squelch is for, and the fix is what a squelch does: `Demodulator` now
measures mean-square **channel power on the IQ, after the channel filter and before the
discriminator** — the last point at which amplitude still exists — into 1 ms frames on the
absolute capture timeline (`Demodulator.power_db`, 29 MB/hour). Segmentation and the squelch
both gate on that track. The threshold is the capture's own noise floor (20th percentile)
plus a margin, so it is independent of RF gain.

**Re-measured on the real hour capture, through the shipped code path:**

| | audio RMS (was) | RF channel power (now) | independent truth |
|---|---|---|---|
| clips | 42 | 39 | 23 transmissions¹ |
| covered | 57.6 of 60.1 min | **3.3 of 60.1 min** | ~3.3 min |
| duty | 95.8% | **4.83%** | **4.8%** |
| longest clip | 746 s | 19.9 s | — |

¹ The survey script bridges gaps under 3 s, so it merges consecutive overs into one
"transmission"; `detect_segments` uses a 600 ms hangover and keeps them separate, which is
closer to how production clips are cut. 39 vs 23 is that setting, not a disagreement — the
duty cycle, which does not depend on it, agrees to 0.03 percentage points, and the two
numbers come from completely different code (the survey takes FFT bin power at the channel
offset, with no channel filter and no demodulator at all).

The floor-to-peak gap on real data is ~19 dB (floor −38.9, strongest transmission −20.0),
not the ~32 dB a synthetic fixture shows — worth knowing before trusting a margin chosen
against synthetic input.

**Why no test caught it:** every fixture was built with `synth_nfm`, which always emits a
carrier. "No transmission" — the state the radio is in for ~95% of a captured hour — was a
case the test suite could not express. `baseband.synth_noise` now exists solely to express
it, and `test_dead_air_is_not_a_segment` is the regression. The lesson generalises past this
bug: a synthesiser that can only produce the working case makes a whole class of failure
untestable, and the coverage number will not show it.

The frame length is 1 ms because the **squelch** needs it, not the segmenter: the squelch
arm exists to quantify how much of a transmission's opening the gate eats, so a frame
coarser than a few ms would quantise away the thing being measured. The segmenter wants
~20 ms and just averages down.

## Why TULIPA SEAWAYS was not identified (2026-08-10)

A live exchange at 23:30:38 resolved to nobody, and the ship was in the cache the whole time.
Worth recording because the resolver was *not* at fault and the obvious fix does not apply.

Whisper garbled "Tulipa Seaways" two different ways across the two turns, and neither garbling
can reach the real ship through `fuzz.ratio`:

| probe | ratio vs TULIPA SEAWAYS | `match_by_name` returned |
|---|---|---|
| `DULLIP CEEWEES` | 57.1 | None |
| `SEAWAYS` | 66.7 | **SEAWAY** (a different real ship) |
| `TO A LIFT AT SEAWAYS` | 70.6 | **LYSVIK SEAWAYS** |
| `TULIPA` | 60.0 | **TUULIA** |
| `TULIPA SEAWAYS` | 100.0 | correct |

The cutoff is 76. The full name matches perfectly and never survived the channel; every
surviving fragment either falls short or lands on somebody else. The AIS hints handed to the
resolver were MAAS -- a vessel genuinely named that, pulled in by the words "Maas Approach" --
and SEAWAY. So the candidate list held two plausible wrong ships and not the right one, and the
resolver, told to choose from the list or return null, correctly returned null.

This is the SANTA ISABEL MAERSK failure recurring; see the comment above `_live_match_candidates`
in `conversations.py`.

**The word-match path would NOT fix this.** `SEAWAYS` is shared by 13 cached vessels (the DFDS
fleet) and `TULIPA` by 2, so its ambiguity guard returns None both times. That is the guard
working, not failing.

**The untested idea:** this failure is phonetic rather than orthographic -- "Dullip Ceewees" and
"Tulipa Seaways" sound alike and spell differently. A crude phonetic normalisation scores that
pair at 76.9 against the raw 57.1. That normaliser was written to fit this one case, clears the
cutoff by 0.9 points, and has never been measured for false positives across 8,672 names where
near-homophone ship names are common. It is a hypothesis, not a finding. If this class of miss
recurs, measure a phonetic scorer against the existing corruption corpus and `bench_identify`
before shipping anything.

## The AIS receiver moved house, and what it can actually hear (2026-08-11)

The local AIS receiver was a dead end at the end of 2026-08-10: a ~4 km reception shell
centred on the operator, nothing within 25 km of Maas Center, and a conclusion that ~17 m of
antenna height was the only lever left. Two changes on 2026-08-11 reopened it.

**The station is now a separate machine.** The second dongle moved to a Windows 10 PC at
`192.168.2.1` that can run 24/7. It runs AIS-catcher alone; nothing else is installed on it.
`server/start-all.bat` no longer launches AIS-catcher, and the proxy's `ais_local` listener
is not fed by it — `bind()` is loopback-only by design, and widening it would let anything on
the LAN inject vessel data.

**The antenna moved to the seaward side of the house.** Same dipole, same position to within
~20 m, so every distance figure anchored to 52.111188 N / 4.292962 E still holds.

### The horizon was never the constraint at 4 km

The `~17 m` figure is right *for reaching 30 km* — solve `30 = 4.12(√h + √10)`. But the same
formula at realistic heights gives a radio horizon of **18.8 km at 2 m** and **22.2 km at
5 m**. A 4 km shell was therefore never horizon-limited; something was eating 15+ dB, and the
house standing between the dipole and the water is the obvious candidate. Height only starts
to matter beyond ~19 km. Moving the antenna, at no extra height, roughly tripled the range.

### Three of the first four sector records were not ships

The range map is meant to answer *how far can this station hear a ship*, and most of what
transmits on the band is not a ship. Unfiltered, the map read:

| sector | claimed | what it actually was |
|---|---|---|
| NW | **5136.95 km** | MMSI `171003622` — `171` is not an allocated MID, name was binary garbage. A corrupt message that passed CRC. |
| S / SW | 20.6 / 20.4 km | MMSI `111205510` — `111` is a **SAR aircraft**. Airborne, so its horizon is enormous and it says nothing about surface reception. |
| W | 16.51 km | MMSI `992446045` — `99` is an **AtoN**, and `[V]` marks it *virtual*: a navigation mark that does not physically exist. |
| W | 13.89 km | MMSI `2444066` = `002444066` — `00` is a **coast station**, a fixed shore site with a proper mast. |

`mmsi_class()` in `server/ais_station_count.py` classifies by prefix per ITU-R M.585, and the
map now counts ship stations only, reporting what it excluded rather than dropping it
silently. A `MAX_PLAUSIBLE_KM` of 150 catches the corrupt-position case.

**This is the same failure as the retracted 69.5 km claim**: a number that looked like
reception range but came from somewhere else. The fix is the same in spirit — record *which*
vessel and *when* alongside every maximum, so a surprising figure can be audited instead of
believed. `last_signal` (AIS-catcher's seconds-since-heard) is the tell: on 2026-08-11 every
long-range record was 17–30 minutes stale while nearby traffic was seconds old, which is the
signature of marginal, occasional catches rather than sustained tracks.

### What it hears, ships only

- **Hoek van Holland at ~20 km** (bearing 224.6°) — real ship stations at the mouth of the
  waterway, the traffic that transits to and from the approach area.
- **Maas Center's own bearing (250°) reaches only ~3.8 km.** The offshore approach is still
  out of range; the coastal corridor into it is not.
- **MULTRASHIP PROTECTOR at 40.98 km** (MMSI 244830813, a genuine ship station, 53 messages).
  No antenna height explains this: 41 km needs ~46 m by line of sight. It is tropospheric
  ducting over seawater. **So range here is propagation-dependent, not a fixed ceiling** —
  an earlier claim in this session that the NW sector was "horizon-limited at 97% of its
  geometric limit" does not survive it. Maas Center at 30 km is demonstrably within reach
  under the right conditions; the open question is how often.

### AISHub

The station comfortably meets AISHub's contributor bar (≥10 vessels and ≥90% uptime, both
averaged over 7 days): over the first ten hours, **mean 45.8 vessels/hour, minimum 40**.
AIS-catcher feeds AISHub directly with `-u <host> <port>` — **without** `JSON on`, which
wraps the NMEA in an envelope their parser cannot read. AIS Dispatcher is not needed; every
feature of it that matters here is already in AIS-catcher, and a second 24/7 process is a
liability when uptime is being formally measured.

Measurement tool: `server/ais_station_count.py`. See the user manual for how to run it.

### The silence watchdog is muted, not removed (2026-08-11)

`AIS_SILENCE_WARN_SEC` now defaults to `0`. The instrument is correct and its diagnosis is
true, which is the problem: aisstream has delivered nothing since 2026-08-05, so it fired
every 60 s, roughly 8,600 times, drowning output still worth reading. A warning that is
permanently on carries no information and only costs attention.

It is a mute rather than a deletion because it is the only thing that would catch aisstream
failing *again* after it recovers — and this feed has already changed failure shape once
mid-outage. Restore with `AIS_SILENCE_WARN_SEC=60`; there is a commented line in
`start-all.bat` saying to do so the moment the feed returns.

### aisstream recovered, and the mute is lifted (2026-08-25)

**The feed delivers again.** Measured directly, three arms of 25 s each against
`wss://stream.aisstream.io/v0/stream`:

| Arm | Frames in 25 s | Breakdown |
|---|---|---|
| key 1, Rotterdam box | 577 | 504 PositionReport, 72 ShipStaticData |
| key 1, whole world | 2,490 | 2,105 PositionReport, 384 ShipStaticData |
| key 2, Rotterdam box | 552 | 485 PositionReport, 66 ShipStaticData |

`ShipStaticData` is the half that carries vessel *names*, so this is a feed that is useful and
not merely open. It is also not the 2026-08-08 failure shape, where the socket accepted the
connection and then delivered nothing.

Both keys work. The recovery date is unknown — the outage began 2026-08-05 13:31 UTC and
nothing was watching in between, so all that can honestly be said is that it was dead then and
delivering on 2026-08-25.

`AIS_SILENCE_WARN_SEC` therefore returns to its documented value of 60, in the code default and
the setting catalogue both. The condition the mute existed for is gone, and the instrument
matters more now than it did before: aisstream became the *default* source on the same date.

### The aisstream box was never moved to the sea box (2026-08-25)

Found while checking whether the aisstream path still worked after the AISHub cutover. It did —
841 live frames fed through `_process_ais` produced 764 cached vessels, all named, with exact
and one-character-garbled name lookups both hitting, callsign lookup hitting, ship types
resolving through the shared table and `_find_ais_hints` returning hints. The provider-agnostic
`record()` merge point did its job.

**But the bounding box had not been touched.** aisstream subscribed with a module constant,
`[[[51.0, 2.95], [52.85, 6.0]]]` — the *wide* box, eastern edge at 6.0, reaching up the Rhine —
with no environment variable able to change it. The 2026-08-13 sea-box change moved
`AISHUB_BBOX` and only `AISHUB_BBOX`, because AISHub was the only source in use and nothing
pointed at the second copy of the box.

That is a 685-versus-43 difference in duplicate-name groups, sitting unnoticed behind a code
path that was working correctly in every other respect. The lesson is narrow and worth stating:
**a dormant-but-live alternative path does not inherit the measurements made on the active one.**
"Still live and still tested" was true of the adapter and false of its configuration.

Fixed by giving aisstream `AIS_BBOX`, defaulting to the same sea box, and routing both feeds'
box parsing through one `ais.parse_bbox()` — two copies of that parse is how they came to
disagree in the first place.

## Offering the near misses instead of asserting them (2026-08-18)

`/conversations` now shows, under a conversation nobody was identified in, the best three
vessel names found *below* the identification cutoff, labelled as unconfirmed. It never
names anyone: `vessel` and `mmsi` stay null, and nothing reads the block back.

This is not "lower the cutoff", which was measured on 2026-08-12 and cost **11 precision
points** — fourteen correctly-unnamed conversations became confident misidentifications.
That result stands. The difference is that a suggestion is not an assertion, so the
precision the cutoff protects is untouched by construction.

**Measured on the 35 unidentified conversations of the 08-13/14 labels:**

| retrieval | truth in top 1 | top 3 | top 5 | anywhere |
|---|---|---|---|---|
| live hints, cutoff 85 (unchanged, for reference) | — | 2 | 2 | 2 |
| global rank, no probe filter | 0 | 3 | 7 | 22 |
| **global rank + document-frequency filter (shipped)** | **4** | **9** | 12 | 22 |

Two findings behind those rows.

**The live retrieval cannot be relaxed into a shortlist.** `_find_ais_hints` runs
`extractOne` per probe and stops at *n* slots, so as the cutoff falls the wrong ships arrive
first, fill the slots and bury the right one — reachability is non-monotonic, going
35 → 38 → 35 → 29 → 24 as the cutoff drops 85 → 80 → 76 → 70 → 65. `suggest_vessels` scores
every (probe, name) pair and ranks globally, so there are no slots to fill.

**Most of the shortlist was the shore station.** Two real cargo ships are named MAAS and
MAS, and "Maas Approach" opens nearly every call, so they took **56 of the 105 top-three
slots** — which is the whole gap between 3 and 9 above. They are removed by document
frequency rather than a hand-written place list: a probe heard in more than
`AIS_SUGGEST_DF_MAX` of stored conversations is procedure, not a name (MAAS is in 93%,
MAAS APPROACH in 91%, while a vessel calls once or twice). That re-learns whatever station
is on air, which matters for the Aviation band. Below `AIS_SUGGEST_MIN_DOCS` stored
conversations the table cannot tell a ship from the station, so nothing is shown at all.

Gating on score buys nothing — the hit rate is flat at 23–26% whether the block is shown
always or only when its best candidate clears 85. So it is shown whenever it has anything.

**9/35 is an upper bound.** The pool was the frozen 08-15 cache, a superset of what was live
when each conversation resolved; candidate lists are not persisted, so the real figure
cannot be recovered. `last_seen` stores only the maximum, so it cannot discriminate either.

The value is not the rank. `heard "Meld Them In"` beside MELTEMI I is something a reader who
heard the audio settles instantly and no edit-distance scorer can: the block turns an
unanswerable question into an adjudicable one.

Knobs: `AIS_SUGGEST` (on), `AIS_SUGGEST_N` (3), `AIS_SUGGEST_FLOOR` (55),
`AIS_SUGGEST_DF_MAX` (0.05), `AIS_SUGGEST_MIN_DOCS` (30). Cost is ~53 ms per unidentified
conversation, against a resolver pass that takes seconds.

**The constraint that keeps this safe**: suggestions are computed in `_store_resolved`,
*after* identity is final and after the correction pass has run, and are read only by
`_format_suggestions`. They reach neither the vessel log, nor the resolver's candidate list,
nor the conversation-correction pass. `tests/test_suggestions.py` asserts each of those.
That feedback path is exactly how a sub-cutoff match once rewrote "motor vessel to Leland"
into "motor vessel Vlieland" and named the wrong ship.

## "The name arrived intact and we still missed it" is 2 of 35 (2026-08-18)

Prompted by three shortlist entries where a full vessel name had been spoken plainly on a
conversation that resolved to nobody: CIELO DI ULSAN, MSC SAUDI ARABIA, BORIS SOKOLOV.

Measured properly, the class is small. Scoring what the **live pass extracted** against
ground truth — a cleaner instrument than raw n-grams, because it is the model's own reading
of the name — over the 35 unidentified conversations in the 08-13/14 labels:

    name arrived at or above the 76 matcher cutoff    2
    never arrived that intact                        33

The closest of the other 33 are `Baltic`/BALTICBORG (75), `Hammerstar`/AMUR STAR (74),
`Amundsen`/MARAN AMUNDSEN (73) — the same cluster pressed under the threshold already
recorded. This corroborates "the name never arrives" rather than overturning it.

**A measurement trap avoided.** Asking instead "would the heard name match *something* in
today's cache" gives 20 of 50 and is worthless: the matches are `Maranamest` → AMUSE where
the truth is MARAN AMUNDSEN, and `Free North` → NORAH where the truth is MELTEMI I. Matching
something is not matching the right thing. Same shape as the candidate-recall trap of
2026-08-12; always score against labels.

The two real cases have **opposite** causes, which is the whole reason the instrumentation
below now exists:

- **BORIS SOKOLOV** — heard as `Boris Sokolov`, title case. That casing is the tell:
  `enrich_with_ais` returns the result untouched when AIS matches nothing, so a title-case
  `live_vessel` means the model heard the name and AIS had no such ship. `live_mmsi` was
  never set, `_live_match_candidates` keys on `live_mmsi`, so the vessel never reached the
  candidate list and the resolver correctly refused an off-list name. *Why* AIS had no
  match could not be determined — the cache state at that instant is not recorded.
- **SEA BANCKERT** — heard as `SEA BANCKERT`, uppercase, i.e. the AIS spelling written back
  by enrichment. AIS *did* match, the ship *was* offered, and the resolver rejected it:
  *"SEA BANCKERT is phonetically similar but the callsign PEER does not appear."* The shore
  station had repeated the name clearly. Prompt rules 3-5 all elevate callsigns while rule 6
  demotes the live-pass candidate to "a lead, not evidence", and between them a good name
  match lost to a callsign a 12-second check-in was never going to contain.

**The prompt was NOT changed.** Across the 19 stored conversations where the live pass had
an AIS match and the resolver still named nobody, the live match is the *wrong* ship in
almost all of them (WESTZEE, GEORGIA, HOUSTON, MULSANNE, OLSKE) and refusing was correct.
SEA BANCKERT is n=1, prompt changes here have cost 11 WER points before, and any change
needs `bench_identify --resolve --repeats 3` against a 2.9-point noise floor to mean
anything. Label more callsign-absent rejections before touching it.

### What was changed: two fields, so the next one is diagnosable

`live_mmsi` is now stored beside `live_vessel` on every turn, and the candidate list the
resolver saw is stored on every row it produces as `resolver_candidates` (name, MMSI, and
which pass put it there — no positions or particulars). Together they separate the two
causes above, which the store previously could not:

| stored | means |
|---|---|
| `live_vessel` set, `live_mmsi` null | the name was heard; AIS had no such ship |
| both set | AIS matched; the ship reached the candidate list |
| truth absent from `resolver_candidates` | never offered — a cache-membership problem |
| truth present, row unnamed | offered and rejected — a resolver-judgement problem |

Recorded on the resolver-error path too, so "never ran" and "ran and found nobody" stay
distinguishable. Costs about 1.1 KB per row, taking the 300-row store from ~595 KB to
~900 KB. This gap has now blocked two post-hoc investigations; that is what it buys.

### And one incidental defect

`_null_out_placeholders` coerced the bare words `null`, `none`, `n/a`, `unknown`, `-` but
not the schema placeholder itself, so the literal string `<name or null>` was journalled
once as the vessel of a real transmission. Now nulled when a value is *wrapped* in angle
brackets — wrapped, not merely containing one, because AIS 6-bit decode artefacts arrive as
names like `CGAS TIGET<<` and those are a bad cache entry to be seen, not a placeholder to
be hidden. One occurrence in ~1,400 turns.

## A five-day-stale barge outranked a ship 12 km away (2026-08-18)

BELLONA — 135×12 m inland barge, draught 1.5 m, bound for Antwerp, 72.5 km from Maas Center,
last AIS fix **122 hours** old — was named with high confidence over GT VELA, which was
12.8 km away and had reported its position **seven minutes** before the call.

Neither ship cleared the 85 hint cutoff (GT VELA peaked at 80.0, BELLONA at 83.3). BELLONA
reached the resolver entirely through `_live_match_candidates`, which re-resolves `live_mmsi`
through `match_by_mmsi` — and that reads `_mmsi_index` directly, so `AIS_MAX_AGE_MIN` never
applied to it. Roughly half of a real candidate list arrives by that route: one live capture
showed 14 candidates, 6 of them live matches. It was the largest ungated path in the system.

Then the correction pass, told the vessel was BELLONA, rewrote `"GT, rella"` to
`"GD Bellona"` and `"GD Bella, Mard Brennan"` to `"GD Bellona, Maas Approach"` — so the
displayed page no longer contained the "GT" that would have shown the error. The raw text is
stored, which is the only reason this was reconstructable.

### The fix, and what was measured

`AIS_LIVE_MATCH_MAX_AGE_MIN` bounds the age of a live-match candidate; **360 minutes, on by
default**. `bench_identify --resolve --repeats 3` over the 08-13/14 labels, only this varied:

| | off (0) | 360 min | |
|---|---|---|---|
| precision | 87.1% | **88.3%** | +1.2 |
| recall | 65.6% | **66.3%** | +0.7 |
| correct | 386 | 386 | unchanged |
| wrong | 57 | **51** | −6 |
| missed | 145 | 145 | unchanged |

Spread **0.0** on every metric across three runs per arm, so this is signal rather than the
~2.9 points of resolver sampling noise. Nothing was lost — not one correct identification
became a miss. All six transmissions that moved are one conversation where nobody was
identifiable and PRESTO, 29 hours stale, was being named across all six.

The contrast between removing and adding candidates is now measured from both ends:
relaxing `AIS_HINT_MIN_SCORE` **added** candidates and cost 11 precision points; this
**removes** them and gains 1.2.

### Age is measured from the last good poll, not the wall clock

`ais._reference_now()` returns the last successful poll time, falling back to the wall clock
before any poll. During a feed outage the wall clock ages every vessel out together, so "the
estuary emptied" and "the feed died" become indistinguishable and the filter would destroy
identification exactly when it is already broken — this project has already lost six days to
a feed that failed quietly. Freezing the reference means a stalled feed changes nothing.

It is also what makes the filter measurable at all: a bench runs against a frozen cache days
later, where every entry is stale by wall clock and any bound excludes the whole cache.
`ais.set_poll_reference()` lets the bench say when each conversation happened, and
`bench_identify` sets it per conversation. Every entry in both real caches carries
`last_seen`, so the "unknown age is not fresh" rule costs nothing in production.

### Two arms that could NOT be measured, and why

Both looked like regressions and neither is. Recording this so the numbers are not re-derived
and believed.

- **Proximity tie-break on the shortlist** (`AIS_SUGGEST_TIEBREAK`, off): 9/35 → 8/35. The
  one conversation that flipped, NOORDBORG, was ranked out for being 101.6 km away — but
  that is where the ship was in the 08-15 snapshot, a day after it called. A frozen cache
  keeps only each vessel's LATEST fix, so proximity-at-conversation-time does not survive in
  it. The arm scored where ships ended up, not where they were on the radio.
- **Callsign suffix fallback** (`AIS_CALLSIGN_SUFFIX_FALLBACK`, off): −0.9 precision, all
  four flips in one conversation. The vessel spelled out *"Victory seven alpha six zero five
  two"* = V7A6052, the callsign of ATLANTIC PRESTIGE **538010447**, and the fallback picked
  exactly that ship. The label says ATLANTIC PRESTIGE by NAME, two ships carry it, and
  `_resolve_expected` resolves a name through `_vessel_cache` — one entry per name — so the
  truth silently became **244700991**, a 2 m-draught barge. The benchmark was wrong, not the
  change. This is the label artifact already recorded as worth ~7 precision points.

### A label naming a ship two vessels share is now NOT SCORED

`_resolve_expected` used to resolve a label name through `_vessel_cache`, which holds one
entry per name — so a shared name silently became whichever ship happened to be there, and
the ambiguity was invisible by construction. The lookup is now built from `_mmsi_index`,
keyed on each entry's current name, so it carries every ship of that name.

An ambiguous line is **skipped with a loud warning**, not fatal. A hard error was written
first and was wrong: it kills a whole file of good labels to protect a handful of lines. This
is the file's own stated policy — *an unlabelled conversation is simply not scored; a guessed
one corrupts every number computed from this file.*

Seven of the 122 lines in `identification-labels-2026-08-13_14.txt` are affected: MAATJE (3
ships), ATLANTIC PRESTIGE (2), MARJATTA ×2 (2), CONDOR ×3 (3). **Every identification number
measured before 2026-08-18 inherited them.** Excluding them, stored-verdict precision on that
corpus reads 89.8% rather than 84.1% and recall 67.9% rather than 64.2% — so the artifact was
worth about 5.7 precision points, close to the ~7 estimated when it was first noticed.

### Why they cannot be relabelled afterwards

Disambiguating two ships of one name is done by matching what the vessel **said about itself**
against where each candidate actually was — "passing the reporting line", "at Anchorage South
position Lima", "on our way to the pilot station". That works only while the fixes are fresh.
The cache keeps just the latest position per vessel, so days later the ships have moved and
the evidence is gone; the audio still says "position Lima" but there is nothing left to check
it against.

This is the same lesson as `_CANDIDATE_FACTS`, from the other end: **a vessel's position is
only knowable at the moment it is used.** Recording candidate positions at resolve time is
what makes these lines answerable in future. The seven above are lost, and are simply not
scored.

Only one was settleable from the transcript: ATLANTIC PRESTIGE spelled out *"Victory seven
alpha six zero five two"* — V7A6052, i.e. 538010447, the 200 m ship rather than the 135 m
barge. The other six carry no spoken callsign or draught to discriminate on.

### The gap the suffix fallback exists to close

CLAMOR SCHULTE (V7B2710) spelled its callsign out and went unidentified. `_spelled_out_runs`
produced `7B2710` — complete but for the leading V, swallowed before the spelling began
("call **Sun**victor seven") rather than garbled within it. `match_by_callsign` cannot match
a short run; `_partial_callsign_pattern` declines a span with no garble as "the exact
lookup's job"; and `match_by_callsign_suffix`, which resolves `7B2710` to exactly one cached
vessel, is reachable only *through* that pattern. Each path defers to the other and nobody
tries the tail. Its draught, 6.1 m, matched the spoken "six decimal one zero" exactly.

The fallback runs behind both existing gates: the tail must fit exactly one cached callsign,
and a name resembling that vessel must be spoken somewhere in the window. Over the 300 stored
conversations it fires on 4, agreeing with the stored verdict on 3 and supplying CLAMOR
SCHULTE on the 4th.

**On by default since 2026-08-18, by decision rather than by measurement.** Worth flagging,
because everything else here is the other way round. Its one arm read −0.9 precision and was
*invalid* rather than negative — all four transmissions it moved were the ATLANTIC PRESTIGE
conversation, scored against a barge because the label named a ship two vessels share. The
favourable evidence is candidate inspection, which is the weaker instrument: relaxing
`AIS_HINT_MIN_SCORE` also looked good by candidate recall and then cost 11 precision points.

The arm is re-runnable now that ambiguous labels are skipped, since the conversation that
invalidated it is excluded. Until someone runs it, this is the first setting to switch off if
identification regresses.

## Testing

- C#: `dotnet test SDRSharp.SttPlugin.Tests/SDRSharp.SttPlugin.Tests.csproj`
- Python: `py -m pytest server/tests`
- End-to-end accuracy: `py server/bench.py --captures <dir> --references <file> --matrix full`
  (see `server/bench.py`'s docstring; `server/references.txt` documents the ground-truth
  format, including conventions for uncertain/inaudible audio)
- Identification accuracy: `py server/bench_identify.py --labels identification-labels.txt`
  (draft the labels with `--make-labels` first, then correct them; add `--resolve` to
  re-run the resolver and score that, which is how a resolver or prompt change is A/B'd).
  `GET /api/labels` on the control panel is now a second source of the same file format,
  built from verdicts recorded against real conversations in the durable archive
  (`server/conversation_archive.py`) rather than from `--make-labels`' guesses — see the
  user manual's [conversation archive](user-manual.md#the-conversation-archive) section.

## Deployment

Build `SDRSharp.SttPlugin` in Release, copy the DLL/PDB to
`D:\SDR\SDRSharp\Plugins\SttPlugin\` (SDR# must be closed — it locks the DLL while
running). Start the server stack via `server/start-all.bat` (copy from
`start-all.bat.template` and fill in API keys, which are gitignored).
