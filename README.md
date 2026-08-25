# SDR# Speech-to-Text Plugin

Live speech-to-text for [SDR#](https://airspy.com/download/), with vessel identification for
maritime VHF.

It listens to whatever SDR# is tuned to, detects speech, transcribes it, and shows the text
in a panel inside SDR#. On Rotterdam maritime channels it will also work out which ship is
talking and match it against live AIS data.

Built and tuned against real Rotterdam / Maas Approach traffic, but the transcription side
works on any voice channel.

```
[11:04:26] [MSC ATHENS/container] (MMSI:636022873) Maas Approach, Maas Approach,
           this is MSC Athens, Callsign five Lima Kilo Victor Five.
[11:04:31] Hello sir, I am passing now the Mike Whiskey pipe.
[11:04:39] Okay, thank you.
```

## Built entirely by AI

Every line of this repository — the plugin, the proxy, the control panel, the tests and this
documentation — was written by [Claude Code](https://claude.com/claude-code). The project
exists as an experiment in what an AI coding agent can carry on its own over months: not a
demo, but a working system that runs continuously against live radio traffic, with more than
270 commits, 1,271 tests, and a design-notes file recording what was measured and rejected as
well as what shipped.

The human role was direction and judgement — what to build, what "better" meant, and whether
a result was believable. That last one mattered most: several times the honest answer was
that a change did nothing, or that a number everyone trusted was measuring the wrong thing.
The [release history](docs/release-history.md) traces how it got here, including the results
that did not survive being checked.

## Features

- **Voice activity detection in the plugin** — silence and static are never sent, so usage
  tracks how much talking there actually is
- **Audio conditioning** tuned for noisy VHF: DC block, high-pass, anti-aliased resample,
  normalisation
- **Two interchangeable backends** — Groq's hosted Whisper (default, **no GPU required**) or
  a local whisper.cpp server, switched by one environment variable
- **Domain corrections** for terms the decoder reliably mishears
- **Conversation-level correction** — a transmission is re-read with the surrounding
  exchange as context, so a name mangled in one turn is fixed from a clearer turn nearby
- **Web control panel** — run the whole station from a browser, including from a phone on
  the LAN: settings, process supervision, live feed status, logs, conversations and vessels
- **Conversation archive** — every resolved exchange kept in SQLite beyond the rolling
  display window, with operator comments and verdicts that export as benchmark ground truth
- **Benchmark and replay tooling** for measuring accuracy on your own recordings
- **1,271 tests** covering the DSP, the proxy pipeline, the identification logic and the panel

With an AIS source configured, additionally:

- **Vessel identification** from speech, matched against live AIS for MMSI and type, and
  linked out to VesselFinder
- **Retrospective conversation resolution** — exchanges are identified after they end, so a
  garbled opening call is resolved by a clearer later turn or a spelled-out callsign
- **Partial-callsign matching** — a callsign half-lost to STT still identifies the ship when
  the surviving characters fit exactly one vessel and a spoken name agrees

## Getting started

**[→ Read the user manual](docs/user-manual.md)** for install, configuration and usage.

### The short version

**Install the plugin.** Either grab the
[latest release](https://github.com/johncranen-ux/sdrsharp-stt-plugin/releases/latest) and
copy `Plugins\SttPlugin\` into your SDR# folder — no build, no SDK — or build it yourself:

```bash
git clone https://github.com/johncranen-ux/sdrsharp-stt-plugin.git
cd sdrsharp-stt-plugin
dotnet build SDRSharp.SttPlugin/SDRSharp.SttPlugin.csproj -c Release \
       -p:SDRSharpSdkPath=C:\SDR\SdrSharpSDK\sdrplugins\lib
# copy bin\Release\...\SDRSharp.SttPlugin.dll into <SDRSharp>\Plugins\SttPlugin\
```

**Then start the server:**

```bash
py -m pip install -r server/requirements.txt
cd server
py -m webapp.set_password     # first run only
py -m webapp                  # http://127.0.0.1:8787
```

Sign in, enter your Groq API key on the Settings screen, press **Start** on the Whisper proxy
card. Then enable the **Speech to Text** panel in SDR#.

## How it works

```
  SDR#  ──audio──▶  Plugin (C#)  ──HTTP──▶  Proxy (Python)  ──▶  Groq  or  whisper.cpp
                    VAD, filtering              corrections,        (cloud)     (local GPU)
                    chunking                    vessel ID,
                         ▲                      AIS matching
                         └────── text ──────────────┘

                                      ▲
                    Control panel ────┘   settings, supervision, logs,
                    (FastAPI, browser)    conversations, vessels
```

The plugin only ever talks to the proxy, so backends and post-processing can change without
rebuilding it. All decoder settings live in the proxy, which means tuning is a restart of a
Python service rather than a plugin rebuild and an SDR# restart.

## Requirements

Windows, SDR#, Python 3.10+, and a Groq API key (free tier is enough). A GPU is **not**
required.

Building the plugin additionally needs the **.NET 9 SDK** and the SDR# SDK DLLs from your own
install — both avoidable by using the [prebuilt
release](https://github.com/johncranen-ux/sdrsharp-stt-plugin/releases/latest). Full list in
the [manual](docs/user-manual.md#requirements).

### Vessel identification needs an AIS source

Transcription works without one. Vessel names spoken on air are still reported, because names
are extracted from the audio rather than from AIS. What an AIS source adds is the MMSI, ship
type, particulars, callsign lookup and the VesselFinder links.

| `AIS_SOURCE` | What it needs | Notes |
|---|---|---|
| `aisstream` *(default)* | A free [aisstream.io](https://aisstream.io/) key | **No extra hardware.** The right choice for most people. |
| `aishub` | An [AISHub](https://www.aishub.net/join-us) username | Better data, but AISHub issues credentials only to stations that **contribute** an AIS feed — meaning your own receiver running 24/7. |
| `off` | Nothing | Transcription only; no conversation is given a vessel. |

Switching is one setting in the panel, or `AIS_SOURCE` in the environment. See
[AIS vessel source](docs/user-manual.md#ais-vessel-source).

## Documentation

| | |
|---|---|
| [User manual](docs/user-manual.md) | Install, configure, run, troubleshoot |
| [Release history](docs/release-history.md) | How the project got here, release by release |
| [Design notes](docs/design-notes.md) | Why it works this way, with the measurements behind each choice |
| [Contributing](CONTRIBUTING.md) | Development setup, tests, and two runtime traps worth knowing |
| [Security](SECURITY.md) | Reporting issues, and how API keys are handled |
| [Notice](NOTICE.md) | Third-party components and their licences |

The design notes are worth a look if you are evaluating approaches — they record what was
tried and rejected as well as what shipped, with numbers. For instance: Groq and local
whisper.cpp measured essentially identical word error rates (0.411 vs 0.416), so the local GPU
path buys privacy rather than accuracy.

## Legal note

Listening is generally lawful; **recording or republishing the content of communications not
addressed to you often is not**, and the rules vary by country (NL: Telecommunicatiewet
art. 18.13; ITU Radio Regulations 17.3).

This repository contains no transcripts of received traffic at all. Capture features are
off by default, and anything you record stays local — `references-*.txt` is gitignored.

## Licence

[MIT](LICENSE). See [NOTICE.md](NOTICE.md) for third-party components — in particular, the
SDR# SDK is proprietary and is not redistributed here, in the repository or in the release
archive; you supply it from your own install.
