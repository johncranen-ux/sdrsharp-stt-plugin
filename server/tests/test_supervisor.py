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
from webapp import supervisor as supervisor_module  # noqa: E402
from webapp.ports import pid_listening_on  # noqa: E402
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
            [sys.executable, str(_FAKE)] + ([values["PROXY_PORT"]] if port_key else [])),
        description="a stand-in",
    )


def _supervisor(tmp_path, monkeypatch, *, port_key=None, **overrides) -> Supervisor:
    """A supervisor over one fake process, writing everything under tmp_path."""
    values = config_store.load(tmp_path / "absent.json")
    values.update(overrides)
    spec = _spec(port_key=port_key)
    monkeypatch.setattr(supervisor_module.registry, "BY_NAME", {"fake": spec}, raising=False)
    monkeypatch.setattr(supervisor_module.registry, "PROCESSES", (spec,), raising=False)
    return Supervisor(paths=Paths(server_dir=_SERVER_DIR, log_dir=tmp_path / "logs"),
                      load_values=lambda: dict(values))


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, monkeypatch)
    yield sup
    for state in sup.status_all():
        if state.state == "running":
            sup.stop(state.name)


def _wait_for(predicate, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    return None


def test_a_stopped_process_reports_stopped(supervisor):
    state = supervisor.status("fake")
    assert state.state == "stopped"
    assert state.pid is None


def test_starting_writes_a_pid_file_and_a_log_the_child_actually_wrote_to(supervisor):
    state = supervisor.start("fake")
    assert state.state == "running"
    assert state.pid

    log = Path(state.log_file)
    assert _wait_for(lambda: "fake child started" in log.read_text(encoding="utf-8")), \
        log.read_text(encoding="utf-8")


def test_a_second_start_is_refused_rather_than_starting_a_twin(supervisor):
    supervisor.start("fake")
    with pytest.raises(AlreadyRunning):
        supervisor.start("fake")


def test_a_fresh_supervisor_reattaches_to_the_running_child(supervisor):
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
    port = _free_port()
    sup = _supervisor(tmp_path, monkeypatch, port_key="PROXY_PORT", PROXY_PORT=str(port))
    try:
        first = sup.start("fake")
        assert _wait_for(lambda: pid_listening_on(port) == first.pid)

        second = sup.restart("fake")
        assert second.pid != first.pid
        assert _wait_for(lambda: pid_listening_on(port) == second.pid)
        assert sup.status("fake").port_ok is True
    finally:
        if sup.status("fake").state == "running":
            sup.stop("fake")


def test_a_disabled_process_is_neither_startable_nor_shown_as_failed(tmp_path, monkeypatch):
    sup = _supervisor(tmp_path, monkeypatch, COUNTER_ENABLED="off")
    assert sup.status("fake").state == "disabled"
    with pytest.raises(Disabled):
        sup.start("fake")
