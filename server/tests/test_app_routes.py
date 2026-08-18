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
from webapp.registry import Paths  # noqa: E402
from webapp.supervisor import AlreadyRunning, ProcessState, StartFailed  # noqa: E402

PASSWORD = "a long enough password"


class _FakeSupervisor:
    def __init__(self, log_dir: Path):
        self.calls: list[str] = []
        self.paths = Paths(server_dir=_SERVER_DIR, log_dir=log_dir)
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
    (tmp_path / "logs" / "proxy-2026-08-18.log").write_bytes(b"banner line\n")
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
    assert client.get("/api/logs/nonesuch").status_code == 404


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
