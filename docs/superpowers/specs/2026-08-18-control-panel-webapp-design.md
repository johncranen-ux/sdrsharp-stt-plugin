# Control panel web app — design

**Status:** agreed. Revised twice on 2026-08-18 — first for a networked miniPC deployment,
then with all six open questions answered. Ready for an implementation plan.

## History of this document

The original design was reached on 2026-08-13 through the brainstorming skill, with the user
answering every question. That session died on a weekly-limit cutoff before the spec was
written; it survived only as a memory note and was written up on 2026-08-18.

**It was then revised the same day**, because the deployment target changed: the intention is
now a low-power miniPC running SDR# and the AIS receiver 24/7, with the control panel reached
over the network — the home LAN first, private external access later. That overturns decisions
the original session took, and the sections below say so explicitly where it does. Anything not
marked as changed still stands as agreed.

## What this is for

> "a good looking web app from where I can start what we have implemented so far (proxy,
> counter, with the possibility to change the env variables/options). Instead of viewing the
> /identified-vessels and /conversations in a browser, I'd like to view them in the web app as
> well"

and now, additionally:

> "a miniPC to run both the AIS and the SDR#. Low in consumption and noise, perfect to run
> 24/7. Making it accessible using the web, so I can start/stop/monitor/configure remotely.
> For now only on my own network, but prepare also for external (private) access. For now the
> server will run on this PC, so make porting to the miniPC easy."

## Decisions still standing from 2026-08-13

| question | decision |
|---|---|
| stack | FastAPI + a no-build frontend: one HTML file, hand-written CSS, vanilla JS, vendored |
| process lifetime | detached — children survive the dashboard closing |
| API keys | stored but masked (●●●● + Replace); never sent to the browser |
| config source of truth | `config.json` + a pydantic `settings_schema.py` |
| restart semantics | every setting requires a restart; no pretending anything applies live |
| UI design | `frontend-design` is pulled in at build time, not during design |

Rejected then, still rejected: pure-stdlib server-rendered; FastAPI + React/Vite.

## Decisions the miniPC overturns

| was | now | why |
|---|---|---|
| **localhost only, no auth, no TLS** | **authenticated from day one; bind address configurable, default loopback** | The app starts and stops processes and holds six API keys. That was acceptable when only a local user could reach it. On a LAN it is not, and "add auth later" means shipping an unauthenticated window. |
| **proxy + counter only; AIS-catcher out of scope** | **AIS-catcher is in scope as a managed process** | It moves onto the same box. The registry was designed for this. |
| **the web app replaces the `start-all.bat` shortcut** | **the web app is a service that starts at boot; there is no shortcut and no local browser** | A headless 24/7 box has nobody to click a shortcut. |
| **one host, paths implicit** | **every path is a setting; nothing hardcodes `D:\SDR\...`** | The whole point is that this moves hosts. |

## Section 0 — deployment model (new)

Two topologies, and the design must serve both without a fork:

**Today.** Everything on this PC: SDR# + plugin, the proxy, the AIS station counter, the web
app. The RX 7900 XTX is here, so local whisper.cpp under WSL is available.

**Target.** The miniPC runs SDR# + plugin, the proxy, the AIS receiver, and the web app. The
operator's laptop or phone runs only a browser.

The move is a **host migration, not a rearchitecture**: the same package, the same
`config.json`, different values for the path settings. What makes that true is Section 2 —
if any path, port or working directory is implicit, the migration turns into a debugging
session.

### STT: cloud, and the local GPU path is kept for others (decided)

The GPU is in *this* PC and a silent miniPC will not run whisper large-v3. **The target
deployment uses Groq**, which is already the default and has proven itself in service. That
severs the miniPC's dependence on this machine entirely — after the move, this PC can be off.

`STT_BACKEND=whisper_cpp` is **not** removed. It stays a fully supported, tested option for
anyone running this repository with their own GPU once it is public, and the settings schema
must present it as a first-class choice rather than a legacy path. What changes is only which
option *this* deployment uses.

### The other consequence: SDR# is a desktop application

SDR# is a Windows GUI app, and the plugin runs inside it. It cannot be supervised the way a
console process can: it needs an interactive desktop session, so `DETACHED_PROCESS` from a
service running as `LocalSystem` will start a process that never renders and may not work.

**Decided: SDR# is monitored, never managed.** Launching the executable is not enough — the
receiver only starts when the *play* button is pressed, and the plugin's checkboxes cannot be
set from outside the GUI. A supervisor that could start the process but not make it receive
would be worse than nothing: the dashboard would report SDR# "running" while no audio flowed.

So the operator starts SDR# by hand, logging in locally or over RDP. The panel's job is to say
plainly whether it is *working*, which is a different question from whether it is running.

**The health signal is chunk arrival, not process liveness.** The proxy already knows when it
last received a transmission; that is what distinguishes "SDR# is up and receiving" from "SDR#
is up with the play button unpressed". The Dashboard shows time-since-last-chunk for exactly
this reason, and a process-alive check alone would be actively misleading.

**RDP hazard worth recording.** Connecting at a different resolution has previously stranded
the plugin's floating panel off-screen — the plugin appears checked in the menu with no visible
frame. Dock the panel rather than leaving it floating. Related: `tasklist` has been observed
reporting stale entries, so any liveness check uses the equivalent of `Get-Process`.

The proxy, the counter and AIS-catcher are console processes and are fully managed as designed.

## Section 1 — architecture and process model

A `server/webapp/` package, uvicorn, and a **process registry rather than hardcoded handlers**.
Each managed process declares how to build its command line from config, its working directory,
pid file, log file, and optionally the port it should be listening on. Proxy, counter and now
AIS-catcher are the entries.

**Every entry carries an `enabled` flag**, because the counter is expected to become
unnecessary once the AIS receiver has proven stable. Disabled means "not started, not shown as
failed" — distinct from stopped, which is a running process the operator turned off. Should the
counter ever fold into the proxy instead, its output goes to **its own log file**, never
interleaved with the proxy's; the proxy log is read to answer questions about transcription and
identification, and per-hour MMSI counts scrolling through it would cost more than the separate
process does.

Three details drawn from failures this project has already had:

- **Detached start.** `DETACHED_PROCESS`, so closing a browser tab — or the dashboard
  restarting — never touches a capture run. On startup the app reattaches by pid file,
  verifying both that the pid is alive **and that the image name matches**; pid reuse would
  otherwise let it adopt an unrelated process.
- **Port-clearing on restart.** Whatever holds `:9000` must be killed before starting, exactly
  as `start-all.bat` does. Python's `allow_reuse_address` lets a second proxy bind alongside
  the first and silently take over while the original runs on as a zombie, so without this
  "Restart" is a quiet no-op.
- **Logs to files, not console windows.** Today the proxy runs under `cmd /k` and its output
  dies with the window. Children get stdout redirected to `server/logs/proxy-YYYY-MM-DD.log`,
  and the UI tails them. On a headless box this stops being a nicety: it is the only record.

**Validated on 2026-08-18.** The proxy ran detached with stdout redirected to a file for a full
day of live traffic and behaved as this section assumes. One constraint was found the hard way:
`start-all.bat` **cannot** be launched from a non-interactive parent, because `start` needs an
interactive window station to create a console. The supervisor must build the command line and
environment itself and spawn the child directly — never by shelling out to the batch file.

## Section 2 — configuration

`config.json` (gitignored) holds **values only**. A pydantic `settings_schema.py` describes each
setting: key, type, default, group, restart-required flag, and description. The schema validates
saves *and* drives form rendering, so `AISHUB_BBOX` is four bounded floats and `AIS_SOURCE` an
enum, each defined once.

**The description field is where the prose comments in `start-all.bat` go** — the sea-box
reasoning, why the east edge is 4.25, the rollback lines, the AISHub rate-limit warning. Those
are some of the best documentation in the project and regenerating the batch file would destroy
them. In the schema they become help text beside the control they explain.

### Scope: the settings `start-all.bat` exposes, and only those (decided)

The proxy reads **65** distinct environment variables. The panel exposes the **22** that
`start-all.bat` already names — 12 active `set` lines plus 10 commented-out rollback switches,
which exist precisely to be turned on and are therefore settable in every sense that matters.
The remaining 43 stay as code defaults.

| group | settings |
|---|---|
| Secrets | `ANTHROPIC_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `AISSTREAM_API_KEY`, `AISSTREAM_API_KEY2`, `AISHUB_USERNAME` |
| STT | `STT_BACKEND`, `GROQ_MODEL`, `WHISPER_BACKEND_PORT` |
| AIS source | `AIS_SOURCE`, `AISHUB_BBOX`, `AISHUB_POLL_SEC`, `AIS_SILENCE_WARN_SEC` |
| Identification | `AIS_HINT_FILTER`, `AIS_NAME_FILTER`, `AIS_PARTIAL_CALLSIGN`, `RESOLVER_LIVE_CANDIDATES`, `CONVERSATION_RESOLVER`, `PROMPT_ECHO_FILTER` |
| Paths and ports | `PROXY_PORT`, plus the new path settings below |

`SCRIPT_DIR` and `PROXY_SCRIPT` are batch-file plumbing, not settings, and are replaced by the
path settings rather than carried across.

**This makes `start-all.bat` the curated operator surface, which is a useful principle to
adopt deliberately:** a setting becomes operator-facing by being added to that file, with the
prose comment explaining it. It keeps one list rather than two, and it means the schema
inherits the file's documentation instead of competing with it.

It also exposes a gap. The flags shipped on 2026-08-18 — `AIS_LIVE_MATCH_MAX_AGE_MIN`,
`AIS_CALLSIGN_SUFFIX_FALLBACK`, `AIS_SUGGEST` and its four companions, `AIS_SUGGEST_TIEBREAK` —
are **absent from `start-all.bat`**, so under this rule they would not appear in the panel. Two
of them are documented as "the first thing to switch off if identification regresses", which is
exactly a control an operator wants. Adding them to the batch file as commented rollback lines,
matching the existing convention, is a small separate change and is listed as a follow-up
below.

**Two settings carry documented footguns and their descriptions must say so**, because the
schema is what the operator reads: `WHISPER_PROMPT` (a plugin-side override cost ~11 WER points
on 2026-08-07) and `AIS_HINT_MIN_SCORE` (relaxing it cost 11 precision points on 2026-08-12).
Neither is in the exposed 22 — which is itself the safest outcome — but if either is ever
promoted, it arrives with its history attached.

### Host-portable by construction (new)

Every filesystem path, port and host becomes a setting with the current value as its default —
the SDR# install, the plugin captures directory, the AIS station host and port, the whisper
backend host and port, the log directory. **Nothing hardcodes a drive letter.** A settings
group named "Paths" exists so that migrating hosts is one screen, and a `GET /api/health`
route reports which of those paths actually resolve on this machine, so a bad migration is
visible immediately rather than at the next transmission.

Migration is a one-time import from `start-all.bat`, with code defaults filling the rest. The
batch file is kept read-only as a fallback rather than regenerated, so nothing silently drifts.

## Section 3 — security (new, and now load-bearing)

The app can start and stop processes and holds `ANTHROPIC_API_KEY`, `GROQ_API_KEY`,
`OPENROUTER_API_KEY`, `AISSTREAM_API_KEY`, `AISSTREAM_API_KEY2` and `AISHUB_USERNAME`. Reachable
from a network, it is the most sensitive thing in the project.

- **Authentication from the first commit.** Single operator, password-based, hashed with
  argon2/bcrypt, session cookie marked `HttpOnly`, `SameSite=Strict`, and `Secure` whenever the
  request arrived over TLS. No "we'll add it before exposing it" — that is how an
  unauthenticated window ships.
- **Bind address is a setting, default `127.0.0.1`.** Widening it is a deliberate act. On
  startup, binding to anything other than loopback **without** a password configured must
  refuse to start and say why.
- **CSRF protection** on every mutating route, because the browser is now on a different
  machine and cookies travel.
- **Secrets never leave the server.** Masked in the UI, absent from API responses, absent from
  logs and error text. A "Replace" control writes a new value; nothing ever reads one back.
- **Rate-limit authentication attempts**, so a LAN-reachable login is not a free oracle.

### External access: terminate it outside this app

**Decided: Tailscale.** External access is terminated outside this app, on a private overlay
network. The app is never put directly on the public internet, and there is no public listener
in it to maintain.

"Prepare for external access" therefore means **make nothing assume localhost**: a configurable
bind address, correct cookie flags, no mixed content, no hardcoded `127.0.0.1` in the frontend,
and honouring `X-Forwarded-Proto` so it sits correctly behind a TLS terminator if one is ever
added. It does not mean building an internet-facing service.

**Application auth is still required, and Tailscale does not replace it.** The first topology is
the home LAN, where every device is already "inside"; and an overlay is network-level access
control, not a per-request identity. Defence in depth on a box that executes processes is
cheap here — one password and a session cookie.

## Section 4 — the UI

Five tabs:

- **Dashboard** — a card per process (state, uptime, pid, port check, start/stop/restart, last
  ~50 log lines), plus a health strip: STT backend, AIS cache size, time since the last AISHub
  poll, conversations stored, whether each configured path resolves, and **time since the last
  transmission arrived** — the one signal that distinguishes "SDR# is receiving" from "SDR# is
  open with the play button unpressed" (Section 0).
- **Conversations** — see Section 5.
- **Vessels** — the identified-vessels log plus a searchable AIS cache. That search earns its
  keep: "is this vessel in the cache and when was it last seen" came up repeatedly through
  August and currently takes a Python one-liner.
- **Settings** — the grouped form, including the new Paths group.
- **Logs** — pick a process, follow the tail, filter by text.

Dark theme, monospace for transcripts and logs, tabular numerals. A data-dense operator tool:
legibility beats decoration.

**Remote changes two things.** The layout must survive a phone screen, because "is it still
running?" will be asked from one. And log tailing now crosses a network, so it polls a bounded
range or uses SSE — never ships a whole day's file per refresh.

## Section 5 — data views

Data comes from the running proxy's endpoints (`/api/conversations`, `/api/ais-cache`), fetched
**server-side** by the web app and re-served. That sidesteps CORS and keeps the proxy the single
source of live truth — the on-disk JSON lags memory by up to 300 s. If the proxy is down, say so
plainly; never render an empty table.

Improvements over today's page:

- **Expose the three-layer text chain** `raw → text → conv` per turn, showing what the regex
  pass and the LLM pass each changed. Only visible through the API today.
- **Distinguish a live name from a confirmed AIS match.** Now directly answerable rather than
  inferred: `live_mmsi` is stored per turn, so `live_vessel` set with `live_mmsi` null means the
  name was heard and AIS had no such ship, while both set means AIS matched.
- **Show the candidate list.** `resolver_candidates` is stored per row — every candidate the
  resolver was offered, with position, draught, destination, age and which pass supplied it.
  "Not in the candidate list" is the stated reason in nearly every unidentified conversation,
  and that list was discarded until this morning. Showing it turns an opaque verdict into a
  reviewable one.
- **Carry the sub-cutoff shortlist**, including its "scored below the identification cutoff"
  framing. Without that framing a suggestion reads as an identification.
- **Drop "high confidence" from unidentified rows** — it reads as a contradiction. The
  confidence is about the reasoning, not about an identification that was not made.
- **A shared name is not an identification.** Where two cached vessels carry one name, show the
  MMSI rather than relying on the name; seven labelled conversations were distorted by exactly
  that collision.
- **Filters** on identified/unidentified, channel, and free text.

## Section 6 — error handling

A port held by something else: identify the holding pid and image and report it; **auto-kill
only when it is recognisably a previous instance of that process**. Start failures surface the
child's first stderr lines, not "failed to start". **Atomic config writes** (temp file +
replace) so an interrupted save cannot truncate `config.json`. Secrets never appear in errors,
logs, or API responses.

## Section 7 — testing

pytest alongside the existing suite (809 tests as of 2026-08-18). FastAPI `TestClient` for every
route, including **an unauthenticated request to every mutating route expecting a rejection** —
that is the test that keeps Section 3 true as routes are added. The supervisor is tested against
a **fake child process** (a script that prints and sleeps) so tests are deterministic and bind
no real ports. Port-clearing is tested by binding a real socket in-test. Settings schema
round-trip: defaults → `config.json` → env dict, plus validation rejecting a malformed bbox, an
out-of-range port, an unknown enum, and **a non-loopback bind with no password set**.

**Explicitly out of scope:** browser-level UI tests. No Playwright unless asked.

## Build order

Four phases now, each independently useful and testable.

1. **Settings** — `settings_schema.py`, `config.json`, the import from `start-all.bat`, the
   env-dict builder, and the Paths group. Ends with a proxy started by hand from that env dict
   behaving exactly as it does today. Everything else depends on this being right.
2. **Auth + supervisor** — session auth, bind-address guard, the process registry, detached
   start, pid-file reattachment with image-name verification, port-clearing, log redirection.
   Plus Dashboard and Logs, the smallest UI that makes the supervisor usable. Auth is in this
   phase, not a later one, because phase 2 is the first phase that can be exposed at all.
3. **Data views** — Conversations and Vessels.
4. **Host migration** — AIS-catcher as a managed process, boot-time startup on the miniPC, and
   a documented move procedure. Deliberately last: it is the phase that needs the miniPC to
   exist.

Phase 2 must not be rushed. Every failure named in Section 1 — zombie listeners, pid reuse,
lost console output — is one this project has already had.

## Decisions taken 2026-08-18 (round two)

| question | answer |
|---|---|
| miniPC OS | **Windows 11**, same as this PC — the supervisor stays Windows-specific |
| SDR# | **monitored, never managed**; started by hand, locally or over RDP |
| STT after the move | **Groq**; `whisper_cpp` kept as a first-class option for others running the repo |
| external access | **Tailscale**; no public listener in the app, app auth retained regardless |
| settings scope | **only what `start-all.bat` exposes** — 22, not 65 |
| the counter | **stays a separate process**, with an `enabled` flag so it can be retired |

## Follow-ups this raises

1. **Add today's flags to `start-all.bat`** as commented rollback lines, matching the existing
   convention, so they become operator-facing under the curation rule above:
   `AIS_LIVE_MATCH_MAX_AGE_MIN`, `AIS_CALLSIGN_SUFFIX_FALLBACK`, `AIS_SUGGEST` and companions.
   Two of them are documented as the first thing to switch off if identification regresses.
   Small, separate, and worth doing before the schema is written so it is imported rather than
   retrofitted.

2. **Win11 Pro on the miniPC** is what makes RDP available, and RDP is now the only way to
   press *play*. Worth confirming before buying — Home does not include the RDP host.

## Open questions

None outstanding. The design is ready to become an implementation plan.
