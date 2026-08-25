# Contributing

Bug reports, fixes and improvements are welcome. This file covers the development setup and
two runtime traps that have each cost a day of debugging — please read those before touching
the C# side.

## Development setup

```bash
py -m pip install -r server/requirements.txt        # proxy + tooling

# The plugin needs the SDR# SDK DLLs from your own install. Point the build at them
# rather than editing the csproj, which is tracked:
dotnet build SDRSharp.SttPlugin/SDRSharp.SttPlugin.csproj \
       -p:SDRSharpSdkPath=C:\SDR\SdrSharpSDK\sdrplugins\lib
```

The plugin targets `net9.0-windows`, so the **.NET 9 SDK** is required to build it — .NET 8
alone will not do, whatever the runtime it eventually loads under (see trap 1 below).

## Layout

```
SDRSharp.SttPlugin/        the plugin that runs inside SDR# (C#)
  Dsp/                     VAD, resampling, filtering, normalisation
  Capture/                 optional chunk and raw-stream recording
  WhisperClient.cs         the only file that talks to the proxy

server/
  whisper-proxy.py         configuration, routing, HTTP handler, entry point
  ship_types.py            the AIS ship-type table, shared by proxy and panel
  conversation_archive.py  the SQLite archive behind the rolling window
  stt_proxy/
    corrections.py         hallucination, prompt echo, STT fixes, callsign checks
    ais.py                 vessel cache, aisstream feed, bbox parsing, name/callsign matching
    aishub.py              the AISHub polling source
    backends.py            groq + whisper.cpp, decoder params, watchdog
    identify.py            identifying the vessel in one transmission
    conversations.py       journal, windowing, retrospective resolver, page
    fewshot.py             runtime-loaded correction examples (never from source)
    llm.py, claude.py      provider abstraction and the Anthropic client
    vessel_log.py          the /identified-vessels HTML log
    markup.py              escaping and the VesselFinder link, shared by both pages
  webapp/                  the control panel (FastAPI)
    settings_schema.py     the validated setting catalogue -- the panel exports only these
    config_store.py        config.json read/write, atomic and account-restricted
    supervisor.py          detached child processes, pid files, log rotation
    health.py              per-feed liveness, separate from process liveness
    proxy_data.py          cached projections of the proxy's collections
    static/                the panel's CSS and JS
  iq/                      the IQ replay harness (baseband reader, NFM demod, segmentation)
  bench.py, bench_identify.py, stress.py, replay_sessions.py, make_references.py   tooling
  tests/                   pytest suite

tools/
  make-release.ps1         builds the release archive; asserts what it must not contain
```

### One rule that matters when editing

Several modules own mutable state — the AIS caches, the conversation journal, the resolved
list — and background threads write to it. **Read it through the module, not through an
imported name:**

```python
from stt_proxy import ais
if ais._vessel_cache: ...        # sees current state

from stt_proxy.ais import _vessel_cache
if _vessel_cache: ...            # binds a snapshot at import; silently wrong
```

The same applies to tests: patch the module that *owns* a flag, not a re-export. Getting
this wrong produces wrong results rather than an error, which is the worst kind of bug.
A `/conversations` outage caused by exactly this is what prompted the route tests.

## Running the tests

```bash
py -m pytest server/tests -q                                              # 1,232 tests
dotnet test SDRSharp.SttPlugin.Tests/SDRSharp.SttPlugin.Tests.csproj      # 39 tests
```

Both run in CI on every push and pull request. Neither needs an API key, a GPU, or a
network connection — everything external is stubbed.

## Two traps in the plugin

Both are real, both were found the hard way, and neither is caught by the compiler.

### 1. SDR# hosts plugins on .NET 8, whatever the project targets

The project targets `net9.0-windows` because the SDR# SDK DLLs require it — but the actual
host process runs the **.NET 8 runtime**. Compiling against the .NET 9 SDK lets C# silently
bind to .NET 9-only overloads that do not exist at runtime, and the plugin dies with
`MissingMethodException` when SDR# loads it.

The classic example is `TimeSpan.FromSeconds(long)`, added in .NET 9: an `int` argument
prefers it over the old `double` overload.

```csharp
TimeSpan.FromSeconds(5)      // binds to the .NET 9 overload — crashes at runtime
TimeSpan.FromSeconds(5.0)    // binds to the .NET 8 double overload — fine
```

Write numeric literals with explicit types that cannot select a newer overload.

### 2. Do not add a dependency on `System.Text.Json`

It fails to load at runtime inside SDR# even though the assembly exists on disk, because of
how SDR#'s plugin loader resolves it. Other framework assemblies compiled the same way
resolve fine, so this is not predictable from the reference table alone.

JSON is therefore hand-parsed in the plugin — see `WhisperClient.ExtractText`. If you need
to add a wire format, prefer something trivially parseable (the corrections endpoint uses
tab-delimited lines for exactly this reason) over reaching for a JSON library.

**Verify plugin changes by loading them in SDR#**, not just by compiling and running the
unit tests. Both traps pass a clean build.

## Code conventions

- **Python**: standard library where practical. The proxy deliberately uses `http.client`
  rather than `requests`.
- **Comments explain *why*, not *what*.** Much of this codebase encodes measurements —
  thresholds, scorers and filters that exist because something else was tried and failed.
  If you change one of those, please say what you measured.
- **Tests are the specification for the tricky parts.** The AIS hint filter, prompt-echo
  detection and callsign verification all have tests built from real captured traffic. If
  a test looks oddly specific, it is pinning down a bug that actually happened.

## Making a change

1. Branch from `master`.
2. Keep both test suites green.
3. Add tests for behaviour changes — particularly anything touching identification, where
   the failure mode is silent wrongness rather than a crash.
4. If you change something that was chosen on evidence, update
   [docs/design-notes.md](docs/design-notes.md) with what you measured.

## Repository tooling

`tools/` holds scripts for maintaining the repository itself. It is deliberately **not** part
of the release archive, which ships `server/` from the tracked file list.

### `make-release.ps1`

Builds the release ZIP. Takes the server tree from `git ls-files` so nothing gitignored can
reach it, and asserts the archive contains no SDR# SDK assemblies -- reference resolution
copies those into `bin\Release` from your local SDR# install, and they are proprietary.

```powershell
pwsh tools/make-release.ps1 -Version v1.0.0
```

### `traffic_snapshot.py`

GitHub's traffic API returns only the last **14 days** and permanently deletes what falls out
of that window. There is no archive and no way to ask for it later, so a repository's traffic
history exists only if something recorded it at the time.

```bash
py tools/traffic_snapshot.py             # fetch and merge into traffic/history.json
py tools/traffic_snapshot.py --report    # ...and print a summary
py tools/traffic_snapshot.py --report-only   # print from the file, no network
```

Each run merges the current window into `traffic/history.json` (gitignored -- it is repo
metadata, not source, and grows without bound). Two rules govern the merge, both covered by
tests in `server/tests/test_traffic_snapshot.py`:

- **A stored day absent from the fetch is never removed.** Absence means "older than 14 days",
  which is exactly the data worth keeping. Treating the fetch as authoritative would erase the
  history on every run while still printing a plausible summary.
- **A day present in the fetch overwrites the stored value**, because the current day is
  partial and a later read of it is the better one.

Authentication is delegated to `gh`, which must be logged in with **push access** -- the
traffic endpoints return 403 for anything less. No token is read or stored by the script.

Two numbers to read carefully. `clones` counts every `actions/checkout`, so CI dominates it:
four jobs per push means four clones per push, which is why this repo showed 52 clones from
one unique cloner before it was public. And per-day `uniques` **cannot be summed** -- GitHub
dedupes within a day, so a visitor returning on four days counts four times. The report says
so rather than quoting the sum as a visitor count.

To run it daily on Windows, pick a time the machine is actually **on** -- an overnight slot
looks tidy and never fires on a desktop that gets shut down, so every run arrives late as a
catch-up instead:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\WINDOWS\py.exe" ``
           -Argument '"D:\path	o	ools	raffic_snapshot.py"'
$trigger = New-ScheduledTaskTrigger -Daily -At 1:00pm
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable ``
            -ExecutionTimeLimit (New-TimeSpan -Minutes 10)
Register-ScheduledTask -TaskName "sdrsharp-stt-traffic" ``
    -Action $action -Trigger $trigger -Settings $settings
```

`-StartWhenAvailable` is the part that matters: without it a run missed because the machine was
off is skipped outright, and a skipped day is only recoverable while it is still inside
GitHub's 14-day window.

Release asset download counts, unlike traffic, are all-time and never expire -- they are the
better long-term signal of actual installs.

## Reference data

Do not commit transcripts of received traffic, and note that `references-*.txt` is
gitignored entirely — there is no tracked sample either. The file format is documented in
the [user manual](docs/user-manual.md#measuring-accuracy-on-your-own-traffic); build your
own set with `server/make_references.py`. See also the
[legal note](docs/user-manual.md#legal-note).
