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
        "CONVERSATIONS_DB": str(tmp_path / "conversations.db"),
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


def _proxy_row(client):
    rows = client.get("/api/processes").json()["processes"]
    return next(row for row in rows if row["name"] == "proxy")


def test_the_panel_starts_the_real_proxy_watches_it_and_stops_it(client):
    started = client.post("/api/processes/proxy/start")
    assert started.status_code == 200, started.text
    assert started.json()["state"] == "running"

    # It is really listening, on the port the config named -- not merely alive.
    assert _wait_for(lambda: _proxy_row(client)["port_ok"]) is True

    # Its console output reached a file rather than dying with a window.
    banner = _wait_for(lambda: "Whisper proxy" in client.get(
        "/api/logs/proxy", params={"offset": 0}).json()["text"])
    assert banner, client.get("/api/logs/proxy", params={"offset": 0}).json()["text"]

    # And the proxy answers the health route about itself.
    assert _wait_for(
        lambda: client.get("/api/health").json()["proxy"].get("stt_backend")) == "groq"

    stopped = client.post("/api/processes/proxy/stop")
    assert stopped.status_code == 200
    assert stopped.json()["state"] == "stopped"
    assert _wait_for(lambda: _proxy_row(client)["pid"] is None)


def test_restarting_through_the_panel_replaces_the_process_holding_the_port(client):
    first = client.post("/api/processes/proxy/start").json()
    assert _wait_for(lambda: _proxy_row(client)["port_ok"])

    second = client.post("/api/processes/proxy/restart").json()
    assert second["pid"] != first["pid"]
    assert _wait_for(lambda: _proxy_row(client)["port_ok"])


def test_a_disabled_process_cannot_be_started_through_the_api(client):
    refused = client.post("/api/processes/counter/start")
    assert refused.status_code == 409
    assert "disabled" in refused.json()["detail"].lower()
