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

_CONVERSATIONS = [
    {
        "start": "2026-08-19T10:15:00+00:00",
        "end": "2026-08-19T10:16:30+00:00",
        "channel": "16",
        "vessel": "PASHA BULKER",
        "mmsi": "244123456",
        "type": "Bulk Carrier",
        "destination": "ROTTERDAM",
        "confidence": 0.87,
        "turns": [
            {"time": "2026-08-19T10:15:05+00:00", "raw": "Pasha Approach",
             "text": "Pasha Bulker", "conv": None,
             "live_vessel": "PASHA BULKER", "live_mmsi": "244123456",
             # 3h15m before the call: confirmed under the 360-minute default, stale under a
             # tightened setting. That difference is what the threshold test below asserts.
             "live_seen": "2026-08-19 07:00:00"},
        ],
        "resolver_candidates": [],
    },
    {
        "start": "2026-08-19T09:00:00+00:00",
        "end": "2026-08-19T09:02:00+00:00",
        "channel": "12",
        "vessel": None,
        "mmsi": None,
        "type": None,
        "destination": None,
        "confidence": None,
        "turns": [],
        "resolver_candidates": [],
    },
]

_VESSELS = [
    {"mmsi": "244123456", "name": "PASHA BULKER", "callsign": "PH1234",
     "type": "Bulk Carrier", "destination": "ROTTERDAM", "draught": 12.3,
     "last_seen": "2026-08-19T10:00:00+00:00", "source": "aishub"},
    {"mmsi": "999888777", "name": "SEA STAR", "callsign": "PH5678", "type": "Tanker",
     "destination": "MAASVLAKTE", "draught": 8.1,
     "last_seen": "2026-08-19T08:00:00+00:00", "source": "local"},
]


def _fake_proxy_data(conversations, vessels):
    from webapp.proxy_data import ProxyData

    def fetch(url, timeout):
        return conversations if "conversations" in url else vessels
    return ProxyData(lambda: {"PROXY_PORT": "9000"}, fetch=fetch)


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


def _build_app(tmp_path):
    credentials.save_password(tmp_path / "credentials.json", PASSWORD)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "proxy-2026-08-18.log").write_bytes(b"banner line\n")
    fake = _FakeSupervisor(tmp_path / "logs")
    proxy_data = _fake_proxy_data(_CONVERSATIONS, _VESSELS)
    app = create_app(server_dir=_SERVER_DIR, config_path=tmp_path / "config.json",
                     credentials_path=tmp_path / "credentials.json", supervisor=fake,
                     proxy_data=proxy_data)
    return app, fake


@pytest.fixture
def client(tmp_path):
    app, fake = _build_app(tmp_path)
    with TestClient(app) as test_client:
        test_client.headers[CSRF_HEADER] = test_client.post(
            "/api/login", json={"password": PASSWORD}).json()["csrf_token"]
        test_client.fake = fake
        yield test_client


@pytest.fixture
def unauthenticated_client(tmp_path):
    """Same app, same fixed proxy data -- but never logs in, and carries no CSRF header.

    Every new data route needs a test proving an unauthenticated request is rejected; this is
    what makes that possible without duplicating app construction.
    """
    app, _ = _build_app(tmp_path)
    with TestClient(app) as test_client:
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


def test_the_health_route_carries_a_lamp_for_every_feed(client):
    """The panel draws the annunciator straight from this, so a feed dropping out of the
    payload would silently remove a lamp rather than show a failed one."""
    feeds = client.get("/api/health").json()["feeds"]
    assert [feed["key"] for feed in feeds] == ["ais-station", "aishub"]
    for feed in feeds:
        assert feed["lamp"] in {"green", "amber", "red", "unlit"}
        assert feed["owner"] in {"proxy", "counter"}


def test_the_index_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


def test_the_conversations_route_pages_and_reports_its_snapshot(client):
    body = client.get("/api/conversations", params={"limit": 1}).json()
    assert body["limit"] == 1
    assert "stale" in body["snapshot"] and "age_sec" in body["snapshot"]


def test_the_conversations_route_never_ships_transcripts_in_the_list(client):
    """The list is polled; 613 KB per poll over Tailscale is not acceptable."""
    for row in client.get("/api/conversations").json()["rows"]:
        assert "turns" not in row


def test_an_unknown_conversation_id_is_a_404_not_an_empty_object(client):
    assert client.get("/api/conversations/nope").status_code == 404


def test_a_conversation_id_with_colons_and_a_pipe_round_trips(client):
    """conversation_id is f"{start}|{channel}" -- it carries both the ':' of an ISO timestamp
    and the '|' separator. The route uses a :path converter; this proves a browser-side
    encodeURIComponent round-trips through it rather than 404ing on every detail page."""
    from urllib.parse import quote

    conv_id = "2026-08-19T10:15:00+00:00|16"
    response = client.get(f"/api/conversations/{quote(conv_id, safe='')}")
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == conv_id
    assert body["vessel"] == "PASHA BULKER"


def test_the_vessels_route_searches(client):
    body = client.get("/api/vessels", params={"text": "pasha"}).json()
    assert "rows" in body and "total" in body
    assert [row["mmsi"] for row in body["rows"]] == ["244123456"]


def test_an_unknown_mmsi_is_a_404(client):
    assert client.get("/api/vessels/999999999").status_code == 404


def test_a_known_vessel_carries_its_conversations(client):
    body = client.get("/api/vessels/244123456").json()
    assert body["mmsi"] == "244123456"
    assert [c["mmsi"] for c in body["conversations"]] == ["244123456"]
    assert body["conversations_snapshot"]["stale"] is False


def test_a_known_vessel_reports_when_its_conversations_could_not_be_fetched(tmp_path):
    """The vessel exists in the AIS cache, but the conversations fetch fails outright -- an
    empty list here must not read as "this vessel has no conversations"; the snapshot lets the
    UI tell the two apart."""
    from webapp.proxy_data import ProxyData

    def fetch(url, timeout):
        if "conversations" in url:
            raise ConnectionRefusedError("nobody home")
        return _VESSELS

    credentials.save_password(tmp_path / "credentials.json", PASSWORD)
    (tmp_path / "logs").mkdir()
    fake = _FakeSupervisor(tmp_path / "logs")
    proxy_data = ProxyData(lambda: {"PROXY_PORT": "9000"}, fetch=fetch)
    app = create_app(server_dir=_SERVER_DIR, config_path=tmp_path / "config.json",
                     credentials_path=tmp_path / "credentials.json", supervisor=fake,
                     proxy_data=proxy_data)
    with TestClient(app) as test_client:
        test_client.headers[CSRF_HEADER] = test_client.post(
            "/api/login", json={"password": PASSWORD}).json()["csrf_token"]
        body = test_client.get("/api/vessels/244123456").json()
    assert body["conversations"] == []
    assert body["conversations_snapshot"]["error"] is not None
    assert body["conversations_snapshot"]["has_data"] is False


@pytest.mark.parametrize("path", ["/api/conversations", "/api/conversations/x",
                                  "/api/vessels", "/api/vessels/1", "/api/settings"])
def test_the_data_routes_reject_an_unauthenticated_request(unauthenticated_client, path):
    assert unauthenticated_client.get(path).status_code == 401


def test_posting_settings_unauthenticated_is_rejected(unauthenticated_client):
    assert unauthenticated_client.post("/api/settings", json={}).status_code == 401


# -- captured audio ---------------------------------------------------------------
#
# The play button on a turn. Every one of these builds its app over a config whose CAPTURES_DIR
# is under tmp_path: the catalogue default is the operator's real 1.5 GB capture directory, and
# conftest refuses to read it.


def _captures(tmp_path, day="2026-08-19", index=0, stamp="2026-08-19T10:15:05"):
    root = tmp_path / "captures"
    (root / day).mkdir(parents=True)
    (root / day / f"{index:04d}_sent.wav").write_bytes(b"RIFF$\x00\x00\x00WAVEfmt ")
    (root / day / f"{index:04d}_raw.wav").write_bytes(b"RIFF-raw")
    (root / day / "index.jsonl").write_text(
        '{"index": %d, "timestamp": "%s"}\n' % (index, stamp), encoding="utf-8-sig")
    return root


def _client_with_captures(tmp_path, root):
    from webapp import config_store

    config_store.save(tmp_path / "config.json", {"CAPTURES_DIR": str(root)})
    app, _fake = _build_app(tmp_path)
    client = TestClient(app)
    client.headers[CSRF_HEADER] = client.post(
        "/api/login", json={"password": PASSWORD}).json()["csrf_token"]
    return client


def test_a_turn_carries_the_clip_captured_for_it(tmp_path):
    # The fixture conversation starts 2026-08-19 10:15:00 with a turn at 10:15:05.
    client = _client_with_captures(tmp_path, _captures(tmp_path))
    listed = client.get("/api/conversations").json()["rows"]
    detail = client.get(f"/api/conversations/{listed[0]['id']}").json()
    assert detail["turns"][0]["clip"] == "0000"
    assert detail["turns"][0]["clip_day"] == "2026-08-19"


def test_a_turn_with_no_capture_says_so_rather_than_offering_a_dead_button(tmp_path):
    client = _client_with_captures(tmp_path, _captures(tmp_path, stamp="2026-08-19T23:00:00"))
    listed = client.get("/api/conversations").json()["rows"]
    detail = client.get(f"/api/conversations/{listed[0]['id']}").json()
    assert detail["turns"][0]["clip"] is None


def test_the_clip_endpoint_returns_the_sent_audio(tmp_path):
    client = _client_with_captures(tmp_path, _captures(tmp_path))
    response = client.get("/api/clips/2026-08-19/0000")
    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    # _sent, not _raw: what the model was actually given.
    assert response.content == b"RIFF$\x00\x00\x00WAVEfmt "


def test_the_clip_endpoint_refuses_a_traversal(tmp_path):
    client = _client_with_captures(tmp_path, _captures(tmp_path))
    for day, clip in (("..", "0000"), ("2026-08-19", "../../../etc/passwd"),
                      ("2026-08-19", "0000_raw"), ("....", "0000")):
        assert client.get(f"/api/clips/{day}/{clip}").status_code in (404, 405), (day, clip)


def test_the_clip_endpoint_is_a_404_when_nothing_was_captured(tmp_path):
    client = _client_with_captures(tmp_path, _captures(tmp_path))
    assert client.get("/api/clips/2026-08-01/0000").status_code == 404


def test_no_captures_directory_configured_leaves_turns_unplayable(tmp_path):
    from webapp import config_store

    config_store.save(tmp_path / "config.json", {"CAPTURES_DIR": ""})
    app, _fake = _build_app(tmp_path)
    client = TestClient(app)
    client.headers[CSRF_HEADER] = client.post(
        "/api/login", json={"password": PASSWORD}).json()["csrf_token"]
    listed = client.get("/api/conversations").json()["rows"]
    detail = client.get(f"/api/conversations/{listed[0]['id']}").json()
    assert detail["turns"][0]["clip"] is None
    assert client.get("/api/clips/2026-08-19/0000").status_code == 404


def test_clip_audio_needs_a_session(unauthenticated_client):
    assert unauthenticated_client.get("/api/clips/2026-08-19/0000").status_code == 401


def _label_with_setting(tmp_path, minutes):
    from webapp import config_store

    config_store.save(tmp_path / "config.json", {"AIS_LIVE_MATCH_MAX_AGE_MIN": minutes})
    app, _fake = _build_app(tmp_path)
    client = TestClient(app)
    client.headers[CSRF_HEADER] = client.post(
        "/api/login", json={"password": PASSWORD}).json()["csrf_token"]
    listed = client.get("/api/conversations").json()["rows"]
    detail = client.get(f"/api/conversations/{listed[0]['id']}").json()
    return detail["turns"][0]["live_match"]


def test_the_route_honours_the_operators_freshness_setting(tmp_path):
    """The screen's "confirmed" must follow AIS_LIVE_MATCH_MAX_AGE_MIN, the same setting the
    resolver uses to refuse a stale live match. Hard-coding the default agreed with the resolver
    only until someone changed it -- and then the screen would keep calling a match confirmed
    that the resolver had already thrown out."""
    # The fixture turn's ship was last seen 3h15m before the call.
    assert _label_with_setting(tmp_path, "360") == "ais-confirmed"


def test_tightening_the_setting_tightens_what_the_screen_claims(tmp_path):
    assert _label_with_setting(tmp_path, "120") == "ais-stale"
