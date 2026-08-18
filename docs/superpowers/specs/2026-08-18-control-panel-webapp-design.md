# Control panel web app — design

**Status:** agreed, awaiting review before an implementation plan.

The design below was reached on 2026-08-13 in a brainstorming session where the user answered
every question. That session died on a weekly-limit cutoff before the spec was written, and
the working tree was clean — no file, no commit. The decisions are therefore **agreed, not
proposed**, and are not re-opened here. What *is* new is Section 2's settings inventory, which
turned out to be twice the size assumed, and a set of deltas from work done between then and
2026-08-18.

## What this is for

> "a good looking web app from where I can start what we have implemented so far (proxy,
> counter, with the possibility to change the env variables/options). Instead of viewing the
> /identified-vessels and /conversations in a browser, I'd like to view them in the web app as
> well (separate tabs maybe, you're the expert)"

Today the system is started by a `.bat` file, configured by editing that file, and observed
through two HTML pages plus a console window whose scrollback is the only record of what the
correction pass did. The web app replaces the first two and absorbs the third.

## Decisions taken (do not re-ask)

| question | decision |
|---|---|
| exposure | localhost only, no auth, no TLS |
| scope | proxy + counter only — AIS-catcher (separate Win10 box at 192.168.2.1) and WSL whisper are out |
| stack | FastAPI + a no-build frontend: one HTML file, hand-written CSS, vanilla JS, vendored |
| process lifetime | detached — children survive the dashboard closing |
| entry point | the web app replaces the `start-all.bat` shortcut as the way in |
| API keys | stored but masked (●●●● + Replace); never sent to the browser |
| UI design | `frontend-design` is pulled in at build time, not during design |

Rejected then, still rejected: pure-stdlib server-rendered (hand-rolling validation for
dozens of settings invites silent bugs); FastAPI + React/Vite (a Node toolchain in a project
that has neither Node nor a build step).

## Section 1 — architecture and process model

A new `server/webapp/` package, uvicorn on `127.0.0.1:9100`, and one shortcut that starts it
and opens a browser.

The core is a **process registry, not two hardcoded handlers**. Each managed process declares
how to build its command line from config, its working directory, pid file, log file, and
optionally the port it should be listening on. Proxy and counter are the two entries; the
registry shape is what lets whisper.cpp-in-WSL or AIS-catcher be added later without a
redesign.

Three details drawn from failures this project has already had:

- **Detached start.** `DETACHED_PROCESS`, so closing the dashboard never touches a capture
  run. On startup the app reattaches by pid file, verifying both that the pid is alive **and
  that the image name matches** — pid reuse would otherwise let it adopt an unrelated process.
- **Port-clearing on restart.** Whatever holds `:9000` must be killed before starting, exactly
  as `start-all.bat` does. Python's `allow_reuse_address` lets a second proxy bind alongside
  the first and silently take over while the original runs on as a zombie, so without this
  "Restart" in the UI is a quiet no-op.
- **Logs to files, not console windows.** Today the proxy runs under `cmd /k` and its output
  dies with the window, so `[conv-correct] not applied: <reason>` scrolls past where nobody
  keeps it. Children get stdout redirected to `server/logs/proxy-YYYY-MM-DD.log`, and the UI
  tails them.

**Validated in practice on 2026-08-18.** The proxy was run detached with stdout redirected to
a file for a full day of live traffic and behaved exactly as this section assumes. One
constraint was discovered the hard way and the implementation must respect it: `start-all.bat`
cannot be launched from a non-interactive parent, because the `start` command needs an
interactive window station to create a console. The web app must therefore spawn the child
**directly** — building the command line and environment itself — and never by shelling out
to the batch file.

## Section 2 — configuration

`server/config.json` (gitignored) becomes the source of truth, holding **values only**. A
`settings_schema.py` of **pydantic models** describes each setting: key, type, default, group,
restart-required flag, and description. That schema is the single source of truth: it
validates saves *and* drives form rendering, so `AISHUB_BBOX` is four bounded floats and
`AIS_SOURCE` is an enum, each defined once.

**The description field is where the prose comments in `start-all.bat` go** — the sea-box
reasoning, why the east edge is 4.25, the rollback lines, the AISHub rate-limit warning. Those
are some of the best documentation in the project, and regenerating the batch file would
destroy them. Moving them into the schema upgrades them into help text beside the control they
explain.

### The inventory is twice what was assumed — and `start-all.bat` is not the source

Counted on 2026-08-18:

| | count |
|---|---|
| active `set` lines in `start-all.bat` | 12 |
| commented-out rollback `set` lines | 10 |
| **distinct env vars actually read by the proxy** | **65** |
| **read in code but absent from `start-all.bat`** | **45** |

The original design said "about 30 settings, everything `set` in `start-all.bat`". That
understates it, and more importantly it names the wrong source. Forty-five settings — including
`AIS_LIVE_MATCH_MAX_AGE_MIN`, the five `AIS_SUGGEST_*` knobs, `AIS_CALLSIGN_SUFFIX_FALLBACK`,
`AIS_HINT_MIN_SCORE`, the `WHISPER_VAD_*` group and every `CONVERSATION_CORRECT_*` — exist only
as `os.environ.get` defaults in code and are invisible to the operator today.

**Decision, taken here and flagged for review:** the schema covers all 65. They are grouped,
and the code-only settings that no one should routinely touch go in an **Advanced** group,
collapsed by default. Importing from `start-all.bat` alone would silently omit the settings
this project spent August tuning.

**Two settings carry documented footguns and their descriptions must say so**, because the
schema is what the operator will read: `WHISPER_PROMPT` (a plugin-side override cost ~11 WER
points on 2026-08-07) and `AIS_HINT_MIN_SCORE` (relaxing it cost 11 precision points on
2026-08-12). Exposing a knob without its scar tissue is worse than not exposing it.

Migration is a **one-time import** of current values from `start-all.bat`, with the code
defaults filling in the rest. The batch file is then kept read-only as a fallback rather than
regenerated, so nothing silently drifts.

Restart semantics: **every setting requires a restart** — all are read at process start.
Saving shows a "proxy restart required" banner with a Restart button. Nothing pretends to
apply live.

## Section 3 — the UI

Five tabs:

- **Dashboard** — a card per process (state, uptime, pid, port check, start/stop/restart, last
  ~50 log lines), plus a health strip: STT backend, AIS cache size, time since the last AISHub
  poll, conversations stored.
- **Conversations** — see Section 4.
- **Vessels** — the identified-vessels log, plus a searchable AIS cache. That search earns its
  keep: "is this vessel in the cache and when was it last seen" came up repeatedly through
  August and currently takes a Python one-liner.
- **Settings** — the grouped form from Section 2.
- **Logs** — pick a process, follow the tail, filter by text.

Dark theme, monospace for transcripts and logs, tabular numerals. This is a data-dense
operator tool: legibility beats decoration.

## Section 4 — data views

Data comes from the running proxy's existing endpoints (`/api/conversations`, `/api/ais-cache`),
fetched **server-side** by the web app and re-served. That sidesteps CORS and keeps the proxy
the single source of live truth — the on-disk JSON lags memory by up to 300 s, so reading files
would show stale data. If the proxy is down, say so plainly; never render an empty table.

Four improvements over today's page:

- **Expose the three-layer text chain** `raw → text → conv` per turn, showing what the regex
  pass and the LLM pass each changed. Only visible through the API today.
- **Distinguish a live name from a confirmed AIS match.** They render identically today, and a
  live name can be a ship that does not exist.
- **Drop "high confidence" from unidentified rows** — it reads as a contradiction. The
  confidence is about the reasoning, not about an identification that was not made.
- **Filters** on identified/unidentified, channel, free text, plus the contested-candidate
  list.

### Additions from work landed 2026-08-18

- **The sub-cutoff shortlist** now renders on unidentified conversations (`suggestions` on the
  stored row: name, MMSI, score, and the fragment that matched). The web app must carry it,
  including the "scored below the identification cutoff" framing — without that, a suggestion
  reads as an identification.
- **`live_mmsi` is now stored per turn**, which makes the second bullet above directly
  answerable rather than inferred: `live_vessel` set with `live_mmsi` null means the name was
  heard and AIS had no such ship; both set means AIS matched.
- **`resolver_candidates` is now stored per row** — every candidate the resolver was offered,
  with position, draught, destination, age, and which pass supplied it. This deserves a view
  of its own, because "not in the candidate list" is the stated reason in nearly every
  unidentified conversation and the list was previously discarded. Showing it turns an opaque
  verdict into a reviewable one.
- **A shared name is not an identification.** Where two cached vessels carry one name, the UI
  must show the MMSI rather than relying on the name — seven labelled conversations were
  distorted by exactly that collision.

## Section 5 — error handling

A port held by something else: identify the holding pid and image and report it; **auto-kill
only when it is recognisably a previous proxy**. Start failures surface the child's first
stderr lines, not "failed to start". **Atomic config writes** (temp file + replace) so an
interrupted save cannot truncate `config.json`. Secrets never appear in errors, logs, or API
responses.

## Section 6 — testing

pytest alongside the existing suite (809 tests as of 2026-08-18). FastAPI `TestClient` for
every route. The supervisor is tested against a **fake child process** — a script that prints
and sleeps — so tests are deterministic and bind no real ports. Port-clearing is tested by
binding a real socket in-test. Settings schema round-trip: defaults → `config.json` → env
dict, plus validation rejecting a malformed bbox, an out-of-range port, and an unknown enum.

**Explicitly out of scope:** browser-level UI tests. No Playwright unless asked.

## Build order

Three phases, each independently useful and independently testable. This is not a change to
the design — it is how the implementation plan should be staged, so that nothing is built on
an unproven layer.

1. **Settings** — `settings_schema.py`, `config.json`, the one-time import from
   `start-all.bat`, and the env-dict builder. No UI, no processes. Ends with a round-trip
   test: defaults → config → env dict, and a proxy started by hand from that env dict
   behaving exactly as it does today. Everything downstream depends on this being right.
2. **Supervisor** — the process registry, detached start, pid-file reattachment with image-name
   verification, port-clearing, log redirection. Plus the Dashboard and Logs tabs, which is the
   smallest UI that makes the supervisor usable. Ends with the app able to replace the
   `start-all.bat` shortcut.
3. **Data views** — Conversations and Vessels: server-side fetch from the proxy's API, the
   three-layer text chain, the shortlist, the candidate list, filters, AIS cache search.

Phase 2 is the one that must not be rushed: every failure named in Section 1 (zombie
listeners, pid reuse, lost console output) is a failure this project has already had.

## Open question for review

One decision was taken in this document rather than in the original session, because the
inventory turned out different from what was assumed:

**Does the Settings tab cover all 65 settings (Advanced group collapsed), or only the 20-odd
that `start-all.bat` mentions?** This spec assumes all 65. The argument for it is that the
settings which mattered most this month live in the invisible 45; the argument against is a
larger form and more schema to write and maintain.
