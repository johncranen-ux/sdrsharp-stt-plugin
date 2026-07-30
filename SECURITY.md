# Security

## Reporting a vulnerability

Please report security issues privately via
[GitHub's security advisory form](https://github.com/johncranen-ux/sdrsharp-stt-plugin/security/advisories/new)
rather than opening a public issue.

This is a hobby project maintained in spare time — expect a response in days rather than
hours.

## How credentials are handled

The project uses up to three third-party API keys: Groq (required), Anthropic and
aisstream.io (both optional).

**Keys live in `server/start-all.bat`, which is gitignored.** The tracked file is
`start-all.bat.template`, containing placeholders only. Nothing in this repository holds a
real key, and no key is ever sent anywhere except to the service it belongs to.

If you fork or contribute:

- Never commit `start-all.bat`. The `.gitignore` prevents it, but check before pushing.
- Do not paste keys into issues, pull requests or logs. The proxy prints
  `Anthropic API key: OK` rather than the value, deliberately.
- If you leak a key, **rotate it**. Deleting the commit does not help — assume anything
  pushed has been scraped.

## What the software sends where

| Destination | What is sent | When |
|---|---|---|
| Groq | 16 kHz mono audio of detected speech | Every chunk, when `STT_BACKEND=groq` |
| Anthropic | Transcribed text (not audio) | Maritime mode, for vessel identification |
| aisstream.io | Nothing outbound — a subscription to a position feed | Continuously, when a key is set |
| Local whisper.cpp | Audio, on your own machine | When `STT_BACKEND=whisper_cpp` |

**Audio leaves your machine only with the cloud backend.** Set `STT_BACKEND=whisper_cpp` for
a fully local pipeline, and leave `ANTHROPIC_API_KEY` and `AISSTREAM_API_KEY` unset to
disable everything else. The proxy then makes no outbound connections at all.

## Network exposure

The proxy binds `0.0.0.0:9000` so the plugin can reach it. It has **no authentication** and
is intended for `localhost` only.

If your machine is on an untrusted network, firewall port 9000. Anyone who can reach it can
submit audio for transcription against your API keys, and read the transcripts and vessel
data it has stored.

## Received radio content

Transcripts of received traffic are personal data about identifiable vessels and crews, and
publishing them is restricted in many jurisdictions — see the
[legal note](docs/user-manual.md#legal-note). Capture features are off by default and this
repository ships only synthetic samples. What you record with them is your responsibility.
