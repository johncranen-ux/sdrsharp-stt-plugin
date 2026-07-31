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
word whose `rapidfuzz.ratio` to "maas" is ≥ `MAAS_FUZZ_THRESHOLD` (default 70). Reviewed
against every rewrite it makes on the full set: no false positives, and the ambiguous real
words (`Marsh`, `Moth`, `last`, `are`) fall below threshold and are correctly left alone.

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
| Maritime prompt | Fluent example transmissions (see `DEFAULT_MARITIME_PROMPT` in `whisper-proxy.py`) | The single largest lever found: ~9-10 points of WER improvement over no prompt. A keyword-list-style prompt was tried first and rejected — it primes Whisper to echo the list back verbatim on noisy/silent audio. |
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

## Known limitations

- **~36% pooled word error rate even in the best configuration** (35.9%, see the
  nautical-term-corrections row in the table above). This is genuinely hard audio —
  accented non-native English, real radio noise, dense maritime jargon, proper nouns not
  in Whisper's vocabulary. Not something further parameter tuning fixes.
- **Nautical-term and vessel-name errors** ("ladder" → "letter", the same vessel name
  transcribed differently across nearby clips) are a distinct, known category.
  A first pass of evidence-backed regex corrections now runs in the proxy (see "Current
  configuration" above — a ~5.7-point pooled WER improvement, though ~4.2 of those points
  come from rules that predate this pass and only ~1.3 from the rules added in it);
  fuzzy/LLM-based correction for cases the regex pass can't catch is still planned for a
  later phase per `CLAUDE.md`'s "Additional Features" section (vessel-name AIS matching is
  already built and working, see below).
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

## Testing

- C#: `dotnet test SDRSharp.SttPlugin.Tests/SDRSharp.SttPlugin.Tests.csproj`
- Python: `py -m pytest server/tests`
- End-to-end accuracy: `py server/bench.py --captures <dir> --references <file> --matrix full`
  (see `server/bench.py`'s docstring; `server/references.txt` documents the ground-truth
  format, including conventions for uncertain/inaudible audio)

## Deployment

Build `SDRSharp.SttPlugin` in Release, copy the DLL/PDB to
`D:\SDR\SDRSharp\Plugins\SttPlugin\` (SDR# must be closed — it locks the DLL while
running). Start the server stack via `server/start-all.bat` (copy from
`start-all.bat.template` and fill in API keys, which are gitignored).
