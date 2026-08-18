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
