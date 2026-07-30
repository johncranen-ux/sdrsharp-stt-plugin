# SDR# Speech-to-Text Plugin

Transcribes VHF radio traffic (Rotterdam maritime, later aviation) received in SDR#, using
either Groq's hosted Whisper API or a local whisper.cpp server on an AMD GPU via ROCm.
See `CLAUDE.md` for the full project brief and business requirements.

## Architecture

1. **STT backend** — one of two, selected by `STT_BACKEND` (see below).
2. **Python proxy** (`server/whisper-proxy.py`, port 9000) — owns all decoder parameters
   (see below), rewrites the plugin's request to inject them, applies hallucination
   filtering and maritime-term corrections, and (on channel 160.650 / Maas Approach)
   extracts vessel names via Claude and enriches them against a live AIS feed.
3. **C# SDR# plugin** (`SDRSharp.SttPlugin/`) — captures post-filter audio, runs VAD
   (pre-roll buffer, squelch-aware, adaptive noise floor), applies an anti-aliased
   resample + DC-block + highpass + normalize chain, and sends chunks to the proxy.

The plugin only ever talks to the proxy on `http://localhost:9000`, so switching backends
never requires a plugin rebuild or an SDR# restart.

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

**Open follow-up:** extend `_apply_sttt_corrections` with Groq's "Maas" variants, ideally
validated against the un-referenced 2026-07-28 captures rather than the set the rules came
from.

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
