# Control panel web app — design

**Status:** revised 2026-08-18 for a networked deployment. Awaiting answers to the open
questions before an implementation plan.

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

### The consequence nobody has costed yet: STT

The GPU is in *this* PC. A miniPC chosen for low power and silence will not run whisper
large-v3. `STT_BACKEND` is currently `groq`, so today this is free — but it means the target
deployment is **committed to cloud STT**, and the local-GPU path becomes unreachable from the
miniPC unless the whisper server keeps running on this PC and the miniPC points at it over the
LAN. That is a supported configuration (`WHISPER_BACKEND_PORT` already exists and the backend
is selected by setting), but it means this PC stays on for transcription, which defeats part of
the purpose.

This is flagged as an open question rather than decided, because it changes what "24/7 on a
silent miniPC" actually buys.

### The other consequence: SDR# is a desktop application

SDR# is a Windows GUI app, and the plugin runs inside it. It cannot be supervised the way a
console process can: it needs an interactive desktop session, so `DETACHED_PROCESS` from a
service running as `LocalSystem` will start a process that never renders and may not work.

Practical resolutions, in the order I would try them:

1. Run the web app **as a scheduled task at user logon**, with the miniPC set to auto-login.
   Everything then lives in one interactive session and SDR# behaves normally. Simplest, and
   the usual answer for unattended Windows SDR boxes.
2. Run the web app as a **Windows service** and leave SDR# outside its control — started by
   the same logon task, monitored but not managed.
3. Do not manage SDR# at all; monitor it only (is the process alive, is the plugin posting
   chunks).

This is an open question because it decides how much of "start/stop remotely" is actually
deliverable for SDR# specifically. The proxy, the counter and AIS-catcher are all console
processes and are unaffected.

## Section 1 — architecture and process model

A `server/webapp/` package, uvicorn, and a **process registry rather than hardcoded handlers**.
Each managed process declares how to build its command line from config, its working directory,
pid file, log file, and optionally the port it should be listening on. Proxy, counter and now
AIS-catcher are the entries.

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

### The inventory is twice what was assumed, and the batch file is the wrong source

Counted 2026-08-18:

| | count |
|---|---|
| active `set` lines in `start-all.bat` | 12 |
| commented-out rollback `set` lines | 10 |
| **distinct env vars actually read by the proxy** | **65** |
| **read in code but absent from `start-all.bat`** | **45** |

The 45 include `AIS_LIVE_MATCH_MAX_AGE_MIN`, all five `AIS_SUGGEST_*`,
`AIS_CALLSIGN_SUFFIX_FALLBACK`, `AIS_HINT_MIN_SCORE`, the `WHISPER_VAD_*` group and every
`CONVERSATION_CORRECT_*` — essentially everything tuned this month. Importing from the batch
file alone would silently omit them.

**Two settings carry documented footguns and their descriptions must say so**, because the
schema is what the operator reads: `WHISPER_PROMPT` (a plugin-side override cost ~11 WER points
on 2026-08-07) and `AIS_HINT_MIN_SCORE` (relaxing it cost 11 precision points on 2026-08-12).

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

**Recommendation:** for access from outside the LAN, use a private overlay network
(Tailscale/WireGuard) or an authenticating reverse proxy that terminates TLS, and keep this app
bound to loopback or the overlay interface. Do not put it directly on the public internet.

The reasoning is proportion: a home-built auth layer on a box that can execute processes and
holds five API keys is a much larger security commitment than a hobby control panel warrants,
and it is one that has to stay correct forever. "Prepare for external access" is therefore
interpreted as **make nothing assume localhost** — configurable bind, correct cookie flags,
no mixed content, no hardcoded `127.0.0.1` in the frontend, honour `X-Forwarded-Proto` so it
sits correctly behind a TLS terminator — rather than *build an internet-facing service*.

This is an open question, since it is a recommendation rather than an agreed decision.

## Section 4 — the UI

Five tabs:

- **Dashboard** — a card per process (state, uptime, pid, port check, start/stop/restart, last
  ~50 log lines), plus a health strip: STT backend, AIS cache size, time since the last AISHub
  poll, conversations stored, and — new, because the box is now unattended — whether each
  configured path resolves.
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

## Open questions

1. **What OS will the miniPC run?** Assumed Windows, since SDR# and the plugin are Windows
   .NET. If so the supervisor stays Windows-specific (`DETACHED_PROCESS`, `taskkill`,
   `netstat`) and that is fine. If anything other than Windows is on the table, say so now —
   it changes the supervisor, and SDR# itself would have to be reconsidered.

2. **How is SDR# managed?** Auto-login plus a logon-scheduled task (everything in one
   interactive session, SDR# fully managed) — or is SDR# started independently and only
   *monitored*? See Section 0. This decides how much of "start/stop remotely" applies to SDR#.

3. **Where does STT run after the move?** Cloud (Groq, current default and the simple answer),
   or keep whisper.cpp on this PC and have the miniPC reach it over the LAN — which keeps a
   power-hungry machine running and undercuts the reason for the miniPC. This is the question
   with the largest practical consequence.

4. **Is the external-access recommendation accepted?** Overlay network or authenticating
   reverse proxy, with this app never directly public (Section 3) — versus building a
   public-facing listener in the app itself. My recommendation is the former, strongly.

5. **Does Settings cover all 65 settings** (Advanced group collapsed), or only the ~20 that
   `start-all.bat` mentions? Carried over from the previous revision, still unanswered. The
   settings that mattered most this month are precisely the invisible ones.

6. **Does the counter stay a separate process?** `ais_station_count.py` currently polls the AIS
   station on its own. If AIS-catcher moves onto the same box, is the counter still a distinct
   managed process, or does it fold into the proxy? Affects the registry's contents, not its
   shape.
