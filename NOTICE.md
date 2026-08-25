# Third-party components and services

This project is MIT licensed (see `LICENSE`). It does not bundle or redistribute any of
the following — each is installed or subscribed to by the user — but it depends on them,
so their terms apply to your use.

## Required at build time

| Component | Role | Licence / terms |
|---|---|---|
| [SDR#](https://airspy.com/download/) SDK (`SDRSharp.Common.dll`, `SDRSharp.Radio.dll`) | Plugin host API the plugin compiles against | Proprietary, © Airspy. **Not redistributed here.** You must obtain SDR# yourself; the `.csproj` references the DLLs from your local install. |
| [.NET 9 SDK](https://dotnet.microsoft.com/) | Builds the plugin (it targets `net9.0-windows`; .NET 8 alone will not build it, though SDR# hosts the result on the .NET 8 runtime) | MIT |

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
| [aisstream.io](https://aisstream.io/) | Live AIS vessel positions for name matching — the default source | Free API key, available to anyone. Disable by leaving `AISSTREAM_API_KEY` unset or setting `AIS_SOURCE=off`. |
| [AISHub](https://www.aishub.net/) | Alternative AIS source, polled | Free, but credentials are issued **only to stations that contribute an AIS feed** (a receiver meeting their vessel-count and uptime bar). Selected with `AIS_SOURCE=aishub`. |

## Python dependencies

Declared in `server/requirements.txt`:

| Package | Role | Licence |
|---|---|---|
| [rapidfuzz](https://github.com/rapidfuzz/RapidFuzz) | Fuzzy vessel-name and callsign matching | MIT |
| [anthropic](https://github.com/anthropics/anthropic-sdk-python) | Vessel extraction, conversation resolution and correction | MIT |
| [websockets](https://github.com/python-websockets/websockets) | The aisstream.io feed | BSD-3-Clause |
| [certifi](https://github.com/certifi/python-certifi) | CA bundle for outbound TLS | MPL-2.0 |
| [pip-system-certs](https://gitlab.com/alelec/pip-system-certs) | Windows certificate-store shim | BSD-3-Clause |
| [numpy](https://github.com/numpy/numpy) | DSP and the IQ replay harness | BSD-3-Clause |
| [scipy](https://github.com/scipy/scipy) | Filter design and resampling in the IQ harness | BSD-3-Clause |
| [fastapi](https://github.com/fastapi/fastapi) | The control panel | MIT |
| [uvicorn](https://github.com/encode/uvicorn) (with `[standard]` extras) | ASGI server for the control panel | BSD-3-Clause |
| [pydantic](https://github.com/pydantic/pydantic) | Setting and request validation | MIT |
| [psutil](https://github.com/giampaolo/psutil) | Process supervision and port ownership checks | BSD-3-Clause |
| [argon2-cffi](https://github.com/hynek/argon2-cffi) | argon2id hashing of the panel password | MIT |
| [httpx](https://github.com/encode/httpx) | HTTP client used by the panel | BSD-3-Clause |

`uvicorn[standard]` pulls in further transitive dependencies (`httptools`, `uvloop` where
available, `websockets`, `watchfiles`, `python-dotenv`, `PyYAML`); each carries its own
permissive licence. Run `py -m pip licenses` or check `pip show` for the exact set installed
on your machine.

## .NET test dependencies

xUnit (Apache-2.0), xunit.runner.visualstudio (Apache-2.0), Microsoft.NET.Test.Sdk (MIT),
coverlet.collector (MIT).

## A note on radio content

This software transcribes radio transmissions you receive. Receiving is generally lawful;
**recording, publishing or otherwise divulging the content of communications not addressed
to you may not be**, and the rules differ by country (in the Netherlands see
Telecommunicatiewet art. 18.13; internationally see ITU Radio Regulations 17.3).

The repository therefore ships no transcripts at all, and neither does the release archive —
its contents are taken from the tracked file list precisely so that nothing gitignored can
reach it. If you enable the capture features, the resulting audio and transcripts are yours to
handle — check what your jurisdiction allows before sharing them.
