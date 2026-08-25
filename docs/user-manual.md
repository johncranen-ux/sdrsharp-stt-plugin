# User manual

Everything needed to install, configure and run the SDR# speech-to-text plugin.

- [What it does](#what-it-does)
- [How it fits together](#how-it-fits-together)
- [Requirements](#requirements)
- [Install](#install)
- [Configure](#configure)
- [Starting the station](#starting-the-station)
- [Reading the Dashboard](#reading-the-dashboard)
- [Using the plugin](#using-the-plugin)
- [Conversations and Vessels](#conversations-and-vessels)
- [Settings](#settings)
- [Settings reference](#settings-reference)
- [Optional: local GPU backend](#optional-local-gpu-backend)
- [Measuring accuracy on your own traffic](#measuring-accuracy-on-your-own-traffic)
- [Measuring what your AIS receiver can hear](#measuring-what-your-ais-receiver-can-hear)
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
| **.NET SDK** | 9, to build the plugin. **Not needed** if you install the [prebuilt release](https://github.com/johncranen-ux/sdrsharp-stt-plugin/releases/latest). .NET 8 alone cannot build it — the project targets `net9.0-windows` — even though SDR# hosts the result on the .NET 8 runtime. |
| **SDR# SDK** | `SDRSharp.Common.dll` and `SDRSharp.Radio.dll` from your SDR# install. **Not needed** for the prebuilt release. |
| **Groq API key** | Free tier is sufficient — see [limits](#groq-free-tier-limits) |

Optional:

| | |
|---|---|
| **Anthropic API key** | Vessel-name extraction and conversation resolution |
| **aisstream.io key** | Live AIS positions for vessel matching. Free, available to anyone, and the default source — see [AIS vessel source](#ais-vessel-source). |
| **AISHub username** | An alternative AIS source with better data, but issued **only to stations contributing their own AIS feed**. Not required. |
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

> **Skipping the build.** The [prebuilt release](https://github.com/johncranen-ux/sdrsharp-stt-plugin/releases/latest)
> contains the compiled plugin and the whole server tree. Unpack it, copy
> `Plugins\SttPlugin\` into your SDR# folder, and jump to step 5 — steps 2 and 3 exist only
> for building from source.

### 2. Find your SDR# SDK

The plugin compiles against two DLLs from your SDR# install, `SDRSharp.Common.dll` and
`SDRSharp.Radio.dll`. They are proprietary and are not redistributed here, so you supply them
from your own copy of SDR#. They live in the SDK download's `sdrplugins\lib\`.

You do **not** edit the project file. Pass the directory on the command line instead — the
`.csproj` is tracked, and a local path edited into it is a change you can commit by accident.

### 3. Build the plugin

```bash
dotnet build SDRSharp.SttPlugin/SDRSharp.SttPlugin.csproj -c Release -p:SDRSharpSdkPath=C:\SDR\SdrSharpSDK\sdrplugins\lib
```

If the path is wrong the build stops with a message naming the flag, rather than a cascade of
"type or namespace not found". Omit `-p:SDRSharpSdkPath` and it falls back to the original
author's layout, which will almost certainly not be yours.

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

This file matters for the batch-file fallback below, and it is what `server/config.json` gets
imported from the first time the panel is set up. If you are starting with the panel on a
fresh install, you can skip straight to [Starting the station](#starting-the-station) and
enter these same keys in the **Settings** screen instead — see [Settings](#settings). Once
`config.json` exists, editing `start-all.bat` no longer changes what the panel-managed proxy
runs with; the two are independent from that point on.

---

## Starting the station

### With the control panel

```bash
cd server
py -m webapp
```

This prints where it is listening, by default `http://127.0.0.1:8787`:

```
[control panel] http://127.0.0.1:8787
```

Open that address in a browser on the same machine.

**First run, no password set.** The panel refuses to sign anyone in until a password exists —
it starts processes and holds every API key you configure, so an unauthenticated instance is
not a safe default. The page tells you what to run:

```bash
cd server
py -m webapp.set_password
```

It asks for the password twice (not echoed to the screen) and rejects anything under 12
characters. The hash goes in `server/credentials.json` — never the plaintext, never
`config.json`. Reload the page once it reports success and sign in.

**Binding beyond this machine.** `WEBAPP_BIND_HOST` defaults to `127.0.0.1`, reachable only
from the machine running the panel. Widen it (e.g. to `0.0.0.0`, to reach the panel from a
phone on the LAN before the planned miniPC move) and the panel will not start without a
password already set — it fails fast, at startup, with no listening socket ever opened:

```
[control panel] WEBAPP_BIND_HOST is '0.0.0.0', which is reachable from the network, and no
password is set. Run `py -m webapp.set_password` from the server directory, or set
WEBAPP_BIND_HOST back to 127.0.0.1.
```

Loopback addresses (`127.0.0.1`, `localhost`, `::1`) are exempt — a password is still a good
idea there, but nothing on the network can reach an unset one.

**Starting the proxy and, if you have a local AIS receiver, the counter.** Once signed in,
the Dashboard's **Processes** section has a card per managed process — the Whisper proxy and
the AIS station counter. Press **Start** on each you need. The panel builds that process's
command line and environment directly from `config.json` (nothing here shells out to
`start-all.bat` — that was tried and does not work headless: `start` needs an interactive
window station a detached process does not have) and clears the port first if something the
panel itself previously started is still holding it. If a *different* process holds the port,
the start is refused rather than that process being killed — see
[Troubleshooting](#troubleshooting).

Then start SDR#, tune to a voice channel, open the **Speech to Text** panel and tick
**Enable transcription** — see [Using the plugin](#using-the-plugin) and
[Reading the Dashboard](#reading-the-dashboard) for what to check next.

**Stopping.** Press **Stop** on a process's card. Closing the panel itself (Ctrl+C, or the
terminal window) does **not** stop the proxy or the counter — each runs as its own detached
process precisely so a panel restart does not interrupt transcription — so stop them from the
Dashboard first if you want the station fully down.

### Running without the panel

For a headless or scripted start, or before a password is set up:

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

`start-all.bat` does not start the AIS station counter — there is no batch-file equivalent of
that card. Run it by hand alongside the proxy if you need it; see
[Measuring what your AIS receiver can hear](#measuring-what-your-ais-receiver-can-hear).

`start-all.bat` and `server/config.json` are two independent sources of the same settings.
The batch file sets them as real environment variables in its own console session; the panel
reads them from `config.json` and hands each managed child an environment it built itself.
Editing one never updates the other — if you use both across different sessions, keep them in
sync by hand, or settle on one.

---

## Reading the Dashboard

The Dashboard is the panel's home tab. Everything on it is read live, polled every few
seconds — nothing here needs a manual refresh.

### The watch

The large reading at the top is time since the last audio chunk reached the proxy. It exists
to answer one question at a glance: is SDR# actually feeding this thing.

| Reading | Meaning |
|---|---|
| **never** | No chunk has arrived since the proxy started. Note reads *"is the play button pressed?"* — a plugin that is enabled but whose SDR# play button is unpressed produces exactly this. |
| **`<n> ago`**, green dot | Under 15 minutes old — audio is arriving. |
| **`<n> ago`**, amber/stale dot | 15 minutes or more old. Note reads *"a silent channel and an unpressed play button look alike"* — past that point the watch genuinely cannot tell a quiet band from SDR# having stopped sending, and says so rather than guessing. |
| **no reading**, red dot | The proxy itself is not answering — a different failure from silence: the source is gone, not quiet. |

Below the watch, five gauges repeat the proxy's own numbers: STT backend, AIS source, vessels
cached, conversations stored, and how many of the configured paths resolve on this machine.

### Feed lamps

The **Feeds** panel answers a question the process cards below it cannot: not just *is the
process running*, but *is data actually arriving*. The two are different facts — aisstream
demonstrated the gap for roughly eight days in 2026-08 (2026-08-05 to 08-13): the proxy was
up, every card was green, and no vessel data had arrived since the 5th. Each lamp reads its
own source's record of the second fact, independent of whether the panel thinks the owning
process is alive.

There are two lamps today: **AIS station** (the local receiver, via the counter) and
**AISHub** (the polled vessel feed, via the proxy).

| Lamp | Meaning |
|---|---|
| Green | Delivering normally. |
| Amber | The owning process is up, but something is off short of a clear failure — a poll is overdue, or AIS-catcher itself is not connected to the counter. Not yet worth restarting anything. |
| Red | Actively failing — the counter's heartbeat has stopped, or AISHub has returned consecutive failed polls. |
| Unlit | **The lamp's own process is not running.** A feed nobody asked for is not the same as a feed that failed — an operator who stopped the counter on purpose sees an unlit lamp, not a false alarm. |

**Lamp test.** The button above the lamps cycles every one through green, amber and red (600
ms per colour) and then back to its real state. It exists for the same reason a real bridge
panel carries one: an unlit lamp is only reassuring if you can prove the bulb still works —
otherwise "nothing is wrong" and "this stopped reporting weeks ago" look identical.

### Process cards

One card per managed process — currently the Whisper proxy and the AIS station counter. Each
shows its state (running / stopped / disabled), uptime, pid, the port it should be on, and
whether it is actually holding that port — a running process that lost its port to something
else shows **Holding port: no**, which is worth investigating even though the process itself
looks fine. **Start**, **Stop** and **Restart** act on that process only; a disabled process
(switched off in Settings) cannot be started until re-enabled there.

### The Logs popup

Each card's **Log** button opens a popup over the page with that process's log tail: today's
output, a **Follow** checkbox that sticks to the bottom as new lines arrive (turns itself off
the moment you scroll up, so reading old lines never gets yanked away), and a substring filter.
A full-page equivalent lives under the **Logs** tab and keeps its own independent read
position, so opening a card's popup never disturbs whatever the Logs tab is showing, and vice
versa.

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

## Conversations and Vessels

The panel's **Conversations** and **Vessels** tabs read the same store the proxy has always
written — they fetch `/api/conversations` and `/api/ais-cache` from the proxy, cache the
result (conversations for 15 s, the vessel cache, up to 1.8 MB, for 60 s), and page it before
it reaches the browser. If the proxy is slow or down, the screen keeps showing its last good
copy with a banner naming how old it is and why — never an empty table, and never a hang.

### Conversations

Each row is one radio exchange, filterable by identified/unidentified, channel, or free text
across vessel, callsign and transcript. Opening a row shows every turn as a three-layer chain:

- **`raw`** — what the decoder actually output.
- **`text`** — after the regex domain corrections (`draft` → `draught` and the rest). This is
  the live transcript, what you would have seen at the time.
- **`conv`** — after the retrospective correction pass re-reads the turn against the rest of
  its exchange. Shown as **`conv: unchanged`** when there is nothing to display here — and
  that phrase covers two different situations the store cannot tell apart: the pass ran and
  genuinely found nothing to change, *or* the pass failed outright (a timeout, a bad
  response). An absent `conv` is not proof the correction ran successfully.

A turn can also carry a live vessel guess independent of whatever the conversation was
eventually resolved to: **`ais-confirmed`** means that guess matched a real MMSI in the AIS
cache; **`heard-only`** means a name was heard but no such ship exists in the cache — a weaker
signal, worth noticing but not to be trusted the way `ais-confirmed` is.

Below the turns, **Resolver candidates** lists every vessel the identification pass was
actually offered for this conversation — by name hint, a live match carried over, callsign, or
partial callsign, each tagged with the route that surfaced it. This is the pass's working set,
not a verdict; it exists so you can see what evidence *was* available even on a conversation
that ended up unidentified.

On a conversation nobody was identified in, a block headed **"Scored below the identification
cutoff"** may list a few names that came close but didn't clear the threshold. That heading is
deliberate: this is **not** an identification, and the system never acts on it — it is here
because you can sometimes settle by ear what an edit-distance score cannot. Measured on
hand-labelled traffic, the right ship is in that list about a quarter of the time (9 of 35) —
an **upper bound**, since the pool measured against was a frozen cache broader than what was
actually live when each conversation resolved, and the real figure cannot be recovered after
the fact. The rest are near-misses worth a glance and nothing more. Turn it off with
`AIS_SUGGEST=off` if it's more noise than help.

The panel's chain view tells you *that* a turn's `conv` differs from its `text`, but not *why*
— no substitution reasons here. For that, the proxy still serves its own page directly and
independently of the panel, needing no login:

**With an Anthropic key set, open http://localhost:9000/conversations.**

Two things on that page show a correction the panel's chain view does not spell out:

- **A green `N corrected` pill** in the conversation header, next to the confidence badge.
  Its absence means nothing in that exchange was changed.
- **A dotted green underline** under the text of each turn that was changed. **Hover** it to
  see what was actually heard, as `was: <original>`, followed by every substitution and its
  reason — for example `Sarbertside -> Starboard side (garbled readback of the instruction in
  turn 7)`.

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

Also available directly from the proxy:

| URL | What |
|---|---|
| `/conversations` | Resolved exchanges (auto-refreshes) |
| `/identified-vessels` | Per-transmission identification log |
| `/api/conversations` | The same data as JSON |
| `/api/ais-cache` | Current AIS vessel cache |

### The conversation archive

`conversations.json` was never meant as long-term storage: it is rewritten whole on every
resolve and keeps only the newest `CONVERSATIONS_KEEP` conversations (default 300), which is
still what drives the Conversations screen above. Everything older used to be gone the moment
it aged out — no backup, no recovery. `server/stt_proxy/conversations.db` fixes that: it is
**append-only and never truncated**, so it holds every conversation ever resolved, for as long
as you keep the file. Comments (below) live in the same file, in their own table.

Because of that, `CONVERSATIONS_KEEP` (Settings screen, **Identification** group) changed
meaning: it used to decide what was *kept* and what was destroyed, and now it only decides how
much the Conversations screen shows. Nothing is lost at any value. **Lowering it is free and
makes the screen lighter** — the panel re-fetches the whole list every 15 seconds at roughly
3.3 KB per conversation, and the proxy rewrites the whole file on every resolve, so raising it
costs more than it appears to. It takes effect on the next proxy restart, like every setting
here.

It sits beside `conversations.json`, one file per install unless you point `CONVERSATIONS_DB`
(Settings screen, **Paths** group) somewhere else; empty means the default location. The proxy
writes conversations there and the panel writes comments there — two processes, two tables,
one file, safe together under SQLite's WAL mode.

**Backing it up safely means using `VACUUM INTO`, not a plain file copy.** WAL mode keeps
`conversations.db` alongside `conversations.db-wal` (uncommitted writes) and
`conversations.db-shm` (rebuildable shared memory, and should never be copied at all). Copying
`.db` and then `-wal` while the proxy or the panel is mid-commit can land the pair
inconsistent with each other — a torn backup that looks fine until you try to restore it. A
plain file copy of all three is only valid with both processes **stopped**. While either is
running, take a consistent-by-construction snapshot instead, from `server/`:

```bash
cd server
py -c "import sqlite3; sqlite3.connect('stt_proxy/conversations.db').execute(\"VACUUM INTO 'conversations.db.backup'\")"
```

This produces one self-contained file — no `-wal` or `-shm` companion needed — that is
guaranteed consistent regardless of what the proxy or panel is doing at the moment it runs.
Naming the output `conversations.db.<something>` keeps it covered by the existing
`conversations.db*` gitignore pattern; a different name needs its own gitignore entry.

**Recovering an old `conversations.json` backup into the archive:**

```bash
cd server
py conversation_archive.py --import <file>...
```

Pass files newest-first — on a conversation both files contain, the first one listed wins. The
import is idempotent (it skips anything already archived), so it's safe to run again whenever
another old backup turns up; a repeat run against files already imported inserts nothing.

**Import before starting the proxy, not after.** If you restore a `conversations.json` holding
more than `CONVERSATIONS_KEEP` records, starting the proxy first will not rescue them: it
truncates the file to the newest `CONVERSATIONS_KEEP` on load and only archives what survived
that cut, so the excess is dropped without ever reaching the database. Run the import command
above first, then start the proxy. (In normal running this cannot bite you — the proxy archives
each conversation as it resolves, and re-archives whatever it loads at every startup, so the
archive stays ahead of the window on its own.)

This is how the archive was first populated, on 2026-08-24: 600 records read across
`conversations.json` and a 2026-08-18 backup, 519 new, 81 duplicates. At that point the archive
spanned 2026-08-07 10:40:14 through 2026-08-24 13:53:09, while the live window — then 300
conversations — only reached back to 2026-08-13 19:55:25. That gap was 219 conversations which,
by then, existed nowhere else.

**Recording a comment.** Open a conversation's detail view (Conversations screen) to find
**Real vessel** and **Note** fields below the turns. Note is free text. Real vessel is the
verdict: type to search the AIS cache and click a match to fill in its MMSI, type `-` if no
vessel on the channel is identifiable, or leave it blank if you're only leaving a note without
a verdict yet. **Prefer the MMSI over typing a name** — the search does this for you when you
click a match. A bare name shared by two vessels resolves arbitrarily wherever it's used
downstream, which has already cost this project about seven points of measured identification
accuracy. Clearing both fields deletes the comment.

**Ground-truth export.** `GET /api/labels` on the panel (sign-in required, like everything
else there) returns every *reviewed* comment — one with a verdict recorded, not just a note —
as a plain-text file in the format `server/bench_identify.py` reads:
`<start><TAB><end><TAB><vessel, MMSI, or -><TAB><note>`. Add `?day=YYYY-MM-DD` to export one
day only. A comment with a note but no verdict is left out entirely rather than exported with
an empty verdict field — an absent line means "not reviewed yet", not "nobody identifiable";
that's what the `-` verdict is for.

### Vessels

Search the whole AIS cache by name, MMSI, callsign or destination as free text. Each row's
**last seen** is that vessel's own most recent position report from the feed, rewritten every
time it is heard again — **never** means the field is genuinely absent, which cannot be
backfilled and happens for any entry written before `last_seen` existed (2026-08-06). A
missing `last_seen` is treated as unknown age everywhere else in the system too, never as
recent.

A row whose name is carried by more than one MMSI is marked **shared name**. That is a
warning, not a defect: a shared name is exactly what makes a bare name unsafe to treat as an
identification — it is why a conversation is never labelled by name alone (see
[Conversations](#conversations) above). Click a vessel to see every
conversation matched to that MMSI — matched by MMSI only, never by name, for the same reason.

---

## Settings

`server/config.json` is what the panel, the proxy and the counter actually read their
settings from once you're running through the panel — not `start-all.bat`. It was imported
once, from the values `start-all.bat` held at the time; after that the two files are
independent (see [Running without the panel](#running-without-the-panel)), and only
`config.json` is what the Settings screen edits.

The form is grouped the same way the reference below is grouped, and every field carries its
description inline. Two things about it are load-bearing rather than incidental:

- **Secrets can be set but never read back.** The six API keys are only ever reported as
  *set* or *not set* — never their value, not in the form, not in any response body. An empty
  box on save therefore means "leave this alone," not "clear it": the browser has no way to
  show a value it was never given, so an empty field cannot be trusted to mean the operator
  wants it gone. Clearing a secret needs the explicit **Clear** control next to the field,
  which stages a sentinel the server reads as "remove this key."
- **Some changes only take effect on that process's next start.** A setting is read once, at
  process startup — `AIS_STATION_*` only by the counter, `WEBAPP_*` only by the panel itself,
  everything else the proxy pulls into its own environment when it starts. Saving tells you
  exactly which processes need restarting; restart them from the Dashboard.

Key names have not changed from before the panel existed — the reference below is still
authoritative.

---

## Settings reference

All are environment variables. Set them from the panel's Settings screen (stored in
`server/config.json`), or, without the panel, in `start-all.bat` — see
[Settings](#settings) and [Running without the panel](#running-without-the-panel).

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

| value | needs | notes |
|---|---|---|
| `aisstream` **(default)** | `AISSTREAM_API_KEY` | A free key from [aisstream.io](https://aisstream.io/), available to anyone. A websocket feed delivering continuously. **No extra hardware.** |
| `aishub` | `AISHUB_USERNAME` | Polled every 15 minutes. Better data — see below — but AISHub issues credentials **only to stations contributing an AIS feed**. |
| `off` | nothing | No vessel enrichment at all. See [Running without AIS](#running-without-ais). |

Set the key from the panel's **Settings** screen — it is stored in `server/config.json`, which
is gitignored. **Never put it in a tracked file.** Editing `start-all.bat` has no effect on a
proxy the panel is managing; see [Settings](#settings) and
[Running without the panel](#running-without-the-panel).

Without a key for the selected source the proxy still starts and transcribes. It prints
`AIS feed: disabled (AIS_SOURCE=aisstream but AISSTREAM_API_KEY is unset)` and runs without
vessel enrichment.

#### Which one should you use

**Start with `aisstream`.** It is the default because its key is free and unconditional.

**Switch to `aishub` if you run your own AIS receiver.** AISHub is a crowd-sourced network:
API credentials go to stations that feed data *in*, and the bar is at least 10 vessels and 90%
uptime, both averaged over 7 days. Signing up is not enough. If you do meet it, AISHub is the
better source — it carries an explicit observation time per vessel, so `last_seen` is true to
when the position was actually reported rather than to when this software heard about it, and
a 15-minute poll costs far less than holding a socket open.

To switch, set **AIS source → `aishub`** on the Settings screen and fill in `AISHUB_USERNAME`,
then restart the proxy from the Dashboard. Nothing else changes: both sources merge into the
same vessel cache through the same code path, so identification behaves identically either
way, and switching back is the same two fields.

#### Bounding boxes

Both sources take `latmin,latmax,lonmin,lonmax` and both default to the **sea box**,
`51.4,52.6,2.0,4.25`:

| setting | applies to |
|---|---|
| `AIS_BBOX` | the `aisstream` subscription |
| `AISHUB_BBOX` | the `aishub` poll |

The eastern edge is the load-bearing number. Maas Approach works ships at sea entering or
waiting to enter, never river traffic already inside, so stopping at 4.25 keeps out the
Rhine/Maas inland barge network. The old wide box (`51.0,53.2,2.0,6.0`) carried 8,381 vessels
with 685 duplicate-name groups against this box's 1,537 and 43 — a 94% cut in exactly the name
collisions that cause misidentification.

`aisstream`'s box was hardcoded to the wide one until 2026-08-25 and could not be changed.
If you are upgrading from an older checkout and had grown used to seeing inland vessels, that
is why they are gone.

Also: `AISHUB_POLL_SEC` (default 900; values under 60 are raised to 60, because AISHub answers
a faster caller with no data at all) and `AIS_SILENCE_WARN_SEC` (default 60) — the latter warns
when a *connected* aisstream socket stops delivering, which is otherwise indistinguishable
from a quiet channel. Set it to 0 only to silence a known outage, and put it back afterwards.

### Running without AIS

Set `AIS_SOURCE=off`, or simply leave the key unset. Everything that does not depend on
knowing which ships are nearby keeps working:

**You keep:**

- All transcription, including the domain corrections and the conversation-level correction
  pass.
- **Vessel names that were actually spoken.** Names are extracted from the audio, not from
  AIS — and by design AIS may only correct the *spelling* of a name someone said, never
  supply one. So "this is MSC Athens" still yields `MSC Athens`.
- Conversation grouping, the archive, comments and the whole control panel.

**You lose:**

- The MMSI, ship type, length, draught and destination.
- The VesselFinder links.
- Callsign-to-vessel lookup, and the corroboration that makes a partly-garbled callsign
  usable.
- The confirmation that the named ship is really in the area — an unmatched name is reported
  as heard, with nothing standing behind it.
- The **Vessels** screen, and the "possible matches" block under unidentified conversations.

The accuracy figures quoted for identification elsewhere in these docs are all measured *with*
an AIS source and do not describe this configuration.

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
correct ship is in the three 9 times out of 35, an **upper bound**: the pool was a frozen
cache broader than what was actually live when each conversation resolved, and the real
figure cannot be recovered after the fact. The other three-quarters are near-misses worth a
glance and nothing more. The block never appears beside a conversation that *was* identified,
and never in the first 30 conversations after a fresh start, because it needs a corpus to
learn which words are the shore station rather than a ship.

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
| `AIS_SILENCE_WARN_SEC` | `60` | Warns when a *connected* AIS feed stops delivering — the failure that otherwise looks identical to a quiet channel. Muted to `0` between 2026-08-11 and 08-25, during an aisstream outage in which it fired every 60 s to no purpose; **restored to `60` once the feed was measured delivering again**, since it is the only thing that catches a relapse. Set it to `0` only for a known outage, and put it back |

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

**Panel refuses to start: `WEBAPP_BIND_HOST is '...', which is reachable from the network, and no password is set`**
Deliberate — see [Starting the station](#starting-the-station). Either run
`py -m webapp.set_password` from `server`, or set `WEBAPP_BIND_HOST` back to `127.0.0.1`.
No socket is ever opened when this fires, so nothing needs to be undone.

**The counter's timestamps look two hours behind the proxy's**
They are two different clocks and both are correct. The counter writes **UTC** and says so —
`2026-08-19T19:19:15+00:00`, `hour=2026-08-19T19Z` — because its hourly buckets have to line up
with AISHub's own accounting and with the timestamps inside AIS messages. The proxy writes
**your local time**, with no zone marker and no date: `[21:19:15] CH01: vessel=...`. In summer
that is CEST, UTC+2, so one moment appears as 19:19 in one log and 21:19 in the other.

This matters whenever you line a transcription up against a vessel count: apply the offset
rather than assuming the two logs share a clock. Note also that the dated log filenames
(`proxy-2026-08-19.log`, `counter-2026-08-19.log`) are named from the **local** date, so a file
named for one day can hold counter records belonging to the next UTC day — between local
midnight and 02:00 in summer, that is exactly what happens.

**A process card's Start fails with `port N is held by pid P (image), which is not one of ours`**
Something the panel did not start is already listening on that port. The panel clears a port
before starting only when the current holder is a process it recognises as its own kind — a
port held by anything else is reported, never killed, so you can decide what to do with it
yourself. Free the port (or change the port setting) and Start again.

**The panel looks stale or a button does nothing after an upgrade**
Hard-reload the tab (Ctrl+F5 or Ctrl+Shift+R). `/static` is served with `Cache-Control:
no-cache`, so a fresh page load always revalidates its script — but a tab left open across an
upgrade is still running the `app.js` it loaded when it was opened, and only a reload replaces
it. This is exactly what a dead button after a deploy usually means; it happened once already,
on 2026-08-19.

**A feed lamp is red, but I stopped that feed's process on purpose**
It shouldn't be — a lamp is **unlit**, not red, whenever its owning process is not running (see
[Reading the Dashboard](#reading-the-dashboard)). If you see red next to a process the
Processes panel shows as stopped, that combination is itself the bug worth reporting; the
lamp's colour should only ever speak to a process that is actually up.

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
Check the Conversations tab, or the proxy's own `/conversations` page — the retrospective
pass often corrects what the live transcript guessed. If names are still poor, the
`Initial prompt` is the biggest lever.

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
