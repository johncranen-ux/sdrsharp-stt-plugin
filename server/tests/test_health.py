"""Does this machine have the paths the settings name, and is the proxy answering?

Both questions exist for the same reason: a host migration that half-worked must be visible
immediately, not at the next transmission.
"""
import datetime
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


def test_the_log_directory_is_reported_once_with_its_resolved_value(tmp_path):
    checks = health.path_checks(_values(LOG_DIR=""),
                                Paths(server_dir=_SERVER_DIR, log_dir=tmp_path))
    log_checks = [check for check in checks if check.key == "LOG_DIR"]
    assert len(log_checks) == 1
    assert log_checks[0].value == str(tmp_path)
    assert log_checks[0].resolves is True


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


def test_health_combines_both_answers(tmp_path):
    report = health.health(_values(), Paths(server_dir=_SERVER_DIR, log_dir=tmp_path),
                           fetch=lambda url, timeout: {"stt_backend": "groq"})
    assert report.proxy["stt_backend"] == "groq"
    assert report.proxy_error is None
    assert any(check.key == "LOG_DIR" for check in report.paths)


# -- the feeds panel ---------------------------------------------------------
#
# A process being up and its data still arriving are two different facts. The cards answer the
# first; these answer the second -- the one that stayed silently green for most of an aisstream
# outage that ran roughly eight days (2026-08-05 to the 08-13 AISHub cutover). The silence was
# diagnosed on 2026-08-07, not at the end of the outage, so "unnoticed" describes only the gap
# before that fix landed, not the outage's full length.

import json  # noqa: E402
import time  # noqa: E402


def _heartbeat(path, *, age_sec=0.0, connected=True, last_poll_ok=True, vessels=25):
    """One line of the counter's log, as it really writes it."""
    stamp = datetime.datetime.fromtimestamp(time.time() - age_sec, datetime.timezone.utc)
    path.write_text(json.dumps({
        "type": "heartbeat", "hour": "2026-08-19T09", "connected": connected,
        "vessels_this_hour": vessels, "messages_this_hour": 1416, "polls": 38,
        "poll_failures": 0, "last_poll_ok": last_poll_ok,
        "last_poll_at": stamp.isoformat(timespec="seconds"),
        "t": stamp.isoformat(timespec="seconds"),
    }) + "\n", encoding="utf-8")


def _station(tmp_path, **kwargs):
    _heartbeat(tmp_path / "ais-station-count.jsonl", **kwargs)
    return health.ais_station_feed(Paths(server_dir=_SERVER_DIR, log_dir=tmp_path))


def test_a_station_heard_from_seconds_ago_is_green(tmp_path):
    feed = _station(tmp_path, age_sec=42)
    assert feed.lamp == "green"
    assert 40 <= feed.since_sec <= 60
    assert "25" in feed.detail


def test_a_station_that_stopped_writing_heartbeats_is_red(tmp_path):
    """No heartbeat means THIS process was not running -- the distinction the log exists for."""
    feed = _station(tmp_path, age_sec=1200)
    assert feed.lamp == "red"


def test_a_counter_alive_but_with_nothing_pushing_to_it_is_amber_not_red(tmp_path):
    """The counter is fine; AIS-catcher is not connected. Red would send someone to restart
    the wrong machine."""
    feed = _station(tmp_path, connected=False)
    assert feed.lamp == "amber"
    assert "connect" in feed.note.lower()


def test_a_station_web_ui_that_did_not_answer_the_last_poll_is_amber(tmp_path):
    feed = _station(tmp_path, last_poll_ok=False)
    assert feed.lamp == "amber"


def test_no_heartbeat_log_at_all_leaves_the_lamp_unlit(tmp_path):
    """Unlit, not red: nothing has claimed to be running, so nothing has failed."""
    feed = health.ais_station_feed(Paths(server_dir=_SERVER_DIR, log_dir=tmp_path))
    assert feed.lamp == "unlit"
    assert feed.since_sec is None


def test_a_truncated_last_line_falls_back_to_the_last_whole_one(tmp_path):
    """The log is appended to by another process, so a read can land mid-line."""
    path = tmp_path / "ais-station-count.jsonl"
    _heartbeat(path, age_sec=30)
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "heartbeat", "connec')
    feed = health.ais_station_feed(Paths(server_dir=_SERVER_DIR, log_dir=tmp_path))
    assert feed.lamp == "green"


# -- AISHub ------------------------------------------------------------------

def _proxy(now=1000.0, **aishub):
    block = {"last_ok_at": None, "last_error_at": None, "last_error": None,
             "last_count": None, "consecutive_failures": 0, "poll_sec": 900}
    block.update(aishub)
    return {"now": now, "ais_source": "aishub", "aishub": block}


def test_a_feed_polling_normally_is_green():
    feed = health.aishub_feed(_proxy(last_ok_at=640.0, last_count=994), None)
    assert feed.lamp == "green"
    assert feed.since_sec == 360.0
    assert "994" in feed.detail


def test_a_failed_last_poll_is_red_and_says_why():
    feed = health.aishub_feed(
        _proxy(last_ok_at=100.0, last_error_at=990.0, consecutive_failures=3,
               last_error="server reported ERROR: rate limit"), None)
    assert feed.lamp == "red"
    # The age still counts from the last GOOD poll: how long the cache has been stale is the
    # thing that matters, not how long ago we last tried.
    assert feed.since_sec == 900.0
    assert "rate limit" in feed.note


def test_a_poll_that_is_simply_overdue_is_amber():
    """No failure recorded, but nothing has succeeded in over two intervals -- the poll thread
    is wedged rather than erroring, which is how aisstream failed."""
    feed = health.aishub_feed(_proxy(last_ok_at=1000.0 - 1900, last_count=994), None)
    assert feed.lamp == "amber"


def test_a_feed_that_has_not_polled_yet_is_unlit():
    feed = health.aishub_feed(_proxy(), None)
    assert feed.lamp == "unlit"
    assert feed.since_sec is None


def test_a_source_that_is_not_aishub_leaves_the_lamp_unlit():
    payload = _proxy()
    payload["ais_source"] = "aisstream"
    feed = health.aishub_feed(payload, None)
    assert feed.lamp == "unlit"
    assert "aisstream" in feed.note


def test_a_proxy_that_is_down_leaves_the_lamp_unlit_rather_than_red():
    """We do not know the feed failed. We know we cannot see it, which is a different claim."""
    feed = health.aishub_feed({}, "the proxy is not answering")
    assert feed.lamp == "unlit"


def test_both_feeds_reach_the_health_payload(tmp_path):
    _heartbeat(tmp_path / "ais-station-count.jsonl")
    result = health.health(_values(), Paths(server_dir=_SERVER_DIR, log_dir=tmp_path),
                           fetch=lambda url, timeout: _proxy(last_ok_at=990.0, last_count=994))
    assert [feed.key for feed in result.feeds] == ["ais-station", "aishub"]
    assert [feed.owner for feed in result.feeds] == ["counter", "proxy"]
