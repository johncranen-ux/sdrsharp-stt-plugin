"""The test that keeps Section 3 true as routes are added: every mutating route, enumerated
from the app itself, must reject a request that carries no session."""
import socket
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store, credentials  # noqa: E402
from webapp.app import create_app  # noqa: E402
from webapp.auth import COOKIE_NAME, CSRF_HEADER  # noqa: E402

PASSWORD = "a long enough password"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def client(tmp_path):
    """An app that cannot reach anything real.

    The config is WRITTEN, not merely named. Naming a file that does not exist leaves every
    setting at its default -- LOG_DIR the real server/logs and PROXY_PORT 9000 -- which gave
    this fixture a supervisor over the live pid files. The CSRF test below posts a real stop
    with a real token, and on 2026-08-18 that killed the running proxy.
    """
    credentials.save_password(tmp_path / "credentials.json", PASSWORD)
    config_store.save(tmp_path / "config.json", {
        "LOG_DIR": str(tmp_path / "logs"),
        "PROXY_PORT": str(_free_port()),
        "CONVERSATIONS_DB": str(tmp_path / "conversations.db"),
    })
    app = create_app(server_dir=_SERVER_DIR,
                     config_path=tmp_path / "config.json",
                     credentials_path=tmp_path / "credentials.json")
    with TestClient(app) as test_client:
        yield test_client


def _all_routes(app):
    """(path, methods) for every route, including ones nested inside included routers.

    FastAPI 0.141 does not flatten include_router into app.routes any more -- it inserts an
    _IncludedRouter holding the original router -- so a non-recursive walk finds only the two
    routes declared on the app itself and would pass while checking nothing.
    """
    found = []
    stack = list(app.routes)
    while stack:
        route = stack.pop()
        nested = getattr(getattr(route, "original_router", None), "routes", None)
        if nested:
            stack.extend(nested)
            continue
        methods = getattr(route, "methods", set()) or set()
        found.append((getattr(route, "path", ""), set(methods) - {"HEAD", "OPTIONS"}))
    return found


def _login(client) -> str:
    response = client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["csrf_token"]


def test_every_mutating_route_rejects_a_request_with_no_session(client):
    """Enumerated from app.routes rather than listed by hand, so a route added later without
    a guard fails here instead of shipping open."""
    checked = 0
    for path, methods in _all_routes(client.app):
        mutating = methods & {"POST", "PUT", "PATCH", "DELETE"}
        if not mutating or path == "/api/login":
            continue
        url = path.replace("{name}", "proxy")
        for method in mutating:
            response = client.request(method, url)
            assert response.status_code in (401, 403), \
                f"{method} {url} answered {response.status_code}"
            checked += 1
    assert checked >= 4, "the enumeration found too few mutating routes -- it has stopped working"


def test_every_reading_route_rejects_a_request_with_no_session(client):
    for url in ("/api/processes", "/api/health", "/api/logs/proxy"):
        assert client.get(url).status_code == 401, url


def test_the_session_probe_and_login_are_reachable_without_a_session(client):
    probe = client.get("/api/session")
    assert probe.status_code == 200
    assert probe.json()["authenticated"] is False
    assert probe.json()["password_set"] is True


def test_a_correct_password_opens_a_session_and_a_wrong_one_does_not(client):
    assert client.post("/api/login",
                       json={"password": "wrong password entirely"}).status_code == 401
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
        assert client.post("/api/login",
                           json={"password": "wrong password entirely"}).status_code == 401
    assert client.post("/api/login",
                       json={"password": "wrong password entirely"}).status_code == 429
    # The correct password is refused too while the lockout stands -- otherwise the throttle
    # would only slow down an attacker who never guesses right.
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_with_no_password_configured_the_panel_says_so_and_refuses_every_login(tmp_path):
    config_store.save(tmp_path / "config.json", {"LOG_DIR": str(tmp_path / "logs")})
    app = create_app(server_dir=_SERVER_DIR, config_path=tmp_path / "config.json",
                     credentials_path=tmp_path / "credentials.json")
    with TestClient(app) as client:
        assert client.get("/api/session").json()["password_set"] is False
        assert client.post("/api/login", json={"password": "anything at all"}).status_code == 401


def test_no_response_ever_carries_a_secret(client, tmp_path):
    """The config holds six API keys. Nothing in phase 2 returns settings, and this is what
    keeps that true if a route is added that does."""
    from webapp import config_store
    config_store.save(tmp_path / "config.json", {"GROQ_API_KEY": "gsk_unmistakable_value"})
    _login(client)
    for url in ("/api/processes", "/api/health", "/api/logs/proxy", "/api/session",
               "/api/settings", "/api/conversations", "/api/conversations/some-id",
               "/api/vessels", "/api/vessels/123456789"):
        assert "gsk_unmistakable_value" not in client.get(url).text, url
