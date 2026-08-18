# Control Panel — Phase 2: Auth, Supervisor, Dashboard and Logs Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Turn the Phase 1 settings layer into a running, password-protected web app that starts, stops, restarts and monitors the proxy and the AIS station counter, shows their logs, and says plainly whether audio is still arriving from SDR#.

**Architecture:** A FastAPI app (`create_app()`, constructed with explicit paths so tests build one against `tmp_path`) sits in front of four independent modules: `auth` (in-memory sessions, CSRF, login throttle), `registry` (declarative catalogue of managed processes and how to build each command line from `config.json`), `supervisor` (detached start, pid files, log redirection, reattachment) and `health` (path resolution plus a server-side fetch of the proxy's own status). Children are spawned directly with `DETACHED_PROCESS` — never by shelling out to `start-all.bat`, which cannot run from a non-interactive parent. The frontend is one no-build HTML/CSS/JS bundle served from `server/webapp/static/`.

**Tech Stack:** Python 3.14, FastAPI 0.141 + Starlette 1.6, uvicorn 0.52, pydantic v2, psutil 7.2, argon2-cffi 25.1, httpx (already present), pytest. No frontend build step, no npm, no CDN.

**Spec:** `docs/superpowers/specs/2026-08-18-control-panel-webapp-design.md`

**Predecessor:** `docs/superpowers/plans/2026-08-18-control-panel-phase1-settings.md` — merged? No: Phase 1 sits on `feat/control-panel-settings`, 13 commits, 865 tests green. **Phase 2 continues on that same branch.**

## Global Constraints

- **Windows 11.** `pathlib.Path` everywhere; `DETACHED_PROCESS` and `icacls` are Windows-specific and guarded with `os.name == "nt"` so an import on another OS does not explode.
- **Never launch `start-all.bat`.** Proven on 2026-08-18: `start` needs an interactive window station. The supervisor builds the command line and the environment itself.
- **Detached children.** Closing the browser, restarting the web app, or killing uvicorn must never touch a running capture.
- **Port-clearing before every start** of a process that declares a port. `ThreadingHTTPServer` sets `SO_REUSEADDR`, so a second proxy binds alongside the first and silently takes over — "restart" would otherwise be a quiet no-op.
- **Auth on every route except login, session-probe and static assets.** A catalogue-independent test enumerates the app's own mutating routes and asserts each rejects an unauthenticated request; that test is what keeps this true as routes are added.
- **Secrets never leave the server.** Not in API responses, not in logs, not in error text. `config_store.redacted_values()` already exists; nothing else may serialise raw values.
- **Every value stays a string** in `config.json`. Typing validates and renders; it never changes storage format.
- **Tests live in `server/tests/`** beside the existing 865 and run with `py -m pytest server/tests`. The full suite must be green at the end of every task.
- **Tests must never touch production.** Port 9000 serves live radio traffic; the AIS cache on disk is real data; the API keys are real money. Every test that starts a child gets a free port from `_free_port()`, a cache file under `tmp_path`, and the six network secrets cleared — the pattern established in `server/tests/test_settings_end_to_end.py`.
- **`config.json`, `credentials.json` and `server/logs/` are gitignored.** `config.json` is already ignored; the other two are added in this phase.

## Out of scope, deliberately

- **The Settings screen.** The spec's build order gives Phase 2 "Dashboard and Logs, the smallest UI that makes the supervisor usable", and the new path settings default correctly for this host, so nothing needs editing through a browser yet. The settings *API* is not built either. **Carry it to Phase 3** with the data views.
- **AIS-catcher as a managed process** — Phase 4, when the miniPC exists. The registry is built so adding it is one entry.
- **Boot-time startup / running as a service** — Phase 4.
- **TLS.** Terminated outside the app (Tailscale). The app only honours `X-Forwarded-Proto` so its cookie flags are right behind a terminator.

---

## File Structure

| file | responsibility |
|---|---|
| `server/webapp/settings_schema.py` | *(modify)* add `exported` flag; add the Paths, Processes and Web app groups |
| `server/webapp/env_builder.py` | *(modify)* export only settings marked `exported` |
| `server/webapp/secure_file.py` | restrict a file's Windows ACL to the current user |
| `server/webapp/credentials.py` | the operator password hash: load, save, verify |
| `server/webapp/set_password.py` | `py -m webapp.set_password` — the only way a password is set |
| `server/webapp/auth.py` | session store, CSRF tokens, login throttle, bind guard |
| `server/webapp/ports.py` | which pid listens on a port; clear it, but only if it is ours |
| `server/webapp/registry.py` | the managed-process catalogue: command line, cwd, log prefix, port, enabled flag |
| `server/webapp/supervisor.py` | start detached, stop, restart, status, pid-file reattachment |
| `server/webapp/logs.py` | locate the current log file; bounded byte-range tail reads |
| `server/webapp/health.py` | do the configured paths resolve; what does the proxy say about itself |
| `server/webapp/app.py` | `create_app()` — routes, auth dependency, CSRF dependency |
| `server/webapp/startup.py` | bind-address guard and the uvicorn entry point |
| `server/webapp/__main__.py` | `py -m webapp` |
| `server/webapp/static/index.html` | the whole UI: login, Dashboard, Logs |
| `server/webapp/static/app.css` | hand-written dark theme, responsive to a phone |
| `server/webapp/static/app.js` | vanilla JS: fetch, poll, render |
| `server/whisper-proxy.py` | *(modify)* `/api/status`, and record when the last chunk arrived |
| `server/stt_proxy/conversations.py` | *(modify)* `CONVERSATIONS_FILE` reads its env override |
| `server/stt_proxy/vessel_log.py` | *(modify)* `VESSELS_LOG_FILE` reads its env override |
| `server/tests/test_settings_schema.py` | *(modify)* the `exported` boundary |
| `server/tests/test_env_builder.py` | *(modify)* non-exported settings stay out of the child env |
| `server/tests/test_whisper_proxy.py` | *(modify)* `/api/status` payload and its secret-freedom |
| `server/tests/test_secure_file.py` | the ACL is actually restricted |
| `server/tests/test_credentials.py` | hash round-trip, wrong password, missing file |
| `server/tests/test_auth.py` | sessions, expiry, CSRF, throttle, bind guard |
| `server/tests/test_ports.py` | real socket, real child, refusal to kill a stranger |
| `server/tests/test_registry.py` | command lines built from config values |
| `server/tests/test_supervisor.py` | fake child: start, status, reattach, pid reuse, stop |
| `server/tests/test_logs.py` | tail offsets, rotation, truncation |
| `server/tests/test_health.py` | path checks; proxy down is stated, not blank |
| `server/tests/test_app_auth.py` | every mutating route rejects an unauthenticated request |
| `server/tests/test_app_routes.py` | process, log and health routes with a fake supervisor |
| `server/tests/test_control_panel_end_to_end.py` | the real proxy, started through the real app |

Thirteen small modules rather than one `server.py`: sessions change for security reasons, the registry changes when a process is added, the supervisor changes when Windows misbehaves, and the routes change when the UI wants something. They must not share a file.

---

### Task 1: Dependencies, and settings that are not child environment variables

**Files:**
- Modify: `server/requirements.txt`
- Modify: `server/webapp/settings_schema.py`
- Modify: `server/webapp/env_builder.py`
- Modify: `server/tests/test_settings_schema.py`
- Modify: `server/tests/test_env_builder.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: `SettingSpec`, `SETTINGS`, `BY_KEY`, `SettingType`, `validate_value` from Phase 1.
- Produces: `SettingSpec.exported: bool = True`; new catalogue keys `CONVERSATIONS_FILE`, `VESSELS_LOG_FILE`, `WHISPER_BACKEND_HOST` (exported), and `LOG_DIR`, `SDRSHARP_DIR`, `CAPTURES_DIR`, `AIS_STATION_HOST`, `AIS_STATION_HTTP_PORT`, `AIS_STATION_NMEA_PORT`, `PROXY_ENABLED`, `COUNTER_ENABLED`, `WEBAPP_BIND_HOST`, `WEBAPP_PORT` (not exported). `build_env()` keeps its signature and now skips non-exported keys.

Why one catalogue rather than a second config file: the operator sees one Settings screen and one `config.json`, which is what makes a host migration one screen (spec, "Host-portable by construction"). The `exported` flag is the boundary that stops `WEBAPP_BIND_HOST` — and, more importantly, anything else the app keeps for itself — being handed to a child process that has no business seeing it.

- [x] **Step 1: Write the failing tests**

```python
# append to server/tests/test_settings_schema.py

def test_settings_the_app_keeps_for_itself_are_not_exported_to_children():
    """A child process gets environment variables. LOG_DIR, the bind address and the
    station's host are consumed by the web app itself; handing them to the proxy would
    invent env vars the proxy never reads and could not act on."""
    for key in ("LOG_DIR", "SDRSHARP_DIR", "CAPTURES_DIR", "AIS_STATION_HOST",
                "AIS_STATION_HTTP_PORT", "AIS_STATION_NMEA_PORT", "PROXY_ENABLED",
                "COUNTER_ENABLED", "WEBAPP_BIND_HOST", "WEBAPP_PORT"):
        assert BY_KEY[key].exported is False, f"{key} must not reach a child environment"


def test_settings_the_proxy_reads_are_exported():
    for key in ("PROXY_PORT", "STT_BACKEND", "CONVERSATIONS_FILE", "VESSELS_LOG_FILE",
                "WHISPER_BACKEND_HOST", "ANTHROPIC_API_KEY"):
        assert BY_KEY[key].exported is True, f"{key} must reach the child environment"


def test_the_bind_address_defaults_to_loopback():
    """Widening it is a deliberate act, and doing so without a password refuses to start
    (see webapp/auth.py::check_bind_allowed)."""
    assert BY_KEY["WEBAPP_BIND_HOST"].default == "127.0.0.1"


def test_every_new_path_setting_is_a_path_type():
    for key in ("LOG_DIR", "SDRSHARP_DIR", "CAPTURES_DIR", "CONVERSATIONS_FILE",
                "VESSELS_LOG_FILE"):
        assert BY_KEY[key].type is SettingType.PATH
```

```python
# append to server/tests/test_env_builder.py

def test_non_exported_settings_never_reach_the_child_environment():
    env = build_env({"PROXY_PORT": "9000", "WEBAPP_BIND_HOST": "0.0.0.0",
                     "LOG_DIR": r"D:\logs"}, base={})
    assert env == {"PROXY_PORT": "9000"}
```

- [x] **Step 2: Run them to verify they fail**

Run: `py -m pytest server/tests/test_settings_schema.py server/tests/test_env_builder.py -v`
Expected: FAIL — `AttributeError: 'SettingSpec' object has no attribute 'exported'` and `KeyError: 'LOG_DIR'`.

- [x] **Step 3: Install the dependencies**

```bash
py -m pip install fastapi "uvicorn[standard]" psutil argon2-cffi httpx
```

Append to `server/requirements.txt` (keep the existing lines):

```
fastapi
uvicorn[standard]
psutil
argon2-cffi
httpx
```

Verify: `py -c "import fastapi, uvicorn, psutil, argon2, httpx; print('ok')"` prints `ok`.

- [x] **Step 4: Add the `exported` flag and the new settings**

In `server/webapp/settings_schema.py`, add to `SettingSpec`:

```python
    exported: bool = True
    """False for settings the web app consumes itself. A child process is configured by
    environment variables; a setting the proxy never reads must not appear in its
    environment, and the app's own bind address must never be visible to a child at all."""
```

Append these entries to `SETTINGS`, keeping the file's comment style. The descriptions are what the operator reads, so they carry the reasoning, exactly as the Phase 1 entries carry `start-all.bat`'s prose:

```python
    # ---- Paths ---------------------------------------------------------------
    SettingSpec(key="CONVERSATIONS_FILE", type=SettingType.PATH, default="", group="Paths",
                description="Where resolved conversations are stored. Empty means "
                            "server/stt_proxy/conversations.json, next to the code. Set it to "
                            "move the data off the install directory before a host migration."),
    SettingSpec(key="VESSELS_LOG_FILE", type=SettingType.PATH, default="", group="Paths",
                description="The identified-vessels HTML log the proxy writes and serves at "
                            "/. Empty means server/identified_vessels.html."),
    SettingSpec(key="LOG_DIR", type=SettingType.PATH, default="", group="Paths",
                exported=False,
                description="Where managed processes write their stdout, one file per process "
                            "per day. Empty means server/logs. On a headless box this is the "
                            "only record of what a process said -- today the proxy runs under "
                            "cmd /k and its output dies with the window."),
    SettingSpec(key="SDRSHARP_DIR", type=SettingType.PATH, default=r"D:\SDR\SDRSharp",
                group="Paths", exported=False,
                description="Where SDR# is installed. Monitored, never managed: SDR# needs an "
                            "interactive desktop and its play button must be pressed by hand. "
                            "The panel only checks that this path resolves."),
    SettingSpec(key="CAPTURES_DIR", type=SettingType.PATH,
                default=r"D:\SDR\SDRSharp\Plugins\SttPlugin\captures", group="Paths",
                exported=False,
                description="Where the plugin writes captured audio, in dated subdirectories. "
                            "Checked for existence only; the panel never reads it."),
    SettingSpec(key="WHISPER_BACKEND_HOST", type=SettingType.TEXT, default="localhost",
                group="Paths",
                description="Host of the local whisper.cpp server, used only when "
                            "STT_BACKEND=whisper_cpp. localhost reaches WSL2 from Windows."),

    # ---- The AIS station -----------------------------------------------------
    SettingSpec(key="AIS_STATION_HOST", type=SettingType.TEXT, default="192.168.2.1",
                group="AIS station", exported=False,
                description="The PC running AIS-catcher. Its own box, on a DHCP reservation."),
    SettingSpec(key="AIS_STATION_HTTP_PORT", type=SettingType.INT, default="8100",
                group="AIS station", exported=False, minimum=1, maximum=65535,
                description="AIS-catcher's web UI port (-N). The counter polls /ships.json "
                            "there for its range map."),
    SettingSpec(key="AIS_STATION_NMEA_PORT", type=SettingType.INT, default="10111",
                group="AIS station", exported=False, minimum=1, maximum=65535,
                description="AIS-catcher's NMEA TCP output port (-P). The counter connects to "
                            "it to count distinct MMSIs per hour."),

    # ---- Managed processes ---------------------------------------------------
    SettingSpec(key="PROXY_ENABLED", type=SettingType.BOOL, default="on", group="Processes",
                exported=False,
                description="Whether the proxy appears on the dashboard as a startable "
                            "process. Disabled is not the same as stopped: a stopped process "
                            "is one the operator turned off and may want back."),
    SettingSpec(key="COUNTER_ENABLED", type=SettingType.BOOL, default="on", group="Processes",
                exported=False,
                description="Whether the AIS station counter is startable. It exists to "
                            "measure the local receiver's coverage and is expected to become "
                            "unnecessary once that receiver has proven itself -- switch this "
                            "off then rather than deleting anything."),

    # ---- The web app itself --------------------------------------------------
    SettingSpec(key="WEBAPP_BIND_HOST", type=SettingType.TEXT, default="127.0.0.1",
                group="Web app", exported=False,
                description="The address the control panel listens on. 127.0.0.1 means this "
                            "machine only. Widening it to 0.0.0.0 exposes a panel that starts "
                            "processes and holds six API keys, so with no password set the "
                            "app refuses to start rather than opening that window."),
    SettingSpec(key="WEBAPP_PORT", type=SettingType.INT, default="8787", group="Web app",
                exported=False, minimum=1, maximum=65535,
                description="The control panel's own port. Deliberately not 9000, which the "
                            "proxy owns."),
```

- [x] **Step 5: Make `build_env` honour the flag**

In `server/webapp/env_builder.py`, replace the `if key not in BY_KEY: continue` line:

```python
        spec = BY_KEY.get(key)
        if spec is None or not spec.exported:
            continue
```

- [x] **Step 6: Ignore the new secret-bearing files**

Append to `.gitignore` (next to the existing `config.json` line):

```
credentials.json
server/logs/
```

- [x] **Step 7: Run the full suite**

Run: `py -m pytest server/tests`
Expected: all pass, including `test_catalogue_defaults.py` — the three new exported settings have no `os.environ.get` in `stt_proxy` yet, so nothing can drift until Task 2 adds them with matching defaults (`""`, `""`, `"localhost"`).

- [x] **Step 8: Commit**

```bash
git add server/requirements.txt server/webapp/settings_schema.py server/webapp/env_builder.py server/tests/test_settings_schema.py server/tests/test_env_builder.py .gitignore
git commit -m "Separate the settings the app keeps for itself from the ones a child process gets"
```

---

### Task 2: The proxy answers for itself

**Files:**
- Modify: `server/whisper-proxy.py`
- Modify: `server/stt_proxy/conversations.py:871`
- Modify: `server/stt_proxy/vessel_log.py:14-16`
- Modify: `server/tests/test_whisper_proxy.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `GET /api/status` on the proxy returning a JSON object with exactly the keys `stt_backend`, `ais_source`, `ais_cache_size`, `ais_last_poll_at`, `conversations`, `last_chunk_at`, `started_at`, `now` — timestamps are epoch seconds or `null`. `webapp/health.py` (Task 11) is its only consumer.

The dashboard's most important number is **time since the last chunk arrived**, because that is what separates "SDR# is up and receiving" from "SDR# is up with the play button unpressed". Nothing records it today. `now` travels with the payload so the client ages every timestamp against the proxy's clock, not the browser's.

- [x] **Step 1: Write the failing tests**

```python
# append to server/tests/test_whisper_proxy.py

def test_status_payload_reports_what_the_dashboard_needs(monkeypatch):
    """Everything the health strip shows, from the process that actually knows it."""
    monkeypatch.setattr(proxy, "_last_chunk_at", 1_755_500_000.0, raising=False)
    monkeypatch.setattr(ais, "_vessel_cache", {"1": {}, "2": {}}, raising=False)
    monkeypatch.setattr(ais, "_last_poll_at",
                        datetime.datetime(2026, 8, 18, 12, 0, 0), raising=False)
    monkeypatch.setattr(conversations, "_resolved", [{}, {}, {}], raising=False)

    payload = proxy._status_payload()

    assert payload["ais_cache_size"] == 2
    assert payload["conversations"] == 3
    assert payload["last_chunk_at"] == 1_755_500_000.0
    assert payload["ais_last_poll_at"] == datetime.datetime(2026, 8, 18, 12, 0, 0).timestamp()
    assert payload["now"] >= payload["started_at"]


def test_status_payload_carries_no_secret(monkeypatch):
    """This payload crosses a network to a browser. The key set is pinned so that adding a
    field is a deliberate act -- one os.environ.get too many would publish an API key."""
    assert set(proxy._status_payload()) == {
        "stt_backend", "ais_source", "ais_cache_size", "ais_last_poll_at",
        "conversations", "last_chunk_at", "started_at", "now",
    }


def test_last_chunk_at_is_unset_before_any_audio_arrives():
    """Null means 'nothing yet', which the dashboard must show differently from 'long ago'."""
    assert proxy._status_payload()["last_chunk_at"] is None or isinstance(
        proxy._status_payload()["last_chunk_at"], float)


def test_conversations_file_honours_its_environment_override(monkeypatch, tmp_path):
    """Hardcoded until now, unlike AIS_CACHE_FILE -- which made the end-to-end test only
    incidentally safe: it never pointed the child's conversation store anywhere private."""
    import importlib
    monkeypatch.setenv("CONVERSATIONS_FILE", str(tmp_path / "conv.json"))
    reloaded = importlib.reload(conversations)
    try:
        assert reloaded.CONVERSATIONS_FILE == str(tmp_path / "conv.json")
    finally:
        monkeypatch.delenv("CONVERSATIONS_FILE")
        importlib.reload(conversations)
```

- [x] **Step 2: Run them to verify they fail**

Run: `py -m pytest server/tests/test_whisper_proxy.py -k "status or conversations_file" -v`
Expected: FAIL — `AttributeError: module 'whisper_proxy' has no attribute '_status_payload'`.

- [x] **Step 3: Add the path overrides**

`server/stt_proxy/conversations.py`, replacing line 871 — mirroring the `AIS_CACHE_FILE` pattern in `ais.py:50` so there is one idiom for this in the codebase:

```python
_DEFAULT_CONVERSATIONS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "conversations.json")
CONVERSATIONS_FILE = os.path.normpath(
    os.environ.get("CONVERSATIONS_FILE", "").strip() or _DEFAULT_CONVERSATIONS_FILE)
```

`server/stt_proxy/vessel_log.py`, replacing lines 14-16:

```python
_DEFAULT_VESSELS_LOG_FILE = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "identified_vessels.html"))
VESSELS_LOG_FILE = os.path.normpath(
    os.environ.get("VESSELS_LOG_FILE", "").strip() or _DEFAULT_VESSELS_LOG_FILE)
```

`server/whisper-proxy.py` line 58 area, so no host is hardcoded:

```python
BACKEND_HOST = os.environ.get("WHISPER_BACKEND_HOST", "localhost").strip() or "localhost"
```

- [x] **Step 4: Record chunk arrival and serve the status**

In `server/whisper-proxy.py`, near the other module-level state (after line 91):

```python
# When the plugin last posted audio, and when this process started. Both are epoch seconds.
# Chunk arrival -- not process liveness -- is what tells the dashboard SDR# is actually
# receiving: SDR# can be open with the play button unpressed and nothing would ever arrive.
_last_chunk_at: float | None = None
_STARTED_AT = time.time()
```

Add the payload builder beside the other module-level helpers:

```python
def _status_payload() -> dict:
    """What the control panel needs, and nothing else.

    Every field here reaches a browser over a network, so the key set is pinned by a test.
    Read the live modules rather than the re-exports: the feed thread rebinds ais._vessel_cache,
    so an imported name would freeze a snapshot.
    """
    with ais._cache_lock:
        cache_size = len(ais._vessel_cache)
    with conversations._resolved_lock:
        stored = len(conversations._resolved)
    last_poll = ais._last_poll_at
    return {
        "stt_backend": STT_BACKEND,
        "ais_source": os.environ.get("AIS_SOURCE", "aishub").strip().lower(),
        "ais_cache_size": cache_size,
        "ais_last_poll_at": last_poll.timestamp() if last_poll else None,
        "conversations": stored,
        "last_chunk_at": _last_chunk_at,
        "started_at": _STARTED_AT,
        "now": time.time(),
    }
```

In `do_GET`, before the final 404:

```python
        if self.path == "/api/status":
            try:
                data = json.dumps(_status_payload()).encode("utf-8")
                self.send_response(200)
                self._send_live_headers("application/json", len(data), cors=True)
                self.wfile.write(data)
            except Exception as exc:
                self.send_error(500, str(exc))
            return
```

In `do_POST`, immediately after the `if self.path not in PATH_MAP:` block returns 404 — i.e. at the point where a real transcription request is confirmed:

```python
        global _last_chunk_at
        _last_chunk_at = time.time()
```

- [x] **Step 5: Run the tests**

Run: `py -m pytest server/tests/test_whisper_proxy.py -v`
Expected: PASS.

- [x] **Step 6: Run the full suite**

Run: `py -m pytest server/tests`
Expected: all pass. `test_catalogue_defaults.py` now sees `CONVERSATIONS_FILE` and `VESSELS_LOG_FILE` with code default `""` and `WHISPER_BACKEND_HOST` with `"localhost"`, which is exactly what Task 1 put in the catalogue.

- [x] **Step 7: Commit**

```bash
git add server/whisper-proxy.py server/stt_proxy/conversations.py server/stt_proxy/vessel_log.py server/tests/test_whisper_proxy.py
git commit -m "Let the proxy report its own health, and stop hardcoding where it writes"
```

---

### Task 3: Files only this account can read

**Files:**
- Create: `server/webapp/secure_file.py`
- Create: `server/tests/test_secure_file.py`
- Modify: `server/webapp/config_store.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `restrict(path: Path) -> bool` — breaks ACL inheritance and grants full control to the current user only; returns False (never raises) when that cannot be done, so a hardening failure can never stop the app from saving a setting.

`config.json` holds six plaintext API keys and inherits its directory's ACL today, which on a normal Windows install means every local user can read it. The spec's Section 3 covers secrets in transit; this is the at-rest half.

- [x] **Step 1: Write the failing test**

```python
# server/tests/test_secure_file.py
"""config.json holds six plaintext API keys. It inherits its directory's ACL, which on a
normal Windows install lets any local account read it. This is the at-rest half of "secrets
never leave the server"."""
import getpass
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.secure_file import restrict  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")


def test_restrict_removes_every_grant_except_this_account(tmp_path):
    secret = tmp_path / "config.json"
    secret.write_text('{"GROQ_API_KEY": "gsk_example"}', encoding="utf-8")

    assert restrict(secret) is True

    acl = subprocess.run(["icacls", str(secret)], capture_output=True, text=True).stdout
    user = getpass.getuser().lower()
    assert user in acl.lower()
    assert "BUILTIN\\Users" not in acl
    assert "Everyone" not in acl


def test_restrict_reports_failure_rather_than_raising(tmp_path):
    """A hardening failure must never be the reason a setting cannot be saved."""
    assert restrict(tmp_path / "does-not-exist.json") is False
```

- [x] **Step 2: Run it to verify it fails**

Run: `py -m pytest server/tests/test_secure_file.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.secure_file'`.

- [x] **Step 3: Implement**

```python
# server/webapp/secure_file.py
"""Restrict a file so only this account can read it.

config.json and credentials.json hold, respectively, six API keys and the password hash that
guards a panel able to start processes. They inherit their directory's ACL, which on a normal
Windows install means every local user can read them.

icacls rather than moving the files to %LOCALAPPDATA%: the config must stay beside the code it
configures, so that a host migration is a copy of one directory rather than a hunt through two.
"""
from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path


def restrict(path: Path) -> bool:
    """Break inheritance and grant full control to the current user alone.

    Returns whether it worked. It never raises: hardening is defence in depth, and a failure
    here must not become the reason an operator cannot save a setting or a password.
    """
    path = Path(path)
    if os.name != "nt" or not path.exists():
        return False
    try:
        done = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
```

- [x] **Step 4: Run the test**

Run: `py -m pytest server/tests/test_secure_file.py -v`
Expected: PASS.

- [x] **Step 5: Harden `config.json` on every save**

In `server/webapp/config_store.py`, add the import and call it after `os.replace`:

```python
from webapp.secure_file import restrict
```

```python
        os.replace(tmp, path)
        # Best effort, deliberately unchecked: this file holds six API keys, and an ACL that
        # could not be tightened is not a reason to refuse a save the caller already made.
        restrict(path)
```

- [x] **Step 6: Run the full suite**

Run: `py -m pytest server/tests`
Expected: all pass. Watch `test_config_store.py` in particular — it writes many temporary configs, and each now takes an `icacls` call.

- [x] **Step 7: Commit**

```bash
git add server/webapp/secure_file.py server/webapp/config_store.py server/tests/test_secure_file.py
git commit -m "Restrict config.json to this account, so six API keys stop being world-readable"
```

---

### Task 4: The operator password

**Files:**
- Create: `server/webapp/credentials.py`
- Create: `server/webapp/set_password.py`
- Create: `server/tests/test_credentials.py`

**Interfaces:**
- Consumes: `webapp.secure_file.restrict`.
- Produces: `hash_password(password: str) -> str`, `verify_password(stored: str, password: str) -> bool`, `load_hash(path: Path) -> str | None`, `save_password(path: Path, password: str) -> None`, `has_password(path: Path) -> bool`, and `MIN_LENGTH: int`. The file is `credentials.json`, `{"password_hash": "$argon2id$..."}`.

A password hash is a credential, not a setting: it is never rendered in a form, never sent to a browser, and never edited as text. It therefore lives in its own file, set only by `py -m webapp.set_password`. There is deliberately no bootstrap route — an unauthenticated "set the first password" endpoint is exactly the window Section 3 exists to close.

- [x] **Step 1: Write the failing test**

```python
# server/tests/test_credentials.py
"""The one password that guards a panel which starts processes and holds six API keys."""
import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import credentials  # noqa: E402


def test_a_saved_password_verifies_and_a_wrong_one_does_not(tmp_path):
    path = tmp_path / "credentials.json"
    credentials.save_password(path, "correct horse battery staple")

    stored = credentials.load_hash(path)
    assert credentials.verify_password(stored, "correct horse battery staple") is True
    assert credentials.verify_password(stored, "correct horse battery stapl") is False


def test_the_password_itself_is_never_written_to_disk(tmp_path):
    path = tmp_path / "credentials.json"
    credentials.save_password(path, "correct horse battery staple")
    assert "correct horse" not in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["password_hash"].startswith("$argon2")


def test_no_password_file_means_no_password(tmp_path):
    """Distinct from an empty one. check_bind_allowed refuses a non-loopback bind on this."""
    assert credentials.load_hash(tmp_path / "credentials.json") is None
    assert credentials.has_password(tmp_path / "credentials.json") is False


def test_a_short_password_is_refused(tmp_path):
    """This panel is reachable from a LAN and executes processes. Twelve characters is the
    floor; rate limiting (auth.py) covers the rest."""
    with pytest.raises(ValueError, match="at least 12"):
        credentials.save_password(tmp_path / "credentials.json", "hunter2")


def test_a_corrupt_credentials_file_reads_as_no_password(tmp_path):
    """Fail closed on reading: a damaged file must not verify anything, and must not crash
    the app at import either."""
    path = tmp_path / "credentials.json"
    path.write_text("{not json", encoding="utf-8")
    assert credentials.load_hash(path) is None


def test_verify_tolerates_a_hash_it_cannot_parse():
    assert credentials.verify_password("not-a-hash", "anything") is False
```

- [x] **Step 2: Run it to verify it fails**

Run: `py -m pytest server/tests/test_credentials.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.credentials'`.

- [x] **Step 3: Implement the credential store**

```python
# server/webapp/credentials.py
"""The operator password: hashed with argon2id, stored in its own file, never in config.json.

A password hash is a credential, not a setting. It is never rendered in a form, never sent to
a browser and never edited as text, so it does not belong in the catalogue that drives both.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from webapp.secure_file import restrict

MIN_LENGTH = 12

_HASHER = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"password must be at least {MIN_LENGTH} characters")
    return _HASHER.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    """False for every failure, including a hash this build cannot parse.

    Fails closed and silently by design: the caller turns this into one 401 with no detail,
    so a login page cannot be used to tell a bad password from a damaged file.
    """
    if not stored:
        return False
    try:
        return _HASHER.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def load_hash(path: Path) -> str | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    value = raw.get("password_hash") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def has_password(path: Path) -> bool:
    return load_hash(path) is not None


def save_password(path: Path, password: str) -> None:
    """Hash, write atomically, then restrict the file to this account."""
    digest = hash_password(password)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"password_hash": digest}, handle, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    restrict(path)
```

- [x] **Step 4: Implement the CLI**

```python
# server/webapp/set_password.py
"""py -m webapp.set_password -- the only way the control panel's password is set.

Deliberately not a route: an unauthenticated "set the first password" endpoint is exactly the
window that authentication exists to close.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

from webapp.credentials import MIN_LENGTH, save_password

CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "credentials.json"


def main(argv: list[str] | None = None) -> int:
    path = Path(argv[0]) if argv else CREDENTIALS_PATH
    first = getpass.getpass("New control panel password: ")
    second = getpass.getpass("Repeat: ")
    if first != second:
        print("They do not match. Nothing was written.", file=sys.stderr)
        return 1
    try:
        save_password(path, first)
    except ValueError as exc:
        print(f"{exc} (minimum {MIN_LENGTH}). Nothing was written.", file=sys.stderr)
        return 1
    print(f"Password set in {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
```

- [ ] **Step 5: Run the tests and set a real password** — tests done; the password itself is
      NOT set. `getpass` needs a console the agent does not have, and choosing the operator's
      password for them would be worse than leaving it. Run it yourself:
      `cd server && py -m webapp.set_password`

Run: `py -m pytest server/tests/test_credentials.py -v` → PASS.

Then, from `server/`, set the password this deployment will use:

```bash
cd server && py -m webapp.set_password
```

Confirm `server/credentials.json` exists, contains only a `$argon2id$` string, and is gitignored (`git status --short` shows nothing).

- [x] **Step 6: Commit**

```bash
git add server/webapp/credentials.py server/webapp/set_password.py server/tests/test_credentials.py
git commit -m "Add the operator password: argon2id, its own file, set only from the console"
```

---

### Task 5: Sessions, CSRF and the bind guard

**Files:**
- Create: `server/webapp/auth.py`
- Create: `server/tests/test_auth.py`

**Interfaces:**
- Consumes: nothing (pure logic; the routes wire it up in Task 9).
- Produces:
  - `Session` (dataclass: `token: str`, `csrf: str`, `created_at: float`, `last_seen_at: float`)
  - `SessionStore(ttl_sec=43200, idle_sec=7200, clock=time.time)` with `create() -> Session`, `get(token: str | None) -> Session | None`, `destroy(token: str) -> None`, `count() -> int`
  - `LoginThrottle(max_failures=5, window_sec=300, clock=time.time)` with `check(client: str) -> None` raising `TooManyAttempts`, `record_failure(client: str)`, `record_success(client: str)`
  - `TooManyAttempts(Exception)`
  - `check_bind_allowed(host: str, has_password: bool) -> None` raising `UnsafeBind`
  - `UnsafeBind(Exception)`
  - `COOKIE_NAME = "cp_session"`, `CSRF_HEADER = "X-CSRF-Token"`

Sessions live in memory: there is one operator, and a restart of the panel logging them out is the correct amount of ceremony. CSRF is a per-session token echoed in a header, because the browser is now on a different machine and cookies travel.

- [x] **Step 1: Write the failing test**

```python
# server/tests/test_auth.py
"""Sessions, CSRF and the guard that stops the panel opening on a LAN with no password."""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.auth import (  # noqa: E402
    LoginThrottle, SessionStore, TooManyAttempts, UnsafeBind, check_bind_allowed,
)


class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_created_session_is_retrievable_by_its_token():
    store = SessionStore()
    session = store.create()
    assert store.get(session.token) is session
    assert store.get("not-a-token") is None
    assert store.get(None) is None


def test_each_session_gets_its_own_csrf_token():
    store = SessionStore()
    first, second = store.create(), store.create()
    assert first.csrf != second.csrf
    assert len(first.csrf) >= 32


def test_a_session_expires_after_the_idle_window():
    clock = _Clock()
    store = SessionStore(idle_sec=100, clock=clock)
    session = store.create()
    clock.now += 99
    assert store.get(session.token) is not None   # activity refreshes it
    clock.now += 99
    assert store.get(session.token) is not None
    clock.now += 101
    assert store.get(session.token) is None


def test_a_session_expires_at_its_absolute_ttl_however_active():
    clock = _Clock()
    store = SessionStore(ttl_sec=200, idle_sec=1_000, clock=clock)
    session = store.create()
    for _ in range(4):
        clock.now += 60
        store.get(session.token)
    assert store.get(session.token) is None


def test_destroying_a_session_makes_its_token_useless():
    store = SessionStore()
    session = store.create()
    store.destroy(session.token)
    assert store.get(session.token) is None


def test_five_failures_lock_a_client_out_and_the_window_expires():
    clock = _Clock()
    throttle = LoginThrottle(max_failures=5, window_sec=300, clock=clock)
    for _ in range(5):
        throttle.check("192.168.2.9")
        throttle.record_failure("192.168.2.9")
    with pytest.raises(TooManyAttempts):
        throttle.check("192.168.2.9")
    clock.now += 301
    throttle.check("192.168.2.9")


def test_a_lockout_is_per_client():
    throttle = LoginThrottle(max_failures=1)
    throttle.record_failure("192.168.2.9")
    with pytest.raises(TooManyAttempts):
        throttle.check("192.168.2.9")
    throttle.check("192.168.2.10")


def test_a_success_clears_the_failure_count():
    throttle = LoginThrottle(max_failures=2)
    throttle.record_failure("192.168.2.9")
    throttle.record_success("192.168.2.9")
    throttle.record_failure("192.168.2.9")
    throttle.check("192.168.2.9")


def test_binding_beyond_loopback_without_a_password_is_refused():
    """The failure this exists to prevent is silent: an app that starts, works, and is open."""
    with pytest.raises(UnsafeBind, match="set_password"):
        check_bind_allowed("0.0.0.0", has_password=False)
    with pytest.raises(UnsafeBind):
        check_bind_allowed("192.168.2.18", has_password=False)


def test_loopback_without_a_password_is_allowed_and_any_bind_with_one_is():
    check_bind_allowed("127.0.0.1", has_password=False)
    check_bind_allowed("localhost", has_password=False)
    check_bind_allowed("::1", has_password=False)
    check_bind_allowed("0.0.0.0", has_password=True)
```

- [x] **Step 2: Run it to verify it fails**

Run: `py -m pytest server/tests/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.auth'`.

- [x] **Step 3: Implement**

```python
# server/webapp/auth.py
"""Sessions, CSRF tokens, login rate limiting, and the bind-address guard.

In memory on purpose: there is one operator, and a panel restart logging them out is the right
amount of ceremony for something that can start and stop processes.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

COOKIE_NAME = "cp_session"
CSRF_HEADER = "X-CSRF-Token"

_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


class TooManyAttempts(Exception):
    """Login refused for now. A LAN-reachable password prompt is not a free oracle."""


class UnsafeBind(Exception):
    """Refusing to listen beyond loopback with no password configured."""


@dataclass
class Session:
    token: str
    csrf: str
    created_at: float
    last_seen_at: float


class SessionStore:
    def __init__(self, ttl_sec: float = 43_200, idle_sec: float = 7_200,
                 clock: Callable[[], float] = time.time):
        self._ttl = ttl_sec
        self._idle = idle_sec
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        now = self._clock()
        session = Session(token=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(32),
                          created_at=now, last_seen_at=now)
        self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        """The session, refreshed; None if unknown, idle too long, or past its absolute TTL."""
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        now = self._clock()
        if now - session.last_seen_at > self._idle or now - session.created_at > self._ttl:
            self._sessions.pop(token, None)
            return None
        session.last_seen_at = now
        return session

    def destroy(self, token: str) -> None:
        self._sessions.pop(token, None)

    def count(self) -> int:
        return len(self._sessions)


@dataclass
class _Attempts:
    failures: int = 0
    first_at: float = 0.0


class LoginThrottle:
    def __init__(self, max_failures: int = 5, window_sec: float = 300,
                 clock: Callable[[], float] = time.time):
        self._max = max_failures
        self._window = window_sec
        self._clock = clock
        self._by_client: dict[str, _Attempts] = {}

    def check(self, client: str) -> None:
        record = self._by_client.get(client)
        if record is None:
            return
        if self._clock() - record.first_at > self._window:
            self._by_client.pop(client, None)
            return
        if record.failures >= self._max:
            raise TooManyAttempts(
                f"too many failed attempts; try again in "
                f"{int(self._window - (self._clock() - record.first_at))}s")

    def record_failure(self, client: str) -> None:
        now = self._clock()
        record = self._by_client.get(client)
        if record is None or now - record.first_at > self._window:
            self._by_client[client] = _Attempts(failures=1, first_at=now)
            return
        record.failures += 1

    def record_success(self, client: str) -> None:
        self._by_client.pop(client, None)


def check_bind_allowed(host: str, has_password: bool) -> None:
    """Raise unless it is safe to listen on `host`.

    The failure this prevents is silent: an app bound to 0.0.0.0 with no password starts
    cleanly, works perfectly, and hands anyone on the network six API keys and the ability to
    start processes. Refusing to start is the only signal that arrives in time.
    """
    if has_password or (host or "").strip().lower() in _LOOPBACK:
        return
    raise UnsafeBind(
        f"WEBAPP_BIND_HOST is {host!r}, which is reachable from the network, and no password "
        f"is set. Run `py -m webapp.set_password` from the server directory, or set "
        f"WEBAPP_BIND_HOST back to 127.0.0.1.")
```

- [x] **Step 4: Run the test**

Run: `py -m pytest server/tests/test_auth.py -v`
Expected: PASS, 10 tests.

- [x] **Step 5: Commit**

```bash
git add server/webapp/auth.py server/tests/test_auth.py
git commit -m "Add sessions, CSRF tokens, login throttling and the refusal to bind wide without a password"
```

---

### Task 6: Who holds the port, and may we take it

**Files:**
- Create: `server/webapp/ports.py`
- Create: `server/tests/test_ports.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `pid_listening_on(port: int) -> int | None`, `image_name(pid: int) -> str | None`, `clear_port(port: int, expected_images: set[str], timeout_sec: float = 5.0) -> bool`, `PortHeldByStranger(Exception)` carrying `.pid` and `.image`.

`start-all.bat` kills whatever holds `:9000` unconditionally. The panel must not: it can be triggered from a phone by someone who cannot see what died. So it kills only when the holder's image name is one this registry entry could plausibly have started, and otherwise reports the pid and the image and refuses.

psutil rather than parsing `netstat -ano` and `tasklist`: `tasklist` has been observed reporting stale entries in this project, and psutil gives pid, image name and creation time from one API.

- [x] **Step 1: Write the failing test**

```python
# server/tests/test_ports.py
"""Port clearing. Without it "Restart" is a quiet no-op: Python's ThreadingHTTPServer sets
SO_REUSEADDR, so a second proxy binds alongside the first, takes the port, and leaves the
original running as a zombie."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.ports import PortHeldByStranger, clear_port, image_name, pid_listening_on  # noqa: E402

_LISTENER = (
    "import socket, time; "
    "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
    "s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(120)"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_listening(port: int, timeout: float = 10.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = pid_listening_on(port)
        if pid:
            return pid
        time.sleep(0.1)
    raise AssertionError(f"nothing ever listened on {port}")


def test_a_listening_socket_in_this_process_is_found():
    port = _free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        assert pid_listening_on(port) == os.getpid()


def test_nothing_listening_is_reported_as_nothing():
    assert pid_listening_on(_free_port()) is None


def test_the_image_name_of_this_process_is_a_python():
    assert "python" in (image_name(os.getpid()) or "").lower()


def test_clear_port_kills_a_listener_it_recognises():
    port = _free_port()
    child = subprocess.Popen([sys.executable, "-c", _LISTENER.format(port=port)])
    try:
        _wait_until_listening(port)
        assert clear_port(port, {Path(sys.executable).name.lower()}) is True
        assert pid_listening_on(port) is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=10)


def test_clear_port_refuses_to_kill_something_it_did_not_start():
    """The operator may be on a phone and cannot see what would die."""
    port = _free_port()
    child = subprocess.Popen([sys.executable, "-c", _LISTENER.format(port=port)])
    try:
        pid = _wait_until_listening(port)
        with pytest.raises(PortHeldByStranger) as caught:
            clear_port(port, {"sdrsharp.exe"})
        assert caught.value.pid == pid
        assert "python" in caught.value.image.lower()
        assert pid_listening_on(port) == pid   # still alive
    finally:
        child.kill()
        child.wait(timeout=10)


def test_clearing_a_free_port_is_a_no_op():
    assert clear_port(_free_port(), {"python.exe"}) is False
```

- [x] **Step 2: Run it to verify it fails**

Run: `py -m pytest server/tests/test_ports.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.ports'`.

- [x] **Step 3: Implement**

```python
# server/webapp/ports.py
"""Which process holds a TCP port, and whether we are allowed to take it.

start-all.bat kills whatever holds :9000 unconditionally. A panel reachable from a phone must
not: the operator cannot see what would die. So a holder is killed only when its image name is
one the registry entry could have started, and anything else is reported and refused.

psutil rather than netstat + tasklist: tasklist has been observed in this project reporting
processes that no longer exist, and psutil answers pid, image name and start time from one API.
"""
from __future__ import annotations

import time

import psutil


class PortHeldByStranger(Exception):
    """Something we did not start is listening. Report it; never kill it."""

    def __init__(self, port: int, pid: int, image: str):
        super().__init__(f"port {port} is held by pid {pid} ({image}), which is not one of ours")
        self.port = port
        self.pid = pid
        self.image = image


def pid_listening_on(port: int) -> int | None:
    """The pid of the process listening on `port`, or None.

    psutil can return connections whose pid is None for processes this account cannot open;
    those are skipped rather than reported, because a pid we cannot see is one we cannot act on.
    """
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return None
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        if conn.laddr.port == port and conn.pid:
            return conn.pid
    return None


def image_name(pid: int) -> str | None:
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def clear_port(port: int, expected_images: set[str], timeout_sec: float = 5.0) -> bool:
    """Free `port` if one of ours holds it. Returns whether anything was killed.

    Raises PortHeldByStranger when the holder's image is not in `expected_images`.
    """
    pid = pid_listening_on(port)
    if pid is None:
        return False
    name = image_name(pid) or "unknown"
    if name.lower() not in {image.lower() for image in expected_images}:
        raise PortHeldByStranger(port, pid, name)

    try:
        holder = psutil.Process(pid)
        holder.terminate()
        holder.wait(timeout=timeout_sec)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        holder.kill()
        holder.wait(timeout=timeout_sec)

    # The handle can outlive the process briefly. Waiting here means a start that follows
    # cannot lose the race and bind alongside a dying zombie -- the exact failure this exists
    # to prevent.
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if pid_listening_on(port) is None:
            return True
        time.sleep(0.1)
    return True
```

- [x] **Step 4: Run the test**

Run: `py -m pytest server/tests/test_ports.py -v`
Expected: PASS, 6 tests. If `test_a_listening_socket_in_this_process_is_found` fails on a machine where psutil cannot see own-process connections, run the suite from an ordinary (non-elevated) shell first — elevation is not required for own processes.

- [x] **Step 5: Commit**

```bash
git add server/webapp/ports.py server/tests/test_ports.py
git commit -m "Free a port before starting, but only when we recognise what holds it"
```

---

### Task 7: The process registry

**Files:**
- Create: `server/webapp/registry.py`
- Create: `server/tests/test_registry.py`

**Interfaces:**
- Consumes: `webapp.settings_schema.BY_KEY`, `webapp.env_builder.build_env`.
- Produces:
  - `ProcessSpec` (frozen dataclass: `name`, `label`, `log_prefix`, `image_name`, `enabled_key`, `port_key: str | None`, `build_argv: Callable[[dict[str, str], Paths], list[str]]`)
  - `Paths` (frozen dataclass: `server_dir: Path`, `log_dir: Path`)
  - `resolve_paths(values: dict[str, str], server_dir: Path) -> Paths`
  - `PROCESSES: tuple[ProcessSpec, ...]` — `proxy` and `counter`
  - `BY_NAME: dict[str, ProcessSpec]`
  - `argv_for(spec, values, paths) -> list[str]`, `env_for(spec, values) -> dict[str, str]`, `port_for(spec, values) -> int | None`, `is_enabled(spec, values) -> bool`

A registry, not two handlers: AIS-catcher joins in Phase 4 as one more entry, and the counter may be retired by flipping `COUNTER_ENABLED`. Disabled is not stopped — a disabled process is not shown as failed.

- [x] **Step 1: Write the failing test**

```python
# server/tests/test_registry.py
"""The managed-process catalogue: what gets started, with which command line, from config."""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store, registry  # noqa: E402


def _values(**overrides) -> dict[str, str]:
    values = config_store.load(Path("does-not-exist.json"))   # every key at its default
    values.update(overrides)
    return values


def test_the_proxy_is_started_by_running_whisper_proxy_with_this_interpreter():
    paths = registry.resolve_paths(_values(), _SERVER_DIR)
    argv = registry.argv_for(registry.BY_NAME["proxy"], _values(), paths)
    assert argv[0] == sys.executable
    assert Path(argv[1]).name == "whisper-proxy.py"
    assert Path(argv[1]).exists()


def test_the_proxy_is_never_started_through_the_batch_file():
    """Proven on 2026-08-18: `start` needs an interactive window station, so start-all.bat
    cannot be launched from a service or from a detached parent."""
    paths = registry.resolve_paths(_values(), _SERVER_DIR)
    for spec in registry.PROCESSES:
        argv = registry.argv_for(spec, _values(), paths)
        joined = " ".join(argv).lower()
        assert "start-all" not in joined
        assert "cmd" not in Path(argv[0]).name.lower()


def test_the_counter_is_pointed_at_the_configured_station():
    values = _values(AIS_STATION_HOST="10.0.0.5", AIS_STATION_HTTP_PORT="8200",
                     AIS_STATION_NMEA_PORT="10222")
    paths = registry.resolve_paths(values, _SERVER_DIR)
    argv = registry.argv_for(registry.BY_NAME["counter"], values, paths)
    assert "--station" in argv and "10.0.0.5:8200" in argv
    assert argv[argv.index("--port") + 1] == "10222"
    assert Path(argv[argv.index("--log") + 1]).parent == paths.log_dir


def test_the_proxy_declares_the_port_it_must_own_and_the_counter_does_not():
    """The counter connects out; it listens on nothing, so clearing a port for it would be
    both meaningless and dangerous."""
    assert registry.port_for(registry.BY_NAME["proxy"], _values(PROXY_PORT="9001")) == 9001
    assert registry.port_for(registry.BY_NAME["counter"], _values()) is None


def test_a_disabled_process_reports_itself_disabled():
    assert registry.is_enabled(registry.BY_NAME["counter"], _values(COUNTER_ENABLED="on"))
    assert not registry.is_enabled(registry.BY_NAME["counter"], _values(COUNTER_ENABLED="off"))


def test_the_child_environment_carries_the_secrets_and_not_the_app_settings():
    env = registry.env_for(registry.BY_NAME["proxy"],
                           _values(GROQ_API_KEY="gsk_test", WEBAPP_BIND_HOST="0.0.0.0"))
    assert env["GROQ_API_KEY"] == "gsk_test"
    assert "WEBAPP_BIND_HOST" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


def test_the_log_directory_defaults_to_server_logs_and_is_created():
    paths = registry.resolve_paths(_values(), _SERVER_DIR)
    assert paths.log_dir == _SERVER_DIR / "logs"


def test_an_explicit_log_directory_is_honoured(tmp_path):
    paths = registry.resolve_paths(_values(LOG_DIR=str(tmp_path)), _SERVER_DIR)
    assert paths.log_dir == tmp_path
```

- [x] **Step 2: Run it to verify it fails**

Run: `py -m pytest server/tests/test_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'registry' from 'webapp'`.

- [x] **Step 3: Implement**

```python
# server/webapp/registry.py
"""What the control panel manages, and how each command line is built from config.

A registry rather than a handler per process: AIS-catcher becomes one more entry when the
miniPC arrives, and the counter is expected to be retired by a flag rather than by deletion.

Nothing here shells out to start-all.bat. That was proven impossible on 2026-08-18 -- `start`
needs an interactive window station, which a detached or service parent does not have -- and
it is also the wrong shape: the panel owns the environment, so it must own the command line.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from webapp.env_builder import build_env


@dataclass(frozen=True)
class Paths:
    server_dir: Path
    log_dir: Path


def resolve_paths(values: dict[str, str], server_dir: Path) -> Paths:
    configured = (values.get("LOG_DIR") or "").strip()
    return Paths(server_dir=Path(server_dir),
                 log_dir=Path(configured) if configured else Path(server_dir) / "logs")


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    label: str
    log_prefix: str
    image_name: str
    enabled_key: str
    port_key: str | None
    build_argv: Callable[[dict[str, str], Paths], list[str]]
    description: str


def _proxy_argv(values: dict[str, str], paths: Paths) -> list[str]:
    return [sys.executable, str(paths.server_dir / "whisper-proxy.py")]


def _counter_argv(values: dict[str, str], paths: Paths) -> list[str]:
    host = (values.get("AIS_STATION_HOST") or "").strip()
    http_port = (values.get("AIS_STATION_HTTP_PORT") or "").strip()
    return [
        sys.executable, str(paths.server_dir / "ais_station_count.py"),
        "--station", f"{host}:{http_port}",
        "--port", (values.get("AIS_STATION_NMEA_PORT") or "").strip(),
        "--log", str(paths.log_dir / "ais-station-count.jsonl"),
    ]


PROCESSES: tuple[ProcessSpec, ...] = (
    ProcessSpec(
        name="proxy", label="Whisper proxy", log_prefix="proxy",
        image_name=Path(sys.executable).name, enabled_key="PROXY_ENABLED",
        port_key="PROXY_PORT", build_argv=_proxy_argv,
        description="Transcribes what the plugin sends, resolves conversations, and serves "
                    "the identified-vessels page.",
    ),
    ProcessSpec(
        name="counter", label="AIS station counter", log_prefix="counter",
        image_name=Path(sys.executable).name, enabled_key="COUNTER_ENABLED",
        port_key=None,
        build_argv=_counter_argv,
        description="Counts distinct vessels per hour from the local AIS receiver. It "
                    "connects out and listens on nothing.",
    ),
)

BY_NAME: dict[str, ProcessSpec] = {spec.name: spec for spec in PROCESSES}


def argv_for(spec: ProcessSpec, values: dict[str, str], paths: Paths) -> list[str]:
    return spec.build_argv(values, paths)


def env_for(spec: ProcessSpec, values: dict[str, str]) -> dict[str, str]:
    """The child's environment. Unbuffered, because its stdout is a log file being tailed:
    a buffered child would show nothing for minutes and look hung."""
    env = build_env(values)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def port_for(spec: ProcessSpec, values: dict[str, str]) -> int | None:
    if spec.port_key is None:
        return None
    try:
        return int((values.get(spec.port_key) or "").strip())
    except ValueError:
        return None


def is_enabled(spec: ProcessSpec, values: dict[str, str]) -> bool:
    return (values.get(spec.enabled_key) or "on").strip().lower() != "off"
```

- [x] **Step 4: Run the test**

Run: `py -m pytest server/tests/test_registry.py -v`
Expected: PASS, 8 tests.

- [x] **Step 5: Commit**

```bash
git add server/webapp/registry.py server/tests/test_registry.py
git commit -m "Describe the managed processes as a registry rather than two handlers"
```

---

### Task 8: The supervisor

**Files:**
- Create: `server/webapp/supervisor.py`
- Create: `server/tests/test_supervisor.py`
- Create: `server/tests/fake_child.py`

**Interfaces:**
- Consumes: `webapp.registry` (`ProcessSpec`, `Paths`, `argv_for`, `env_for`, `port_for`, `is_enabled`), `webapp.ports` (`clear_port`, `pid_listening_on`, `PortHeldByStranger`).
- Produces:
  - `ProcessState` (pydantic model: `name`, `label`, `description`, `enabled: bool`, `state: str` — one of `running`, `stopped`, `disabled`; `pid: int | None`, `started_at: float | None`, `uptime_sec: float | None`, `port: int | None`, `port_ok: bool | None`, `log_file: str | None`)
  - `Supervisor(paths: Paths, load_values: Callable[[], dict[str, str]])` with `status(name) -> ProcessState`, `status_all() -> list[ProcessState]`, `start(name) -> ProcessState`, `stop(name) -> ProcessState`, `restart(name) -> ProcessState`, `log_path(name) -> Path | None`
  - `SupervisorError(Exception)`, `AlreadyRunning(SupervisorError)`, `NotRunning(SupervisorError)`, `Disabled(SupervisorError)`, `StartFailed(SupervisorError)`

Three failures this project has already had are what this task's tests are about: a child dying with its console window, a zombie holding a port, and a pid file adopted by an unrelated process after pid reuse. The pid file therefore records the image name **and** the process creation time; both must match on reattachment.

- [x] **Step 1: Write the fake child and the failing tests**

```python
# server/tests/fake_child.py
"""A deterministic stand-in for the proxy: prints a banner, optionally binds a port, sleeps.

Real children are not used in supervisor tests -- the proxy talks to Groq and to an AIS feed,
and the counter opens a TCP connection to another machine. This one only does what the
supervisor can observe.
"""
import socket
import sys
import time

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print("fake child started", flush=True)
    held = None
    if port:
        held = socket.socket()
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", port))
        held.listen()
        print(f"listening on {port}", flush=True)
    time.sleep(300)
```

```python
# server/tests/test_supervisor.py
"""Starting, stopping and re-finding detached children.

The three failures under test are ones this project has actually had: output dying with a
console window, a zombie holding a port so that "restart" silently does nothing, and a pid
file adopted by an unrelated process after pid reuse.
"""
import json
import os
import socket
import sys
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store  # noqa: E402
from webapp.registry import Paths, ProcessSpec  # noqa: E402
from webapp.supervisor import AlreadyRunning, Disabled, NotRunning, Supervisor  # noqa: E402

_FAKE = Path(__file__).resolve().parent / "fake_child.py"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _spec(port_key: str | None = None) -> ProcessSpec:
    return ProcessSpec(
        name="fake", label="Fake child", log_prefix="fake",
        image_name=Path(sys.executable).name, enabled_key="COUNTER_ENABLED",
        port_key=port_key,
        build_argv=lambda values, paths: (
            [sys.executable, str(_FAKE)]
            + ([values["PROXY_PORT"]] if port_key else [])),
        description="a stand-in",
    )


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    """A supervisor over one fake process, writing everything under tmp_path."""
    from webapp import registry, supervisor as supervisor_module

    values = config_store.load(tmp_path / "absent.json")
    monkeypatch.setattr(registry, "BY_NAME", {"fake": _spec()}, raising=False)
    monkeypatch.setattr(registry, "PROCESSES", (registry.BY_NAME["fake"],), raising=False)
    monkeypatch.setattr(supervisor_module.registry, "BY_NAME", registry.BY_NAME, raising=False)
    monkeypatch.setattr(supervisor_module.registry, "PROCESSES", registry.PROCESSES,
                        raising=False)
    paths = Paths(server_dir=_SERVER_DIR, log_dir=tmp_path / "logs")
    sup = Supervisor(paths=paths, load_values=lambda: dict(values))
    yield sup
    for state in sup.status_all():
        if state.state == "running":
            sup.stop(state.name)


def test_a_stopped_process_reports_stopped(supervisor):
    state = supervisor.status("fake")
    assert state.state == "stopped"
    assert state.pid is None


def test_starting_writes_a_pid_file_and_a_log_the_child_actually_wrote_to(supervisor):
    state = supervisor.start("fake")
    assert state.state == "running"
    assert state.pid

    log = Path(state.log_file)
    deadline = time.time() + 10
    while time.time() < deadline and "fake child started" not in log.read_text(encoding="utf-8"):
        time.sleep(0.1)
    assert "fake child started" in log.read_text(encoding="utf-8")


def test_a_second_start_is_refused_rather_than_starting_a_twin(supervisor):
    supervisor.start("fake")
    with pytest.raises(AlreadyRunning):
        supervisor.start("fake")


def test_a_fresh_supervisor_reattaches_to_the_running_child(supervisor, tmp_path):
    """The panel restarting must never orphan a capture run."""
    started = supervisor.start("fake")
    reborn = Supervisor(paths=supervisor.paths, load_values=supervisor.load_values)
    state = reborn.status("fake")
    assert state.state == "running"
    assert state.pid == started.pid
    assert state.uptime_sec >= 0


def test_a_pid_file_naming_an_unrelated_process_is_not_adopted(supervisor):
    """Pid reuse. The pid is alive and is even the right image -- it is this test runner --
    but it was not started by us, and its creation time proves it."""
    supervisor.paths.log_dir.mkdir(parents=True, exist_ok=True)
    (supervisor.paths.log_dir / "fake.pid").write_text(json.dumps({
        "pid": os.getpid(),
        "image": Path(sys.executable).name,
        "create_time": 1.0,
        "started_at": 1.0,
        "log": str(supervisor.paths.log_dir / "fake-2026-01-01.log"),
    }), encoding="utf-8")
    assert supervisor.status("fake").state == "stopped"


def test_stopping_ends_the_process_and_clears_the_pid_file(supervisor):
    supervisor.start("fake")
    state = supervisor.stop("fake")
    assert state.state == "stopped"
    assert not (supervisor.paths.log_dir / "fake.pid").exists()
    with pytest.raises(NotRunning):
        supervisor.stop("fake")


def test_restart_leaves_a_different_process_running(supervisor):
    first = supervisor.start("fake")
    second = supervisor.restart("fake")
    assert second.state == "running"
    assert second.pid != first.pid


def test_restart_frees_the_port_before_binding_it_again(tmp_path, monkeypatch):
    """Without this the second child binds alongside the first (SO_REUSEADDR) and the
    original runs on as a zombie -- "restart" would be a quiet no-op."""
    from webapp import registry, supervisor as supervisor_module

    port = _free_port()
    values = config_store.load(tmp_path / "absent.json")
    values["PROXY_PORT"] = str(port)
    monkeypatch.setattr(supervisor_module.registry, "BY_NAME",
                        {"fake": _spec(port_key="PROXY_PORT")}, raising=False)
    monkeypatch.setattr(supervisor_module.registry, "PROCESSES",
                        (supervisor_module.registry.BY_NAME["fake"],), raising=False)
    sup = Supervisor(paths=Paths(server_dir=_SERVER_DIR, log_dir=tmp_path / "logs"),
                     load_values=lambda: dict(values))
    try:
        first = sup.start("fake")
        deadline = time.time() + 10
        from webapp.ports import pid_listening_on
        while time.time() < deadline and pid_listening_on(port) != first.pid:
            time.sleep(0.1)
        assert pid_listening_on(port) == first.pid

        second = sup.restart("fake")
        deadline = time.time() + 10
        while time.time() < deadline and pid_listening_on(port) != second.pid:
            time.sleep(0.1)
        assert pid_listening_on(port) == second.pid
        assert second.pid != first.pid
    finally:
        if sup.status("fake").state == "running":
            sup.stop("fake")


def test_a_disabled_process_is_neither_startable_nor_shown_as_failed(tmp_path, monkeypatch):
    from webapp import supervisor as supervisor_module

    values = config_store.load(tmp_path / "absent.json")
    values["COUNTER_ENABLED"] = "off"
    monkeypatch.setattr(supervisor_module.registry, "BY_NAME", {"fake": _spec()}, raising=False)
    monkeypatch.setattr(supervisor_module.registry, "PROCESSES",
                        (supervisor_module.registry.BY_NAME["fake"],), raising=False)
    sup = Supervisor(paths=Paths(server_dir=_SERVER_DIR, log_dir=tmp_path / "logs"),
                     load_values=lambda: dict(values))
    assert sup.status("fake").state == "disabled"
    with pytest.raises(Disabled):
        sup.start("fake")
```

- [x] **Step 2: Run them to verify they fail**

Run: `py -m pytest server/tests/test_supervisor.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.supervisor'`.

- [x] **Step 3: Implement**

```python
# server/webapp/supervisor.py
"""Start, stop and re-find detached child processes.

Three failures this project has already had shape every decision here:

  - The proxy has run under `cmd /k`, so its output died with the window. Children get stdout
    redirected to a dated file, which on a headless box is the only record there is.
  - A second proxy can bind :9000 alongside the first (SO_REUSEADDR) and silently take over
    while the original runs on as a zombie, so a declared port is cleared before every start.
  - A pid outlives its process and gets reused, so the pid file records the image name AND the
    process creation time, and both must match before a pid is adopted.
"""
from __future__ import annotations

import datetime
import json
import os
import subprocess
import time
from collections.abc import Callable
from pathlib import Path

import psutil
from pydantic import BaseModel

from webapp import registry
from webapp.ports import PortHeldByStranger, clear_port, pid_listening_on
from webapp.registry import Paths, ProcessSpec

_DETACHED = (
    subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    if os.name == "nt" else 0
)


class SupervisorError(Exception):
    """Base for every refusal, so a route can turn them into one 409 with a real message."""


class AlreadyRunning(SupervisorError):
    pass


class NotRunning(SupervisorError):
    pass


class Disabled(SupervisorError):
    pass


class StartFailed(SupervisorError):
    """The child exited immediately. Carries its first lines of output, never "failed to
    start" -- the operator may be on a phone with no other way to see them."""


class ProcessState(BaseModel):
    name: str
    label: str
    description: str
    enabled: bool
    state: str
    pid: int | None = None
    started_at: float | None = None
    uptime_sec: float | None = None
    port: int | None = None
    port_ok: bool | None = None
    log_file: str | None = None


class Supervisor:
    def __init__(self, paths: Paths, load_values: Callable[[], dict[str, str]]):
        self.paths = paths
        self.load_values = load_values

    # -- pid files ---------------------------------------------------------

    def _pid_file(self, spec: ProcessSpec) -> Path:
        return self.paths.log_dir / f"{spec.name}.pid"

    def _read_pid_file(self, spec: ProcessSpec) -> dict | None:
        try:
            raw = json.loads(self._pid_file(spec).read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        return raw if isinstance(raw, dict) else None

    def _live_process(self, record: dict) -> psutil.Process | None:
        """The recorded process, or None if it is gone or the pid now belongs to someone else.

        Creation time is the part that makes this safe: a reused pid can be alive and even be
        the same image, and only the start time distinguishes it from what we started.
        """
        try:
            proc = psutil.Process(int(record.get("pid", 0)))
            if proc.name().lower() != str(record.get("image", "")).lower():
                return None
            if abs(proc.create_time() - float(record.get("create_time", -1))) > 1.0:
                return None
            return proc
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError, TypeError):
            return None

    # -- status ------------------------------------------------------------

    def status(self, name: str) -> ProcessState:
        spec = registry.BY_NAME[name]
        values = self.load_values()
        enabled = registry.is_enabled(spec, values)
        port = registry.port_for(spec, values)
        record = self._read_pid_file(spec)
        proc = self._live_process(record) if record else None

        if proc is None:
            if record is not None:
                self._pid_file(spec).unlink(missing_ok=True)
            return ProcessState(
                name=spec.name, label=spec.label, description=spec.description,
                enabled=enabled, state="disabled" if not enabled else "stopped",
                port=port, log_file=self._latest_log(spec))

        started_at = float(record.get("started_at", proc.create_time()))
        return ProcessState(
            name=spec.name, label=spec.label, description=spec.description,
            enabled=enabled, state="running", pid=proc.pid, started_at=started_at,
            uptime_sec=max(0.0, time.time() - started_at), port=port,
            port_ok=None if port is None else pid_listening_on(port) == proc.pid,
            log_file=record.get("log") or self._latest_log(spec))

    def status_all(self) -> list[ProcessState]:
        return [self.status(spec.name) for spec in registry.PROCESSES]

    # -- logs --------------------------------------------------------------

    def _log_for_today(self, spec: ProcessSpec) -> Path:
        stamp = datetime.date.today().isoformat()
        return self.paths.log_dir / f"{spec.log_prefix}-{stamp}.log"

    def _latest_log(self, spec: ProcessSpec) -> str | None:
        found = sorted(self.paths.log_dir.glob(f"{spec.log_prefix}-*.log"))
        return str(found[-1]) if found else None

    def log_path(self, name: str) -> Path | None:
        state = self.status(name)
        return Path(state.log_file) if state.log_file else None

    # -- lifecycle ---------------------------------------------------------

    def start(self, name: str) -> ProcessState:
        spec = registry.BY_NAME[name]
        values = self.load_values()
        if not registry.is_enabled(spec, values):
            raise Disabled(f"{spec.label} is disabled in the settings")
        if self.status(name).state == "running":
            raise AlreadyRunning(f"{spec.label} is already running")

        self.paths.log_dir.mkdir(parents=True, exist_ok=True)
        port = registry.port_for(spec, values)
        if port is not None:
            try:
                clear_port(port, {spec.image_name})
            except PortHeldByStranger as exc:
                raise SupervisorError(str(exc)) from None

        argv = registry.argv_for(spec, values, self.paths)
        env = registry.env_for(spec, values)
        log = self._log_for_today(spec)
        handle = open(log, "ab", buffering=0)
        try:
            proc = subprocess.Popen(
                argv, cwd=str(self.paths.server_dir), env=env,
                stdout=handle, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                creationflags=_DETACHED, close_fds=True)
        finally:
            handle.close()

        started_at = time.time()
        time.sleep(0.4)
        if proc.poll() is not None:
            tail = ""
            try:
                tail = log.read_text(encoding="utf-8", errors="replace")[-2000:]
            except OSError:
                pass
            raise StartFailed(
                f"{spec.label} exited immediately with code {proc.returncode}.\n{tail}")

        try:
            create_time = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            create_time = started_at
        self._pid_file(spec).write_text(json.dumps({
            "pid": proc.pid,
            "image": Path(argv[0]).name,
            "create_time": create_time,
            "started_at": started_at,
            "log": str(log),
            "argv": argv,
        }), encoding="utf-8")
        return self.status(name)

    def stop(self, name: str) -> ProcessState:
        spec = registry.BY_NAME[name]
        record = self._read_pid_file(spec)
        proc = self._live_process(record) if record else None
        if proc is None:
            self._pid_file(spec).unlink(missing_ok=True)
            raise NotRunning(f"{spec.label} is not running")
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except psutil.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
        except psutil.NoSuchProcess:
            pass
        self._pid_file(spec).unlink(missing_ok=True)
        return self.status(name)

    def restart(self, name: str) -> ProcessState:
        try:
            self.stop(name)
        except NotRunning:
            pass
        return self.start(name)
```

- [x] **Step 4: Run the tests**

Run: `py -m pytest server/tests/test_supervisor.py -v`
Expected: PASS, 9 tests. They start real detached children; if one leaks, `py -c "import psutil;[p.kill() for p in psutil.process_iter(['cmdline']) if p.info['cmdline'] and 'fake_child.py' in ' '.join(p.info['cmdline'])]"` clears them.

- [x] **Step 5: Run the full suite**

Run: `py -m pytest server/tests`
Expected: all pass.

- [x] **Step 6: Commit**

```bash
git add server/webapp/supervisor.py server/tests/test_supervisor.py server/tests/fake_child.py
git commit -m "Supervise detached children: dated logs, cleared ports, pid files that survive pid reuse"
```

---

### Task 9: Reading a log without shipping the whole day

**Files:**
- Create: `server/webapp/logs.py`
- Create: `server/tests/test_logs.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TailWindow` (pydantic model: `path: str`, `offset: int`, `next_offset: int`, `size: int`, `text: str`, `restarted: bool`), `read_tail(path: Path, offset: int | None = None, limit: int = 65_536) -> TailWindow`, `latest_log(log_dir: Path, prefix: str) -> Path | None`, `MAX_LIMIT = 262_144`.

Tailing now crosses a network, so it reads a bounded byte range and the client comes back with `next_offset`. `offset=None` means "the end minus `limit`", which is the first view. A file that shrank was rotated or truncated, so the reader restarts at zero and says so rather than returning nonsense.

- [x] **Step 1: Write the failing test**

```python
# server/tests/test_logs.py
"""Bounded log reads. Log tailing crosses a network now; shipping a whole day per refresh
is not an option, and neither is a client that silently loses its place after rotation."""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.logs import latest_log, read_tail  # noqa: E402


def test_the_first_read_returns_the_end_of_the_file(tmp_path):
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_bytes(b"x" * 1000 + b"the end\n")
    window = read_tail(log, offset=None, limit=16)
    assert window.text.endswith("the end\n")
    assert window.next_offset == log.stat().st_size


def test_reading_from_an_offset_returns_only_what_was_appended(tmp_path):
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_text("first\n", encoding="utf-8")
    first = read_tail(log, offset=0)
    log.write_text("first\nsecond\n", encoding="utf-8")

    window = read_tail(log, offset=first.next_offset)
    assert window.text == "second\n"
    assert window.restarted is False


def test_a_truncated_or_rotated_file_restarts_at_the_beginning(tmp_path):
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_text("a long first day\n", encoding="utf-8")
    stale = read_tail(log, offset=0).next_offset
    log.write_text("new\n", encoding="utf-8")

    window = read_tail(log, offset=stale)
    assert window.restarted is True
    assert window.text == "new\n"
    assert window.offset == 0


def test_a_read_is_bounded_however_large_the_file_and_the_limit(tmp_path):
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_bytes(b"y" * 2_000_000)
    window = read_tail(log, offset=0, limit=10_000_000)
    assert len(window.text) <= 262_144
    assert window.next_offset < window.size


def test_a_missing_log_reads_as_empty_rather_than_raising(tmp_path):
    window = read_tail(tmp_path / "never-written.log")
    assert window.text == ""
    assert window.size == 0


def test_undecodable_bytes_do_not_break_a_read(tmp_path):
    """The proxy prints vessel names from AIS, which have arrived mis-encoded before."""
    log = tmp_path / "proxy-2026-08-18.log"
    log.write_bytes(b"before \xff\xfe after\n")
    assert "before" in read_tail(log, offset=0).text


def test_the_latest_log_is_the_most_recent_dated_file(tmp_path):
    for stamp in ("2026-08-16", "2026-08-18", "2026-08-17"):
        (tmp_path / f"proxy-{stamp}.log").write_text("x", encoding="utf-8")
    (tmp_path / "counter-2026-08-19.log").write_text("x", encoding="utf-8")
    assert latest_log(tmp_path, "proxy").name == "proxy-2026-08-18.log"
    assert latest_log(tmp_path, "nothing") is None
```

- [x] **Step 2: Run it to verify it fails**

Run: `py -m pytest server/tests/test_logs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.logs'`.

- [x] **Step 3: Implement**

```python
# server/webapp/logs.py
"""Bounded reads of a growing log file.

The UI polls with the offset it last received, so a refresh costs only what was appended.
A file that shrank was rotated or truncated; the reader restarts at zero and says so, rather
than seeking past the end and returning nothing forever.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

MAX_LIMIT = 262_144
DEFAULT_LIMIT = 65_536


class TailWindow(BaseModel):
    path: str
    offset: int
    next_offset: int
    size: int
    text: str
    restarted: bool = False


def latest_log(log_dir: Path, prefix: str) -> Path | None:
    """The newest dated log for a process. Names sort chronologically by construction."""
    found = sorted(Path(log_dir).glob(f"{prefix}-*.log"))
    return found[-1] if found else None


def read_tail(path: Path, offset: int | None = None, limit: int = DEFAULT_LIMIT) -> TailWindow:
    path = Path(path)
    limit = max(1, min(int(limit), MAX_LIMIT))
    try:
        size = path.stat().st_size
    except OSError:
        return TailWindow(path=str(path), offset=0, next_offset=0, size=0, text="")

    restarted = False
    if offset is None:
        start = max(0, size - limit)
    elif offset > size:
        start, restarted = 0, True
    else:
        start = max(0, int(offset))

    try:
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(limit)
    except OSError:
        return TailWindow(path=str(path), offset=start, next_offset=start, size=size, text="")

    return TailWindow(
        path=str(path), offset=start, next_offset=start + len(chunk), size=size,
        # errors="replace" because AIS vessel names have arrived mis-encoded before, and a
        # log viewer that dies on one bad byte is worse than one that shows a box.
        text=chunk.decode("utf-8", errors="replace"), restarted=restarted)
```

- [x] **Step 4: Run the test**

Run: `py -m pytest server/tests/test_logs.py -v`
Expected: PASS, 7 tests.

- [x] **Step 5: Commit**

```bash
git add server/webapp/logs.py server/tests/test_logs.py
git commit -m "Read logs as bounded byte ranges, surviving rotation and bad bytes"
```

---

### Task 10: Health — do the paths resolve, and is anything still arriving

**Files:**
- Create: `server/webapp/health.py`
- Create: `server/tests/test_health.py`

**Interfaces:**
- Consumes: `webapp.settings_schema` (`SETTINGS`, `SettingType`), `webapp.registry.Paths`.
- Produces: `PathCheck` (pydantic: `key`, `label`, `value`, `resolves: bool`), `Health` (pydantic: `paths: list[PathCheck]`, `proxy: dict`, `proxy_error: str | None`), `path_checks(values, paths) -> list[PathCheck]`, `proxy_status(values, fetch=None) -> tuple[dict, str | None]`, `health(values, paths, fetch=None) -> Health`.

`fetch` is injected so tests never open a socket. When the proxy is down the panel says so in words — the spec is explicit that an empty table must never stand in for "the source is gone".

- [x] **Step 1: Write the failing test**

```python
# server/tests/test_health.py
"""Does this machine have the paths the settings name, and is the proxy answering?

Both questions exist for the same reason: a host migration that half-worked must be visible
immediately, not at the next transmission.
"""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store, health  # noqa: E402
from webapp.registry import Paths  # noqa: E402


def _values(**overrides):
    values = config_store.load(Path("does-not-exist.json"))
    values.update(overrides)
    return values


def test_a_path_that_exists_and_one_that_does_not_are_both_reported(tmp_path):
    real = tmp_path / "here"
    real.mkdir()
    checks = health.path_checks(
        _values(SDRSHARP_DIR=str(real), CAPTURES_DIR=str(tmp_path / "gone")),
        Paths(server_dir=_SERVER_DIR, log_dir=tmp_path))
    by_key = {check.key: check for check in checks}
    assert by_key["SDRSHARP_DIR"].resolves is True
    assert by_key["CAPTURES_DIR"].resolves is False


def test_an_empty_path_setting_is_not_reported_as_broken(tmp_path):
    """Empty means "use the built-in default", which is a working configuration."""
    checks = health.path_checks(_values(CONVERSATIONS_FILE=""),
                                Paths(server_dir=_SERVER_DIR, log_dir=tmp_path))
    assert all(check.key != "CONVERSATIONS_FILE" for check in checks)


def test_the_proxy_status_is_passed_through_when_it_answers():
    payload = {"stt_backend": "groq", "ais_cache_size": 1694, "last_chunk_at": 1.0,
               "now": 61.0, "conversations": 12, "ais_source": "aishub",
               "ais_last_poll_at": 2.0, "started_at": 0.0}
    result, error = health.proxy_status(_values(), fetch=lambda url, timeout: payload)
    assert result["ais_cache_size"] == 1694
    assert error is None


def test_a_proxy_that_is_down_is_said_plainly_rather_than_shown_as_empty():
    def _refuse(url, timeout):
        raise ConnectionRefusedError("nobody home")

    result, error = health.proxy_status(_values(), fetch=_refuse)
    assert result == {}
    assert "not answering" in error.lower()


def test_the_proxy_is_asked_on_loopback_at_the_configured_port():
    seen = {}

    def _record(url, timeout):
        seen["url"] = url
        return {}

    health.proxy_status(_values(PROXY_PORT="9100"), fetch=_record)
    assert seen["url"] == "http://127.0.0.1:9100/api/status"
```

- [x] **Step 2: Run it to verify it fails**

Run: `py -m pytest server/tests/test_health.py -v`
Expected: FAIL — `ImportError: cannot import name 'health' from 'webapp'`.

- [x] **Step 3: Implement**

```python
# server/webapp/health.py
"""Two questions the dashboard must answer: do the configured paths exist here, and is the
proxy still hearing anything.

The proxy is asked over HTTP, server-side, rather than the browser asking it directly: that
sidesteps CORS, keeps the proxy the single source of live truth, and means the browser needs
to reach only one host.
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from webapp.registry import Paths
from webapp.settings_schema import SETTINGS, SettingType

TIMEOUT_SEC = 2.0


class PathCheck(BaseModel):
    key: str
    label: str
    value: str
    resolves: bool


class Health(BaseModel):
    paths: list[PathCheck]
    proxy: dict
    proxy_error: str | None = None


def path_checks(values: dict[str, str], paths: Paths) -> list[PathCheck]:
    """Every PATH setting that names something, plus the log directory.

    An empty PATH setting means "use the built-in default" and is a working configuration,
    so it is not listed -- a red mark against a setting nobody set would train the operator
    to ignore the strip.
    """
    checks = [
        PathCheck(key=spec.key, label=spec.group, value=(values.get(spec.key) or "").strip(),
                  resolves=Path((values.get(spec.key) or "").strip()).exists())
        for spec in SETTINGS
        if spec.type is SettingType.PATH and spec.key != "LOG_DIR"
        and (values.get(spec.key) or "").strip()
    ]
    # LOG_DIR is appended below with its RESOLVED value -- empty means server/logs, which is a
    # real directory the panel must still be able to write to.
    checks.append(PathCheck(key="LOG_DIR", label="Paths", value=str(paths.log_dir),
                            resolves=paths.log_dir.exists()))
    return checks


def _fetch_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def proxy_status(values: dict[str, str],
                 fetch: Callable[[str, float], dict] | None = None) -> tuple[dict, str | None]:
    """(payload, error). Loopback by name, because the proxy binds 0.0.0.0 and the panel is
    on the same machine by definition -- it is the thing that started it."""
    port = (values.get("PROXY_PORT") or "9000").strip()
    url = f"http://127.0.0.1:{port}/api/status"
    getter = fetch or _fetch_json
    try:
        payload = getter(url, TIMEOUT_SEC)
    except Exception as exc:
        return {}, f"the proxy is not answering on {url} ({type(exc).__name__})"
    return (payload if isinstance(payload, dict) else {}), None


def health(values: dict[str, str], paths: Paths,
           fetch: Callable[[str, float], dict] | None = None) -> Health:
    payload, error = proxy_status(values, fetch=fetch)
    return Health(paths=path_checks(values, paths), proxy=payload, proxy_error=error)
```

- [x] **Step 4: Run the test**

Run: `py -m pytest server/tests/test_health.py -v`
Expected: PASS, 5 tests.

- [x] **Step 5: Commit**

```bash
git add server/webapp/health.py server/tests/test_health.py
git commit -m "Answer whether the configured paths resolve and whether the proxy still hears anything"
```

---

### Task 11: The app — routes, the auth guard, and the CSRF guard

**Files:**
- Create: `server/webapp/app.py`
- Create: `server/webapp/startup.py`
- Create: `server/webapp/__main__.py`
- Create: `server/tests/test_app_auth.py`
- Create: `server/tests/test_app_routes.py`

**Interfaces:**
- Consumes: `webapp.auth`, `webapp.credentials`, `webapp.config_store`, `webapp.registry`, `webapp.supervisor`, `webapp.logs`, `webapp.health`.
- Produces: `create_app(*, server_dir: Path, config_path: Path, credentials_path: Path, supervisor=None) -> FastAPI` and, on it, these routes:

| method | path | auth | body / query |
|---|---|---|---|
| POST | `/api/login` | no | `{"password": str}` → `{"csrf_token": str}` |
| POST | `/api/logout` | session + CSRF | — |
| GET | `/api/session` | no | → `{"authenticated": bool, "csrf_token": str \| null, "password_set": bool}` |
| GET | `/api/processes` | session | → `{"processes": [ProcessState]}` |
| POST | `/api/processes/{name}/start` | session + CSRF | → `ProcessState` |
| POST | `/api/processes/{name}/stop` | session + CSRF | → `ProcessState` |
| POST | `/api/processes/{name}/restart` | session + CSRF | → `ProcessState` |
| GET | `/api/logs/{name}` | session | `?offset=&limit=` → `TailWindow` |
| GET | `/api/health` | session | → `Health` |

Plus `startup.check_and_run()` and `startup.main()`.

- [x] **Step 1: Write the failing auth tests**

```python
# server/tests/test_app_auth.py
"""The test that keeps Section 3 true as routes are added: every mutating route, enumerated
from the app itself, must reject a request that carries no session."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import credentials  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.auth import COOKIE_NAME, CSRF_HEADER  # noqa: E402

PASSWORD = "a long enough password"


@pytest.fixture
def client(tmp_path):
    credentials.save_password(tmp_path / "credentials.json", PASSWORD)
    app = create_app(server_dir=_SERVER_DIR,
                     config_path=tmp_path / "config.json",
                     credentials_path=tmp_path / "credentials.json")
    with TestClient(app) as test_client:
        yield test_client


def _login(client) -> str:
    response = client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def test_every_mutating_route_rejects_a_request_with_no_session(client):
    """Enumerated from app.routes rather than listed by hand, so a route added later without
    a guard fails here instead of shipping open."""
    checked = 0
    for route in client.app.routes:
        methods = getattr(route, "methods", set()) - {"HEAD", "OPTIONS"}
        path = getattr(route, "path", "")
        if not methods & {"POST", "PUT", "PATCH", "DELETE"} or path == "/api/login":
            continue
        url = path.replace("{name}", "proxy")
        for method in methods & {"POST", "PUT", "PATCH", "DELETE"}:
            response = client.request(method, url)
            assert response.status_code in (401, 403), f"{method} {url} answered {response.status_code}"
            checked += 1
    assert checked >= 3, "the enumeration found no mutating routes -- it has stopped working"


def test_every_reading_route_rejects_a_request_with_no_session(client):
    for url in ("/api/processes", "/api/health", "/api/logs/proxy"):
        assert client.get(url).status_code == 401


def test_the_session_probe_and_login_are_reachable_without_a_session(client):
    assert client.get("/api/session").status_code == 200
    assert client.get("/api/session").json()["authenticated"] is False
    assert client.get("/api/session").json()["password_set"] is True


def test_a_correct_password_opens_a_session_and_a_wrong_one_does_not(client):
    assert client.post("/api/login", json={"password": "wrong password entirely"}).status_code == 401
    csrf = _login(client)
    assert client.cookies.get(COOKIE_NAME)
    assert client.get("/api/processes").status_code == 200
    assert csrf


def test_the_session_cookie_is_httponly_and_samesite_strict(client):
    response = client.post("/api/login", json={"password": PASSWORD})
    header = response.headers["set-cookie"].lower()
    assert "httponly" in header
    assert "samesite=strict" in header


def test_the_cookie_is_marked_secure_only_behind_a_tls_terminator(client):
    plain = client.post("/api/login", json={"password": PASSWORD})
    assert "secure" not in plain.headers["set-cookie"].lower()
    client.cookies.clear()
    forwarded = client.post("/api/login", json={"password": PASSWORD},
                            headers={"X-Forwarded-Proto": "https"})
    assert "secure" in forwarded.headers["set-cookie"].lower()


def test_a_mutating_route_needs_the_csrf_token_as_well_as_the_cookie(client):
    csrf = _login(client)
    assert client.post("/api/processes/proxy/stop").status_code == 403
    assert client.post("/api/processes/proxy/stop",
                       headers={CSRF_HEADER: "not-the-token"}).status_code == 403
    # The right token gets past the guard; whether the proxy was running is another matter.
    assert client.post("/api/processes/proxy/stop",
                       headers={CSRF_HEADER: csrf}).status_code in (200, 409)


def test_logging_out_invalidates_the_session(client):
    csrf = _login(client)
    assert client.post("/api/logout", headers={CSRF_HEADER: csrf}).status_code == 200
    assert client.get("/api/processes").status_code == 401


def test_repeated_wrong_passwords_are_throttled(client):
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong password entirely"}).status_code == 401
    assert client.post("/api/login", json={"password": "wrong password entirely"}).status_code == 429
    # And the correct password is refused too while the lockout stands -- otherwise the
    # throttle would only slow down an attacker who never guesses right.
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_with_no_password_configured_the_panel_says_so_and_refuses_every_login(tmp_path):
    app = create_app(server_dir=_SERVER_DIR, config_path=tmp_path / "config.json",
                     credentials_path=tmp_path / "credentials.json")
    with TestClient(app) as client:
        assert client.get("/api/session").json()["password_set"] is False
        assert client.post("/api/login", json={"password": "anything at all"}).status_code == 401
```

- [x] **Step 2: Write the failing route tests**

```python
# server/tests/test_app_routes.py
"""The routes, over a supervisor that records calls instead of starting anything."""
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import credentials  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.auth import CSRF_HEADER  # noqa: E402
from webapp.supervisor import AlreadyRunning, ProcessState, StartFailed  # noqa: E402

PASSWORD = "a long enough password"


class _FakeSupervisor:
    def __init__(self, log_dir: Path):
        self.calls: list[str] = []
        self.paths = type("P", (), {"log_dir": log_dir, "server_dir": _SERVER_DIR})()
        self.raise_on_start: Exception | None = None

    def _state(self, name: str, state: str = "running") -> ProcessState:
        return ProcessState(name=name, label="Whisper proxy", description="d", enabled=True,
                            state=state, pid=4242, started_at=0.0, uptime_sec=61.0,
                            port=9000, port_ok=True,
                            log_file=str(self.paths.log_dir / "proxy-2026-08-18.log"))

    def status_all(self):
        return [self._state("proxy"), self._state("counter", "disabled")]

    def status(self, name):
        return self._state(name)

    def start(self, name):
        self.calls.append(f"start:{name}")
        if self.raise_on_start:
            raise self.raise_on_start
        return self._state(name)

    def stop(self, name):
        self.calls.append(f"stop:{name}")
        return self._state(name, "stopped")

    def restart(self, name):
        self.calls.append(f"restart:{name}")
        return self._state(name)

    def log_path(self, name):
        return self.paths.log_dir / "proxy-2026-08-18.log"


@pytest.fixture
def client(tmp_path):
    credentials.save_password(tmp_path / "credentials.json", PASSWORD)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "proxy-2026-08-18.log").write_text("banner line\n", encoding="utf-8")
    fake = _FakeSupervisor(tmp_path / "logs")
    app = create_app(server_dir=_SERVER_DIR, config_path=tmp_path / "config.json",
                     credentials_path=tmp_path / "credentials.json", supervisor=fake)
    with TestClient(app) as test_client:
        test_client.headers[CSRF_HEADER] = test_client.post(
            "/api/login", json={"password": PASSWORD}).json()["csrf_token"]
        test_client.fake = fake
        yield test_client


def test_the_process_list_carries_what_a_card_needs(client):
    rows = client.get("/api/processes").json()["processes"]
    assert {row["name"] for row in rows} == {"proxy", "counter"}
    first = rows[0]
    assert first["uptime_sec"] == 61.0 and first["pid"] == 4242 and first["port_ok"] is True


def test_start_stop_and_restart_reach_the_supervisor(client):
    for action in ("start", "stop", "restart"):
        assert client.post(f"/api/processes/proxy/{action}").status_code == 200
    assert client.fake.calls == ["start:proxy", "stop:proxy", "restart:proxy"]


def test_an_unknown_process_is_a_404(client):
    assert client.post("/api/processes/nonesuch/start").status_code == 404


def test_a_refused_action_answers_409_with_the_reason(client):
    client.fake.raise_on_start = AlreadyRunning("Whisper proxy is already running")
    response = client.post("/api/processes/proxy/start")
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_a_failed_start_returns_the_childs_own_output(client):
    """"failed to start" is useless on a phone. The child's first lines are what is needed."""
    client.fake.raise_on_start = StartFailed("exited with code 1\nModuleNotFoundError: groq")
    response = client.post("/api/processes/proxy/start")
    assert response.status_code == 409
    assert "ModuleNotFoundError" in response.json()["detail"]


def test_the_log_route_returns_a_bounded_window_with_a_next_offset(client):
    window = client.get("/api/logs/proxy", params={"offset": 0}).json()
    assert window["text"] == "banner line\n"
    assert window["next_offset"] == len("banner line\n")


def test_the_health_route_reports_paths_and_the_proxy_being_down(client):
    body = client.get("/api/health").json()
    assert isinstance(body["paths"], list)
    assert body["proxy_error"] is None or "not answering" in body["proxy_error"]


def test_the_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
```

- [x] **Step 3: Run both to verify they fail**

Run: `py -m pytest server/tests/test_app_auth.py server/tests/test_app_routes.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.app'`.

- [x] **Step 4: Implement the app**

```python
# server/webapp/app.py
"""The control panel's HTTP surface.

create_app takes its paths explicitly so a test builds an app over tmp_path rather than over
the live config, the live credentials and the live logs.

Everything except /api/login, /api/session and the static files sits behind a session
dependency, and every mutating route additionally behind a CSRF dependency. A test enumerates
this app's own routes and asserts both -- that is what keeps it true as routes are added.
"""
from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from webapp import config_store, credentials, health as health_module, logs, registry
from webapp.auth import (
    COOKIE_NAME, CSRF_HEADER, LoginThrottle, SessionStore, TooManyAttempts,
)
from webapp.supervisor import ProcessState, Supervisor, SupervisorError

STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginRequest(BaseModel):
    password: str


class SessionInfo(BaseModel):
    authenticated: bool
    csrf_token: str | None = None
    password_set: bool


def _is_secure(request: Request) -> bool:
    """TLS is terminated outside this app, so the forwarded header is the real signal."""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
    return forwarded == "https" or request.url.scheme == "https"


def create_app(*, server_dir: Path, config_path: Path, credentials_path: Path,
               supervisor: Supervisor | None = None) -> FastAPI:
    app = FastAPI(title="SDR# control panel", docs_url=None, redoc_url=None,
                  openapi_url=None)

    sessions = SessionStore()
    throttle = LoginThrottle()

    def values() -> dict[str, str]:
        return config_store.load(config_path)

    paths = registry.resolve_paths(values(), server_dir)
    sup = supervisor or Supervisor(paths=paths, load_values=values)

    # -- guards ------------------------------------------------------------

    def require_session(request: Request):
        session = sessions.get(request.cookies.get(COOKIE_NAME))
        if session is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="not signed in")
        return session

    def require_csrf(request: Request, session=Depends(require_session)):
        sent = request.headers.get(CSRF_HEADER, "")
        if not sent or not secrets.compare_digest(sent, session.csrf):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN,
                                detail="missing or wrong CSRF token")
        return session

    def _spec_or_404(name: str):
        spec = registry.BY_NAME.get(name)
        if spec is None:
            raise HTTPException(status_code=404, detail=f"no managed process named {name!r}")
        return spec

    # -- open routes -------------------------------------------------------

    open_routes = APIRouter()

    @open_routes.get("/api/session", response_model=SessionInfo)
    def session_probe(request: Request) -> SessionInfo:
        session = sessions.get(request.cookies.get(COOKIE_NAME))
        return SessionInfo(authenticated=session is not None,
                           csrf_token=session.csrf if session else None,
                           password_set=credentials.has_password(credentials_path))

    @open_routes.post("/api/login")
    def login(body: LoginRequest, request: Request, response: Response) -> dict:
        client = request.client.host if request.client else "unknown"
        try:
            throttle.check(client)
        except TooManyAttempts as exc:
            raise HTTPException(status_code=429, detail=str(exc)) from None

        stored = credentials.load_hash(credentials_path)
        if not credentials.verify_password(stored, body.password):
            throttle.record_failure(client)
            # One message for a wrong password, a missing file and a damaged hash alike:
            # a login page must not tell an attacker which of those it is.
            raise HTTPException(status_code=401, detail="wrong password")

        throttle.record_success(client)
        session = sessions.create()
        response.set_cookie(COOKIE_NAME, session.token, httponly=True, samesite="strict",
                            secure=_is_secure(request), path="/")
        return {"csrf_token": session.csrf}

    # -- protected routes --------------------------------------------------

    guarded = APIRouter(dependencies=[Depends(require_session)])
    mutating = APIRouter(dependencies=[Depends(require_csrf)])

    @mutating.post("/api/logout")
    def logout(request: Request, response: Response) -> dict:
        token = request.cookies.get(COOKIE_NAME)
        if token:
            sessions.destroy(token)
        response.delete_cookie(COOKIE_NAME, path="/")
        return {"ok": True}

    @guarded.get("/api/processes")
    def list_processes() -> dict:
        return {"processes": [state.model_dump() for state in sup.status_all()]}

    def _act(name: str, action: str) -> ProcessState:
        _spec_or_404(name)
        try:
            return getattr(sup, action)(name)
        except SupervisorError as exc:
            # 409, not 500: refusing to start something already running, or refusing to kill
            # a stranger's process, is a state conflict and the message is the whole point.
            raise HTTPException(status_code=409, detail=str(exc)) from None

    # Three routes rather than one /{action}: the action is part of the API surface, not a
    # parameter, and the test that enumerates this app's mutating routes should see all three.
    @mutating.post("/api/processes/{name}/start", response_model=ProcessState)
    def start_process(name: str) -> ProcessState:
        return _act(name, "start")

    @mutating.post("/api/processes/{name}/stop", response_model=ProcessState)
    def stop_process(name: str) -> ProcessState:
        return _act(name, "stop")

    @mutating.post("/api/processes/{name}/restart", response_model=ProcessState)
    def restart_process(name: str) -> ProcessState:
        return _act(name, "restart")

    @guarded.get("/api/logs/{name}", response_model=logs.TailWindow)
    def read_log(name: str, offset: int | None = None,
                 limit: int = logs.DEFAULT_LIMIT) -> logs.TailWindow:
        _spec_or_404(name)
        path = sup.log_path(name)
        if path is None:
            return logs.TailWindow(path="", offset=0, next_offset=0, size=0,
                                   text="(this process has not written a log yet)")
        return logs.read_tail(path, offset=offset, limit=limit)

    @guarded.get("/api/health", response_model=health_module.Health)
    def read_health() -> health_module.Health:
        return health_module.health(values(), sup.paths)

    app.include_router(open_routes)
    app.include_router(guarded)
    app.include_router(mutating)

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
```

- [x] **Step 5: Implement the entry point**

```python
# server/webapp/startup.py
"""Reading the config, refusing an unsafe bind, and handing the app to uvicorn."""
from __future__ import annotations

import sys
from pathlib import Path

import uvicorn

from webapp import config_store, credentials
from webapp.app import create_app
from webapp.auth import UnsafeBind, check_bind_allowed

SERVER_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SERVER_DIR / "config.json"
CREDENTIALS_PATH = SERVER_DIR / "credentials.json"


def build(server_dir: Path = SERVER_DIR, config_path: Path = CONFIG_PATH,
          credentials_path: Path = CREDENTIALS_PATH) -> tuple:
    """(app, host, port), after the bind guard has had its say."""
    values = config_store.load(config_path)
    host = (values.get("WEBAPP_BIND_HOST") or "127.0.0.1").strip()
    port = int((values.get("WEBAPP_PORT") or "8787").strip())
    check_bind_allowed(host, credentials.has_password(credentials_path))
    return create_app(server_dir=server_dir, config_path=config_path,
                      credentials_path=credentials_path), host, port


def main() -> int:
    try:
        app, host, port = build()
    except UnsafeBind as exc:
        print(f"[control panel] {exc}", file=sys.stderr)
        return 2
    print(f"[control panel] http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
```

```python
# server/webapp/__main__.py
"""py -m webapp"""
import sys

from webapp.startup import main

if __name__ == "__main__":
    sys.exit(main())
```

- [x] **Step 6: Run the tests**

Run: `py -m pytest server/tests/test_app_auth.py server/tests/test_app_routes.py -v`
Expected: PASS. `test_the_index_page_is_served` fails until Task 12 creates `static/index.html` — create the directory with a one-line placeholder `index.html` now if it blocks, and let Task 12 replace it.

- [x] **Step 7: Run the full suite**

Run: `py -m pytest server/tests`
Expected: all pass.

- [x] **Step 8: Commit**

```bash
git add server/webapp/app.py server/webapp/startup.py server/webapp/__main__.py server/tests/test_app_auth.py server/tests/test_app_routes.py
git commit -m "Serve the panel: sessions on every route, CSRF on every mutation, a guarded bind"
```

---

### Task 12: The Dashboard and the Logs tab

**Files:**
- Create: `server/webapp/static/index.html`
- Create: `server/webapp/static/app.css`
- Create: `server/webapp/static/app.js`

**Interfaces:**
- Consumes: the routes from Task 11 only. No other origin, no CDN, no build step.
- Produces: nothing other code imports.

**REQUIRED SUB-SKILL: invoke the `frontend-design` skill before writing any markup.** The spec defers the visual design to build time deliberately, and this is that moment.

What the screen must do, which is not negotiable by the design:

1. **Login.** Shown when `GET /api/session` says `authenticated: false`. When it also says `password_set: false`, show the console command instead of a password box — there is no bootstrap route on purpose.
2. **Two tabs**, Dashboard and Logs. (Conversations, Vessels and Settings arrive in Phase 3; do not stub them.)
3. **A card per process**: label, state, uptime, pid, port and whether that port is actually held, plus Start / Stop / Restart and the last ~50 log lines. A `disabled` process shows as disabled with its buttons inert — not as failed.
4. **A health strip** across the top: STT backend, AIS cache size, time since the last AISHub poll, conversations stored, each configured path with a tick or a cross, and — given the most prominent position — **time since the last transmission arrived**. Age is computed against the proxy's `now`, never the browser's clock. "Never" and "4 minutes ago" must not look alike.
5. **When `/api/health` returns `proxy_error`**, say so in that strip. Never render zeros as if they were measurements.
6. **Logs tab**: pick a process, follow the tail, filter by substring. Poll from `next_offset`, append rather than replace, stick to the bottom only when the operator is already at the bottom, and show `restarted: true` as a visible "log rotated" divider.
7. **Every mutating fetch** sends the `X-CSRF-Token` header from the login response, and a 401 anywhere drops back to the login view. **Every URL is relative** — no `127.0.0.1`, no `localhost`, no port number anywhere in the JS, because the browser is on a different machine and will reach this app by LAN address or Tailscale name.
8. **Phone-sized screens work.** "Is it still running?" gets asked from a phone. Cards stack, the health strip wraps, buttons stay thumb-sized.
9. **Dark, monospace for logs, tabular numerals for the numbers.** A data-dense operator tool: legibility beats decoration.
10. **No secret is ever requested or rendered.** No route in Phase 2 returns one; keep it that way.

Polling: 3 s for processes and health, 2 s for the visible log tail; pause polling when `document.hidden`, so a tab left open overnight on a phone is not a load.

- [x] **Step 1: Invoke the frontend-design skill and settle the visual direction**

Run the skill, then write down in one paragraph at the top of `app.css` what the direction is, so a later change has something to be consistent with.

- [x] **Step 2: Write `index.html`**

One file, three regions: `#login`, `#dashboard`, `#logs`. No inline event handlers; `app.js` wires everything by id. Include `<meta name="viewport" content="width=device-width, initial-scale=1">` — without it point 8 is impossible.

- [x] **Step 3: Write `app.css`**

Hand-written, no framework, no web font from a CDN (the panel must work with no internet). Define the palette as CSS custom properties on `:root`.

- [x] **Step 4: Write `app.js`**

Vanilla, no modules to bundle. Structure it as: `api()` wrapper that attaches the CSRF header and routes 401s back to login; `renderHealth()`, `renderProcesses()`, `renderLog()`; one `setInterval` per concern, all suspended on `document.hidden`.

- [ ] **Step 5: Check it against a real browser** — done for checks 1, 2 and 5 plus live log
      tailing and filtering, against a throwaway instance on port 8799 with its own config and
      password in the scratchpad. Checks 3, 4 and 6 (start the counter from the panel, stop it,
      restart the app and see a running child re-found) were NOT done in a browser: starting a
      real child from a preview instance would have reached the live AIS station, and the same
      behaviour is covered by test_control_panel_end_to_end.py and test_supervisor.py. Worth
      repeating by hand once your own password is set.

```bash
cd server && py -m webapp
```

Open `http://127.0.0.1:8787`. Verify by hand, and record the result of each in the commit message:
1. Wrong password is refused; the sixth attempt says "too many".
2. The right password lands on the Dashboard.
3. Start the counter from the panel; its card goes green, a `counter-<date>.log` appears under `server/logs/`, and the Logs tab shows it growing.
4. Stop it; the card goes grey and the pid disappears.
5. Shrink the window to 390 px wide: the cards stack, nothing overflows horizontally.
6. Reload the page: still signed in. Restart `py -m webapp`: signed out, and the counter — if left running — is **still running** and is re-found.

- [x] **Step 6: Commit**

```bash
git add server/webapp/static
git commit -m "Add the Dashboard and Logs screens, dark and usable from a phone"
```

---

### Task 13: The phase's actual claim, tested end to end

**Files:**
- Create: `server/tests/test_control_panel_end_to_end.py`
- Modify: `docs/superpowers/plans/2026-08-18-control-panel-phase2-supervisor.md` (tick the boxes)

**Interfaces:**
- Consumes: everything.
- Produces: nothing.

Every other test tests a part. This one signs in to the real app and starts the **real proxy** through it, because the claim of this phase is that an operator can run the system from a browser without a console window. It inherits the hazard rules from `test_settings_end_to_end.py`: a free port, a cache under `tmp_path`, and every network secret cleared.

- [x] **Step 1: Write the failing test**

```python
# server/tests/test_control_panel_end_to_end.py
"""The phase's claim: sign in, start the real proxy, watch it, stop it -- no console window.

Hazards this test must stay clear of, exactly as in test_settings_end_to_end.py:
  - A live proxy is serving real radio traffic on 9000. The child here always gets a free port.
  - The AIS cache on disk is real data. The child is pointed at one inside tmp_path.
  - The API keys are real money. Every secret that would enable an outbound call is cleared.
"""
import socket
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store, credentials  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.auth import CSRF_HEADER  # noqa: E402

PASSWORD = "a long enough password"
_NETWORK_SECRETS = ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                    "AISSTREAM_API_KEY", "AISSTREAM_API_KEY2", "AISHUB_USERNAME")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for(predicate, timeout=30.0, interval=0.25):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    return None


@pytest.fixture
def client(tmp_path):
    port = _free_port()
    config = tmp_path / "config.json"
    config_store.save(config, {
        "PROXY_PORT": str(port),
        "AIS_CACHE_FILE": str(tmp_path / "ais_cache.json"),
        "CONVERSATIONS_FILE": str(tmp_path / "conversations.json"),
        "VESSELS_LOG_FILE": str(tmp_path / "identified_vessels.html"),
        "LOG_DIR": str(tmp_path / "logs"),
        "AIS_SOURCE": "off",
        "COUNTER_ENABLED": "off",
        **{key: "" for key in _NETWORK_SECRETS},
    })
    credentials.save_password(tmp_path / "credentials.json", PASSWORD)
    app = create_app(server_dir=_SERVER_DIR, config_path=config,
                     credentials_path=tmp_path / "credentials.json")
    with TestClient(app) as test_client:
        test_client.headers[CSRF_HEADER] = test_client.post(
            "/api/login", json={"password": PASSWORD}).json()["csrf_token"]
        test_client.port = port
        yield test_client
        try:
            test_client.post("/api/processes/proxy/stop")
        except Exception:
            pass


def test_the_panel_starts_the_real_proxy_watches_it_and_stops_it(client):
    started = client.post("/api/processes/proxy/start")
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "running"

    # It is really listening, on the port the config named -- not merely alive.
    listening = _wait_for(lambda: client.get("/api/processes").json()["processes"][0]["port_ok"])
    assert listening is True

    # Its console output reached a file rather than dying with a window.
    banner = _wait_for(lambda: "Whisper proxy" in client.get(
        "/api/logs/proxy", params={"offset": 0}).json()["text"])
    assert banner, client.get("/api/logs/proxy", params={"offset": 0}).json()["text"]

    # And the proxy answers the health route about itself.
    reported = _wait_for(lambda: client.get("/api/health").json()["proxy"].get("stt_backend"))
    assert reported == "groq"

    stopped = client.post("/api/processes/proxy/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"
    assert _wait_for(lambda: client.get("/api/processes").json()["processes"][0]["pid"] is None)


def test_restarting_through_the_panel_replaces_the_process_holding_the_port(client):
    first = client.post("/api/processes/proxy/start").json()
    _wait_for(lambda: client.get("/api/processes").json()["processes"][0]["port_ok"])

    second = client.post("/api/processes/proxy/restart").json()
    assert second["pid"] != first["pid"]
    assert _wait_for(lambda: client.get("/api/processes").json()["processes"][0]["port_ok"])


def test_a_disabled_process_cannot_be_started_through_the_api(client):
    refused = client.post("/api/processes/counter/start")
    assert refused.status_code == 409
    assert "disabled" in refused.json()["detail"].lower()
```

- [x] **Step 2: Run it to verify it fails, then passes**

Run: `py -m pytest server/tests/test_control_panel_end_to_end.py -v`
Expected: it fails only for real reasons at this point (everything it needs exists). If the proxy exits immediately, read the `StartFailed` detail — it carries the child's own output, which is the whole reason it was built that way.

- [x] **Step 3: Run the full suite and count**

Run: `py -m pytest server/tests`
Expected: green, ~940+ tests. Record the real number.

- [x] **Step 4: Confirm nothing leaked**

```bash
git status --short
py -c "import json,pathlib; p=pathlib.Path('server/config.json'); print('tracked' if p.exists() else 'absent')"
git ls-files | grep -E "config\.json|credentials\.json|server/logs" || echo "no secret file is tracked"
```

Expected: `no secret file is tracked`.

- [x] **Step 5: Tick this plan's boxes and commit**

```bash
git add server/tests/test_control_panel_end_to_end.py docs/superpowers/plans/2026-08-18-control-panel-phase2-supervisor.md
git commit -m "Prove the panel runs the real proxy end to end, with no console window"
```

---

## Done when

- [x] `py -m pytest server/tests` is green, with the new tests included.
- [x] `py -m webapp` serves a panel that starts, stops and restarts the proxy and the counter, and survives its own restart without orphaning either.
- [x] Binding beyond loopback with no password refuses to start, with a message naming `set_password`.
- [x] Every mutating route rejects an unauthenticated request, proven by a test that enumerates the app's own routes.
- [x] No secret appears in any API response, log line or error message.
- [x] The dashboard shows time since the last transmission, and says so plainly when the proxy is not answering.
- [x] `config.json` and `credentials.json` are readable only by this account and are not tracked.

## Carried to Phase 3

- **The Settings screen and its API** — deferred here deliberately (see "Out of scope"). It is the natural companion to the data views, and the Paths group it edits is what makes the Phase 4 migration one screen.
- Conversations and Vessels views, per spec Section 5.

## Carried to Phase 4

- AIS-catcher as a managed process — one more `ProcessSpec`.
- Boot-time startup on the miniPC, and the documented move procedure.
- Whether the panel should run as a Windows service, and if so how a service account reaches the AIS station's TCP port.
