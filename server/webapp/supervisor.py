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

# How long a child gets to fall over before a start is called successful. Long enough to catch
# an import error or a bad path, short enough that the operator is not left staring at a button.
_SETTLE_SEC = 0.6


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
                # Stale: the process it named is gone, or the pid was reused. Clearing it here
                # keeps "stopped" honest and stops the next start refusing as AlreadyRunning.
                self._pid_file(spec).unlink(missing_ok=True)
            return ProcessState(
                name=spec.name, label=spec.label, description=spec.description,
                enabled=enabled, state="stopped" if enabled else "disabled",
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
        return self.paths.log_dir / f"{spec.log_prefix}-{datetime.date.today().isoformat()}.log"

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
            # The child holds its own duplicate of this handle; keeping ours open would pin
            # the file for as long as the panel runs.
            handle.close()

        started_at = time.time()
        time.sleep(_SETTLE_SEC)
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
