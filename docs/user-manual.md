# User manual

Everything needed to install, configure and run the SDR# speech-to-text plugin.

- [What it does](#what-it-does)
- [How it fits together](#how-it-fits-together)
- [Requirements](#requirements)
- [Install](#install)
- [Configure](#configure)
- [Run](#run)
- [Using the plugin](#using-the-plugin)
- [The conversations page](#the-conversations-page)
- [Settings reference](#settings-reference)
- [Optional: local GPU backend](#optional-local-gpu-backend)
- [Measuring accuracy on your own traffic](#measuring-accuracy-on-your-own-traffic)
- [Troubleshooting](#troubleshooting)
- [Legal note](#legal-note)

---

## What it does

It listens to whatever SDR# is tuned to, detects speech, transcribes it, and shows the text
in a panel inside SDR#. It was built for Rotterdam maritime VHF, so it also knows some
things about that traffic — but the transcription itself works on any voice channel.

**Core**

- **Voice activity detection in the plugin.** Only speech is sent. Silence and static never
  leave your machine, which keeps API usage roughly proportional to how much talking there
  actually is.
- **Audio conditioning before transcription** — DC block, 150 Hz high-pass, anti-aliased
  resample to 16 kHz, peak normalisation. Noisy VHF benefits from this more than from any
  decoder setting.
- **Two interchangeable speech-to-text backends.** Groq's hosted Whisper (default, no GPU
  needed) or a local whisper.cpp server. One environment variable switches between them.
- **Domain corrections** for terms the decoder reliably gets wrong (`draft` → `draught`,
  `boys` → `buoys`, and fuzzy matching of the many ways "Maas" comes out).

**Maritime extras** (optional — each disables itself if you don't supply a key)

- **Vessel identification.** Names and callsigns are extracted from the speech and matched
  against a live AIS feed, so a transmission can be labelled with the ship, its MMSI and type.
- **Retrospective conversation resolution.** Radio exchanges are identified *after* they
  finish, using the whole exchange rather than one transmission — so a garbled opening call
  is resolved by the clearer turn that follows, or by a callsign spelled out three turns
  later. Results appear on a web page; the live transcript is left alone.

**For tuning and evaluation**

- Optional recording of every chunk sent (audio + a JSONL index), a benchmark harness that
  replays those clips and reports word error rate, and an offline replay tool for the
  identification pipeline.

---

## How it fits together

```
  SDR#  ──audio──▶  Plugin (C#)  ──HTTP──▶  Proxy (Python)  ──▶  Groq  or  whisper.cpp
                    VAD, filtering              corrections,        (cloud)     (local GPU)
                    chunking                    vessel ID,
                         ▲                      AIS matching
                         └────── text ──────────────┘
                                                    │
                                                    └──▶  http://localhost:9000/conversations
```

Three pieces:

1. **The plugin** (`SDRSharp.SttPlugin/`) runs inside SDR#. It taps the filtered audio,
   decides what is speech, conditions it, and posts each chunk to the proxy.
2. **The proxy** (`server/whisper-proxy.py`) is a small Python HTTP server. It owns all the
   decoder settings, talks to whichever backend is selected, applies corrections, and does
   the maritime enrichment. Keeping this outside the plugin means tuning is a proxy restart,
   not a plugin rebuild and an SDR# restart.
3. **The backend** does the actual speech-to-text.

The plugin only ever talks to the proxy, so backends can change without touching it.

---

## Requirements

| | |
|---|---|
| **OS** | Windows 10/11 (SDR# is Windows-only) |
| **SDR#** | A working install with a supported SDR device |
| **Python** | 3.10 or newer, on `PATH` as `py` |
| **.NET SDK** | 8 or 9, to build the plugin |
| **SDR# SDK** | `SDRSharp.Common.dll` and `SDRSharp.Radio.dll` from your SDR# install |
| **Groq API key** | Free tier is sufficient — see [limits](#groq-free-tier-limits) |

Optional:

| | |
|---|---|
| **Anthropic API key** | Vessel-name extraction and conversation resolution |
| **aisstream.io key** | Live AIS positions for vessel matching (free) |
| **AMD/NVIDIA GPU + WSL2** | Only if you want the local whisper.cpp backend |

> **You do not need a GPU.** The default backend is Groq's hosted Whisper. The local GPU
> path exists as an alternative and is entirely optional.

---

## Install

### 1. Clone and install Python dependencies

```bash
git clone https://github.com/johncranen-ux/sdrsharp-stt-plugin.git
cd sdrsharp-stt-plugin
py -m pip install -r server/requirements.txt
```

### 2. Point the plugin project at your SDR# SDK

`SDRSharp.SttPlugin/SDRSharp.SttPlugin.csproj` references two DLLs from your SDR# install.
Edit the `<HintPath>` entries if your SDR# is not where the project expects:

```xml
<Reference Include="SDRSharp.Common">
  <HintPath>C:\SDR\SdrSharpSDK\sdrplugins\lib\SDRSharp.Common.dll</HintPath>
</Reference>
```

### 3. Build the plugin

```bash
dotnet build SDRSharp.SttPlugin/SDRSharp.SttPlugin.csproj -c Release
```

### 4. Deploy it into SDR#

Copy the built DLL into its own folder under your SDR# installation:

```
<SDRSharp>\Plugins\SttPlugin\SDRSharp.SttPlugin.dll
```

**That is the whole installation.** Current SDR# versions discover plugins by scanning
`Plugins\<Name>\` on startup — there is no registration file to edit. Restart SDR# and the
**Speech to Text** panel appears in the left-hand list.

<details>
<summary>If you are on an older SDR# that needs a registration line</summary>

Builds from before plugin auto-discovery required the plugin's type and assembly to be
listed in `SDRSharp.exe.config`. If your SDR# has a `<sharpPlugins>` section in that file,
add:

```xml
<add key="SpeechToText" value="SDRSharp.SttPlugin.SttPlugin,SDRSharp.SttPlugin" />
```

This line is also kept in `Plugins\SttPlugin\MagicLine.txt` for convenience. Many plugins
still ship such a file out of habit; on a modern SDR# it is documentation, not
configuration, and nothing reads it.

</details>

### 5. Get a Groq API key

Sign up at [console.groq.com](https://console.groq.com/) and create an API key. The free
tier is enough for continuous monitoring — see the limits below.

---

## Configure

Copy the template and fill in your keys:

```bash
cd server
copy start-all.bat.template start-all.bat
```

Then edit `start-all.bat`. It is **gitignored**, so your keys never enter version control.

```bat
set GROQ_API_KEY=gsk_your_key_here

:: Optional — leave as placeholders to disable these features
set ANTHROPIC_API_KEY=sk-ant-api03-YOUR_KEY_HERE
set AISSTREAM_API_KEY=YOUR_AISSTREAM_KEY_HERE
```

Everything else has working defaults.

---

## Run

```bash
server\start-all.bat
```

That starts the proxy and prints what it is doing:

```
Anthropic API key: OK
[AIS] loaded 7433 vessels from cache
Conversation resolver: enabled (window gap 60s) -> http://localhost:9000/conversations
Whisper proxy  :  localhost:9000  ->  groq api.groq.com (whisper-large-v3)
```

Then start SDR#, tune to a voice channel, open the **Speech to Text** panel and tick
**Enable transcription**.

To stop, close the proxy window. Restarting `start-all.bat` is safe at any time — it frees
the port first, so you never end up with two proxies fighting over it.

---

## Using the plugin

The panel has:

| Control | What it does |
|---|---|
| **Server URL** | Where the proxy is. Default `http://localhost:9000`. |
| **Mode** | `Maritime` enables vessel identification; `Airband` applies only band-neutral corrections. |
| **VAD threshold** | Speech detection sensitivity. Lower = more sensitive. Start at 10. |
| **End silence (ms)** | Silence before a transmission is considered finished. 600 ms suits VHF. |
| **Language** | Two-letter code passed to the decoder. `en` for most maritime traffic. |
| **Initial prompt** | Example phrasing that biases the decoder toward the right vocabulary. The single biggest accuracy lever — see [design notes](design-notes.md). |
| **Enable transcription** | Master on/off. |
| **Capture chunks** | Saves every chunk sent as WAV plus a JSONL index. For benchmarking. Off by default. |
| **Capture raw stream** | Saves the continuous audio. Large files. Off by default. |

The status line under the controls shows live diagnostics — VAD state, RMS level, noise
floor, queue depths. `sendq` rising steadily means the proxy is not keeping up.

### Tuning the VAD

If transmissions are missed, lower **VAD threshold**. If it triggers on static, raise it.
The status line's `rms` and `floor` values tell you what the detector is seeing: speech
should push `rms` clearly above `floor`.

---

## The conversations page

With an Anthropic key set, open **http://localhost:9000/conversations**.

Each entry is one radio exchange with a single identified vessel, decided *after* the
exchange ended so late evidence counts. It shows the identity, how confident the resolver
was, the evidence it used, and every transmission. Where the live transcript had guessed a
different vessel, that guess is shown alongside in red — so corrections are visible rather
than silently applied.

### Spotting a corrected transmission

Transmission text is not always verbatim. After identity is resolved, a second pass re-reads
each turn against the rest of its exchange and repairs what the channel garbled — a mangled
opening call from the shore station's clearer answer, a garbled readback from the instruction
it was answering. Two things on the page tell you when that has happened:

- **A green `N corrected` pill** in the conversation header, next to the confidence badge.
  Its absence means nothing in that exchange was changed, and what you are reading is exactly
  what was transcribed live.
- **A dotted green underline** under the text of each turn that was changed. Turns without it
  are untouched.

**Hover the underlined text** to see what was actually heard, as `was: <original>`, followed by
every substitution and the reason for it — for example
`Sarbertside -> Starboard side (garbled readback of the instruction in turn 7)`.

Nothing is ever overwritten: the original wording and the reason for each change are stored
alongside the correction, and `/api/conversations` returns all of it (`text` is the live
transcript, `conv` the corrected version, `changes` the list of substitutions). If you would
rather see the raw transcript everywhere, set `CONVERSATION_CORRECT=off` and the page renders
exactly what it did before this pass existed.

On both HTML pages an identified vessel's name is a link to its
[VesselFinder](https://www.vesselfinder.com/) page, looked up by MMSI. A vessel matched by
name alone, with no MMSI, is shown as plain text — there is nothing reliable to look up.

`/identified-vessels` is an append-only file, so rows written before this was added stay
unlinked; new transmissions get links as they arrive.

Also available:

| URL | What |
|---|---|
| `/conversations` | Resolved exchanges (auto-refreshes) |
| `/identified-vessels` | Per-transmission identification log |
| `/api/conversations` | The same data as JSON |
| `/api/ais-cache` | Current AIS vessel cache |

---

## Settings reference

All are environment variables, normally set in `start-all.bat`.

### Backend

| Variable | Default | Meaning |
|---|---|---|
| `STT_BACKEND` | `groq` | `groq` or `whisper_cpp` |
| `GROQ_API_KEY` | — | Required when the backend is `groq` |
| `GROQ_MODEL` | `whisper-large-v3` | Groq model id |
| `GROQ_TIMEOUT_S` | `30` | HTTP timeout; the plugin gives up at 60 s |
| `PROXY_PORT` | `9000` | Port the plugin connects to |

### Decoding

| Variable | Default | Meaning |
|---|---|---|
| `WHISPER_LANGUAGE` | `en` | Language hint |
| `WHISPER_PROMPT` | maritime example | Vocabulary bias |
| `WHISPER_TEMPERATURE` | `0` | Sampling temperature |
| `WHISPER_BEAM_SIZE` / `WHISPER_BEST_OF` | `5` | **whisper.cpp only** — Groq exposes no decoder controls |

### Quality filters

| Variable | Default | Meaning |
|---|---|---|
| `AIS_HINT_FILTER` | `on` | Stops ordinary speech being matched to real ships |
| `AIS_HINT_MIN_SCORE` | `85` | Similarity needed for an AIS name hint |
| `AIS_HINT_MAX_NGRAM` | `4` | Longest word span looked up as a vessel name (2 restores pre-2026-08-06 behaviour) |
| `AIS_MAX_AGE_MIN` | `0` (off) | Ignore vessels not heard from in this many minutes when matching. Excludes rather than deletes, so raising it brings them back. Needs a day of `last_seen` data before a threshold can be chosen from evidence |
| `AIS_NAME_FILTER` | `on` | Stops a misheard name matching a short vessel spelled inside it ("Orason" → `RA`) |
| `AIS_NAME_MIN_SCORE` | `76` | Similarity needed to match a spoken name to an AIS vessel |
| `AIS_PARTIAL_CALLSIGN` | `on` | Identifies a vessel from a partly-garbled spelled-out callsign when a spoken name agrees |
| `PARTIAL_CALLSIGN_MIN_NAME_SCORE` | `60` | Name similarity required to accept a partial-callsign match |
| `PROMPT_ECHO_FILTER` | `on` | Drops transcriptions that are the prompt read back |
| `MAAS_FUZZ_THRESHOLD` | `70` | Fuzzy matching for "Maas" before "Approach" |

Turning `AIS_NAME_FILTER` or `AIS_PARTIAL_CALLSIGN` off restores the previous behaviour
exactly, which is what makes them a usable rollback rather than a rough approximation. The
defaults were measured, not chosen — see `docs/design-notes.md`.

### AIS vessel source

`AIS_SOURCE` selects where vessel data comes from:

| value | meaning |
|---|---|
| `aishub` (default) | Poll AISHub every 15 minutes. Needs `AISHUB_USERNAME`. |
| `aisstream` | The original aisstream.io websocket. Needs `AISSTREAM_API_KEY`. Dead since 2026-08-05; kept because it was reliable for a long time and may return. |
| `off` | No vessel enrichment. |

`AISHUB_USERNAME` is the key from AISHub's welcome mail. **It goes in `server/start-all.bat`
alongside the other API keys — that file is gitignored. Never put it in a tracked file.** There
is no `.env` loader in this project; every setting is read straight from the environment.

Without `AISHUB_USERNAME` the proxy still starts and transcribes; it prints `AIS feed: disabled`
and runs without vessel enrichment.

Other settings: `AISHUB_BBOX` (`latmin,latmax,lonmin,lonmax`, default `51.0,53.2,2.0,6.0`) and
`AISHUB_POLL_SEC` (default 900; values under 60 are raised to 60, because AISHub answers a
faster caller with no data at all).

When a heard name fits more than one ship, `/conversations` lists the candidates with
VesselFinder links instead of choosing. Pick the one that fits what was said — a vessel already
inside the harbour is not dropping anchor at Echo 3.

### Possible matches, when nobody was identified

A conversation that resolved to nobody gets a second, greyer block: the best three vessel
names found **below** the identification cutoff, each with the fragment it matched.

```
Possible matches — these scored below the identification cutoff, so nobody was named.
Unconfirmed:
  1  MELTEMI I   76   heard "Meld Them In"
  2  HEIN        73   heard "Them In"
  3  THEMHOF     73   heard "Them"
```

These are **not** identifications and the system will never act on them. They are there
because you can settle by ear what the matcher cannot: "Meld Them In" for MELTEMI I is
obvious to someone who heard the transmission and invisible to an edit-distance score.

Expect to be right about a quarter of the time — measured on hand-labelled traffic, the
correct ship is in the three 9 times out of 35. The other three-quarters are near-misses
worth a glance and nothing more. The block never appears beside a conversation that *was*
identified, and never in the first 30 conversations after a fresh start, because it needs a
corpus to learn which words are the shore station rather than a ship.

Turn it off with `AIS_SUGGEST=off`. `AIS_SUGGEST_N` changes how many are listed.

### Conversation resolution

| Variable | Default | Meaning |
|---|---|---|
| `CONVERSATION_RESOLVER` | `on` | Retrospective identification and `/conversations` |
| `RESOLVER_LIVE_CANDIDATES` | `on` | Offers the resolver the vessel the live pass already matched |
| `CONVERSATION_GAP_S` | `60` | Silence that closes a window |
| `CONVERSATION_MAX_CHUNKS` | `40` | Hard cap on window size |
| `AIS_SUGGEST` | `on` | The sub-cutoff "possible matches" block on unidentified conversations |
| `AIS_SUGGEST_N` | `3` | How many possible matches to list |
| `AIS_SUGGEST_FLOOR` | `55` | Name-similarity score below which nothing is suggested |
| `AIS_SUGGEST_DF_MAX` | `0.05` | A word span heard in more than this share of stored conversations is treated as shore-station procedure, not a name |
| `AIS_SUGGEST_MIN_DOCS` | `30` | Stored conversations needed before suggestions appear at all |
| `AIS_LIVE_MATCH_MAX_AGE_MIN` | `360` | A vessel the live pass matched is only re-offered to the resolver if its AIS fix is newer than this. Age counts from the last **successful** AIS poll, so a stalled feed does not age every ship out at once. Measured 2026-08-18: +1.2 precision, 6 false positives removed, no correct identification lost. `0` disables it |
| `AIS_CALLSIGN_SUFFIX_FALLBACK` | `on` | Try the tail of a spelled-out callsign that decoded cleanly but short (heard "call **Sun**victor seven" → `7B2710` for `V7B2710`). The tail must fit exactly one cached callsign **and** a resembling name must be spoken in the same conversation. On by decision rather than by measurement — if identification regresses, switch this off first |
| `AIS_SUGGEST_TIEBREAK` | `off` | Rank equally-scoring suggestions by plausibility. Off; not yet measurable — see design-notes |
| `ANTHROPIC_API_KEY` | — | Unset disables identification entirely |
| `AISSTREAM_API_KEY` | — | Unset disables AIS matching |
| `AIS_SILENCE_WARN_SEC` | `0` (off) | Warns when a *connected* AIS feed stops delivering — the failure that otherwise looks identical to a quiet channel. Muted by default since 2026-08-11 because aisstream has delivered nothing since 08-05 and it fired every 60 s. **Set it to `60` the moment the feed recovers**; it is the only thing that catches a relapse |

### Conversation correction

| Variable | Default | Meaning |
|---|---|---|
| `CONVERSATION_CORRECT` | `on` | Re-corrects each turn's text from the rest of its exchange after identity is resolved. `off` restores the pre-correction behaviour exactly |
| `CONVERSATION_CORRECT_PROVIDER` | `anthropic` | LLM provider for the correction pass |
| `CONVERSATION_CORRECT_MODEL` | `claude-haiku-4-5-20251001` | Model id for the correction pass |
| `CONVERSATION_CORRECT_FEWSHOT` | `on` | Includes worked examples in the prompt |
| `CONVERSATION_CORRECT_TEMPERATURE` | `0` | Sampling temperature. Set to `none` for models that reject the parameter (claude-sonnet-5 does) |
| `CONVERSATION_CORRECT_TIMEOUT_S` | `60` | HTTP timeout for the correction call; falls back to this default if set to something that doesn't parse as a number |
| `CONVERSATION_FEWSHOT_FILE` | — (uses a small synthetic set) | Path to your own worked examples, built from real exchanges |

`CONVERSATION_FEWSHOT_FILE` must point at a path matching one of the gitignored patterns
(`*fewshot*.json` or `*examples*.json`, anywhere in the repo) — that is what stops a real
exchange, which is received radio traffic, from ever being committed. The CI transcript gate
is a list of known filenames rather than a content scan, so a path that doesn't match those
patterns bypasses both `.gitignore` and the gate. Keep the file outside the repository
entirely (e.g. alongside `start-all.bat`) if you'd rather not rely on the name matching.

### Groq free-tier limits

| Limit | Free tier | Typical busy channel |
|---|---|---|
| Audio seconds/hour | 7,200 | ~500 |
| Requests/day | 2,000 | ~105/hour |
| Requests/minute | 20 | ~2 |
| Max file size | 25 MB | ~1 MB for a 30 s chunk |

Requests/day is the only one you can realistically reach — about 19 hours of continuous
busy-channel monitoring. The proxy warns in its console as the daily allowance runs down.

---

## Optional: local GPU backend

Only if you want transcription to run entirely on your own hardware.

You need whisper.cpp built with GPU support inside WSL2, plus a Whisper model.
`whisper_gpu_setup_guide.docx` in the repository root walks through this for an AMD card
with ROCm; NVIDIA users can follow whisper.cpp's own CUDA instructions.

Once `whisper-server` runs on port 8080, switch backends in `start-all.bat`:

```bat
set STT_BACKEND=whisper_cpp
```

Restart the proxy. It now targets the local server and arms a watchdog that restarts
`whisper-server` if a request hangs.

> **Before choosing this path**, read the [design notes](design-notes.md). Measured on the
> same clips, Groq and local whisper.cpp scored the same word error rate (0.416 both), so
> the local backend buys privacy and offline operation — not accuracy. On the AMD hardware
> this was developed against it also brought frequent GPU driver hangs, which is why the
> cloud backend is the default.

---

## Measuring accuracy on your own traffic

1. Tick **Capture chunks** in the plugin and record a session. Clips and an index land in
   `<SDRSharp>\Plugins\SttPlugin\captures\<date>\`.
2. Generate a draft reference file, pre-filled with what the system heard:

   ```bash
   py server/make_references.py --captures "<captures>\<date>" --out server/references-local.txt
   ```

3. Correct it while listening to each clip. **Listen properly** — the pre-fill is often
   confidently wrong, and accepting it without checking makes the resulting scores flatter
   the model rather than measure it.

   The format is one clip per line, `<4-digit id><TAB><correct transcript>`:

   ```
   0000	Maas Approach, Maas Approach, this is Motorvessel Northern Harrier, over.
   0001	Northern Harrier, Maas Approach, go ahead.
   0002	Good morning sir, we are approaching the pilot station.
   ```

   Conventions, which `bench.py` understands:

   | Situation | Write | Effect |
   |---|---|---|
   | Best guess, not certain | `Fjordstrom?` | Scored as a normal word; the `?` is a note to yourself and is ignored |
   | A word or phrase is unintelligible | `this is [inaudible], calling` | That span is excluded from scoring rather than penalising the model |
   | Nothing usable, or you want to skip the clip | leave empty after the TAB | Clip is excluded from all aggregates |

   A partly finished file is perfectly usable — clips without a reference are simply not
   scored, so you can stop whenever you have enough.

   On CH01 the plugin records the AIS-enriched display string rather than the plain
   transcript (`[CALLAO EXPRESS/tanker] (MMSI:218839000) Callao Express, Maas...`).
   `make_references.py` strips that prefix for you; if you spot one that survived, delete
   it — a reference nobody said corrupts the score it feeds.
4. Score:

   ```bash
   py server/bench.py --captures "<captures>\<date>" --references server/references-local.txt \
       --matrix groq_prompt --host localhost --port 9000 --path /v1/audio/transcriptions
   ```

`bench-report.html` gives per-clip results and a pooled word error rate.

`references-local.txt` and anything else matching `references-*.txt` is gitignored, so your
recordings stay yours.

---

## Measuring what your AIS receiver can hear

`server/ais_station_count.py` answers two questions about a local AIS receiver: how many
distinct vessels it hears per hour, and how far it reaches on each bearing. It was written to
test a station against [AISHub](https://www.aishub.net/join-us)'s contributor bar — at least
10 vessels and at least 90% uptime, both averaged over 7 days — but the range map is the more
useful half if you are deciding where to put an antenna.

It needs **nothing but the Python standard library**, so it will run on any machine with a
Python 3.8+, including one that has nothing else installed.

### Point AIS-catcher at it

The receiver does not have to be the same machine. Run the counter where you want the data:

```bash
py server/ais_station_count.py --station 192.168.2.1:8100
```

and on the receiving station:

```bat
AIS-catcher.exe -gr TUNER 42.1 RTLAGC off -p 2 -v 10 ^
  -Z <your-lat> <your-lon> -N 8100 ^
  -P <counter-host> 10111
```

`-P` is a TCP destination, chosen over UDP deliberately: the output of this tool is a count
judged against a threshold, and silent datagram loss would be indistinguishable from
genuinely hearing fewer vessels. `--station` additionally polls AIS-catcher's `/ships.json`
once a minute to build the range map; counting works without it, the map does not.

If the counter runs on a different machine, open an inbound firewall rule for TCP 10111
scoped to your local subnet, and give that machine a fixed address — if it moves, the station
transmits into nowhere and the log shows a gap that reads exactly like the station going
silent.

### Reading the output

Ctrl+C prints the hourly table, the verdict against the threshold, and the range map. Every
line is also appended to `ais-station-count.jsonl` (gitignored), which survives restarts, so
the true maxima for a whole run can be reconstructed even if the process is restarted partway.

Three things in the design are worth knowing, because each exists to stop a specific wrong
conclusion:

- **A heartbeat is written every minute whether or not traffic arrives.** No heartbeat means
  *this process* was not running; a heartbeat with zero messages means the station really was
  quiet. Without that distinction your own machine sleeping looks identical to the receiver
  failing — and if you are evidencing an uptime figure, that ambiguity argues against you.
- **The range map counts ship stations only.** Most of what transmits on the band is not a
  ship, and non-vessels flatter the map badly: a SAR aircraft (MMSI prefix `111`) is airborne
  and has an enormous horizon, a virtual AtoN (`99…` marked `[V]`) does not physically exist,
  and a coast station (`00…`) is a fixed shore mast. Excluded records are reported, not
  silently dropped.
- **Every sector maximum records which vessel set it and how long before the poll it was
  heard.** A record-breaking range from a vessel last heard 30 minutes ago deserves less trust
  than one heard 10 seconds ago, and without those fields you cannot tell the difference.

---

## Troubleshooting

**"Request timed out (60 s)" on every chunk**
The proxy is not responding. Check its console window is running and reachable:
`curl http://localhost:9000/`.

**Transcript shows `HTTP 503` occasionally**
The backend dropped a request. On Groq this is usually a transient network problem; on
`whisper_cpp` it is normally the watchdog recovering a hung GPU, and service resumes in
15–25 s. The affected chunk is lost — it is not retried.

**Nothing is transcribed, `rms=0.000` in the status line**
No audio is reaching the plugin. Check SDR# is playing audio and that the plugin is enabled.

**Everything is transcribed, including static**
Raise **VAD threshold** until `rms` for static sits below `floor`.

**Vessel names are wrong**
Check the `/conversations` page — the retrospective pass often corrects what the live
transcript guessed. If names are still poor, the `Initial prompt` is the biggest lever.

**Plugin does not appear in SDR#**
SDR# skips plugins it cannot load, silently in the UI — but it does record why. Check
**`<SDRSharp>\PluginError.log`**, which names the failing type and gives a stack trace:

```
*** Plugin Load Error - 2026-07-30 11:41:10.978
Config Key   'SDRSharp.TimeShift.TimeShiftPlugin,...'
Message      'Method not found: 'Void SDRSharp.PanView.Waterfall.set_Zoom(Int32)'.'
```

A `Method not found` or assembly-load message there is almost always the .NET 8 runtime trap
described in [CONTRIBUTING.md](../CONTRIBUTING.md). If the log has no entry for the plugin
at all, SDR# never saw it — check the DLL really is at
`<SDRSharp>\Plugins\SttPlugin\SDRSharp.SttPlugin.dll` and that you restarted SDR#.

**`MissingMethodException` or an assembly load error on startup**
The plugin targets `net9.0-windows` but SDR# hosts it on the **.NET 8** runtime. See
[CONTRIBUTING.md](../CONTRIBUTING.md) — this is a known trap with a documented workaround.

**Quota warnings in the proxy console**
`[quota] Groq daily requests remaining: 180` means you are approaching the 2,000/day free
tier limit. It refills continuously rather than resetting at midnight.

---

## Legal note

This software transcribes radio you receive. In most countries listening is lawful, but
**recording, publishing or otherwise passing on the content of communications not addressed
to you often is not** — see Telecommunicatiewet art. 18.13 in the Netherlands, or ITU Radio
Regulations 17.3 internationally. Rules vary; check yours.

The repository deliberately contains no real received traffic. The capture features are
off by default, and anything you record with them is yours to handle responsibly.
