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

## Features

- **Voice activity detection in the plugin** — silence and static are never sent, so usage
  tracks how much talking there actually is
- **Audio conditioning** tuned for noisy VHF: DC block, high-pass, anti-aliased resample,
  normalisation
- **Two interchangeable backends** — Groq's hosted Whisper (default, **no GPU required**) or
  a local whisper.cpp server, switched by one environment variable
- **Domain corrections** for terms the decoder reliably mishears
- **Vessel identification** from speech, matched against a live AIS feed for MMSI and type,
  and linked out to VesselFinder
- **Retrospective conversation resolution** — exchanges are identified after they end, so a
  garbled opening call is resolved by a clearer later turn or a spelled-out callsign
- **Partial-callsign matching** — a callsign half-lost to STT still identifies the ship when
  the surviving characters fit exactly one vessel and a spoken name agrees
- **Benchmark and replay tooling** for measuring accuracy on your own recordings
- **275 tests** covering the DSP, the proxy pipeline and the identification logic

## Getting started

**[→ Read the user manual](docs/user-manual.md)** for install, configuration and usage.

The short version:

```bash
git clone https://github.com/johncranen-ux/sdrsharp-stt-plugin.git
cd sdrsharp-stt-plugin
py -m pip install -r server/requirements.txt

dotnet build SDRSharp.SttPlugin/SDRSharp.SttPlugin.csproj -c Release
# copy the DLL into <SDRSharp>\Plugins\SttPlugin\  (SDR# finds it automatically)

cd server && copy start-all.bat.template start-all.bat   # add your Groq API key
start-all.bat
```

Then enable the **Speech to Text** panel in SDR#.

## How it works

```
  SDR#  ──audio──▶  Plugin (C#)  ──HTTP──▶  Proxy (Python)  ──▶  Groq  or  whisper.cpp
                    VAD, filtering              corrections,        (cloud)     (local GPU)
                    chunking                    vessel ID,
                         ▲                      AIS matching
                         └────── text ──────────────┘
```

The plugin only ever talks to the proxy, so backends and post-processing can change without
rebuilding it. All decoder settings live in the proxy, which means tuning is a restart of a
Python script rather than a plugin rebuild and an SDR# restart.

## Requirements

Windows, SDR#, Python 3.10+, .NET 8/9 SDK, and a Groq API key (free tier is enough). A GPU
is **not** required. Full list in the [manual](docs/user-manual.md#requirements).

## Documentation

| | |
|---|---|
| [User manual](docs/user-manual.md) | Install, configure, run, troubleshoot |
| [Design notes](docs/design-notes.md) | Why it works this way, with the measurements behind each choice |
| [Contributing](CONTRIBUTING.md) | Development setup, tests, and two runtime traps worth knowing |
| [Security](SECURITY.md) | Reporting issues, and how API keys are handled |
| [Notice](NOTICE.md) | Third-party components and their licences |

The design notes are worth a look if you are evaluating approaches — they record what was
tried and rejected as well as what shipped, with numbers. For instance: Groq and local
whisper.cpp measured identical word error rates, so the local GPU path buys privacy rather
than accuracy.

## Legal note

Listening is generally lawful; **recording or republishing the content of communications not
addressed to you often is not**, and the rules vary by country (NL: Telecommunicatiewet
art. 18.13; ITU Radio Regulations 17.3).

This repository contains no transcripts of received traffic at all. Capture features are
off by default, and anything you record stays local — `references-*.txt` is gitignored.

## Licence

[MIT](LICENSE). See [NOTICE.md](NOTICE.md) for third-party components — in particular, the
SDR# SDK is proprietary and is not redistributed here; you supply it from your own install.
