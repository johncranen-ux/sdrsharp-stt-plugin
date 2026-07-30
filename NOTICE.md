# Third-party components and services

This project is MIT licensed (see `LICENSE`). It does not bundle or redistribute any of
the following — each is installed or subscribed to by the user — but it depends on them,
so their terms apply to your use.

## Required at build time

| Component | Role | Licence / terms |
|---|---|---|
| [SDR#](https://airspy.com/download/) SDK (`SDRSharp.Common.dll`, `SDRSharp.Radio.dll`) | Plugin host API the plugin compiles against | Proprietary, © Airspy. **Not redistributed here.** You must obtain SDR# yourself; the `.csproj` references the DLLs from your local install. |
| [.NET 8/9 SDK](https://dotnet.microsoft.com/) | Builds the plugin | MIT |

## Required at run time (choose a speech-to-text backend)

| Component | Role | Licence / terms |
|---|---|---|
| [Groq API](https://groq.com/) | Hosted `whisper-large-v3` — the default backend | Commercial service, free tier available. Your own API key, subject to Groq's terms. |
| [whisper.cpp](https://github.com/ggml-org/whisper.cpp) | Local GPU backend (optional alternative) | MIT, © Georgi Gerganov |
| [OpenAI Whisper models](https://github.com/openai/whisper) | The `ggml-large-v3` weights whisper.cpp loads | MIT |
| [Silero VAD](https://github.com/snakers4/silero-vad) | Optional server-side VAD model for whisper.cpp | MIT |

## Optional services (maritime enrichment)

| Component | Role | Licence / terms |
|---|---|---|
| [Anthropic API](https://www.anthropic.com/) | Vessel-name extraction and conversation resolution (Claude Haiku) | Commercial service. Your own API key. Disable by leaving `ANTHROPIC_API_KEY` unset. |
| [aisstream.io](https://aisstream.io/) | Live AIS vessel positions for name matching | Free API key. Disable by leaving `AISSTREAM_API_KEY` unset. |

## Python dependencies

Declared in `server/requirements.txt`:

| Package | Licence |
|---|---|
| [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) | MIT |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | MIT |
| [websockets](https://github.com/python-websockets/websockets) | BSD-3-Clause |
| [certifi](https://github.com/certifi/python-certifi) | MPL-2.0 |
| [pip-system-certs](https://gitlab.com/alelec/pip-system-certs) | BSD-3-Clause |

## .NET test dependencies

xUnit (Apache-2.0), Microsoft.NET.Test.Sdk (MIT), coverlet.collector (MIT).

## A note on radio content

This software transcribes radio transmissions you receive. Receiving is generally lawful;
**recording, publishing or otherwise divulging the content of communications not addressed
to you may not be**, and the rules differ by country (in the Netherlands see
Telecommunicatiewet art. 18.13; internationally see ITU Radio Regulations 17.3).

The repository therefore ships no transcripts at all. If you enable the capture features,
the resulting audio and transcripts are yours to handle — check what your jurisdiction
allows before sharing them.
