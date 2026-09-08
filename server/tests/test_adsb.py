"""Tests for stt_proxy/adsb.py: the adsb.fi poll loop and live aircraft cache."""

import json
import sys
import threading
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import adsb  # noqa: E402


def test_parse_response_returns_aircraft_list():
    payload = json.dumps({"now": 123.0, "aircraft": [{"hex": "abc123"}]}).encode("utf-8")
    result = adsb.parse_response(payload)
    assert result == [{"hex": "abc123"}]


def test_parse_response_empty_aircraft_list_is_a_real_observation():
    """A genuinely empty sky is a valid answer, not a failure -- same reasoning as
    aishub.py's parse_response for a genuinely empty box."""
    payload = json.dumps({"now": 123.0, "aircraft": []}).encode("utf-8")
    assert adsb.parse_response(payload) == []


def test_parse_response_missing_aircraft_key_raises():
    payload = json.dumps({"now": 123.0}).encode("utf-8")
    with pytest.raises(adsb.AdsbError):
        adsb.parse_response(payload)


def test_parse_response_aircraft_not_a_list_raises():
    payload = json.dumps({"now": 123.0, "aircraft": "not a list"}).encode("utf-8")
    with pytest.raises(adsb.AdsbError):
        adsb.parse_response(payload)


def test_parse_response_malformed_json_raises():
    with pytest.raises(adsb.AdsbError):
        adsb.parse_response(b"not json at all")


def test_parse_response_non_dict_envelope_raises():
    payload = json.dumps([1, 2, 3]).encode("utf-8")
    with pytest.raises(adsb.AdsbError):
        adsb.parse_response(payload)


def test_map_aircraft_extracts_known_fields():
    ac = {
        "hex": "484443", "flight": "KLM281  ", "r": "PH-BHA", "t": "A332",
        "alt_baro": 8125, "gs": 245.3, "track": 91.2,
        "lat": 52.333237, "lon": 4.445724, "squawk": "1000",
    }
    result = adsb.map_aircraft(ac)
    assert result["hex"] == "484443"
    assert result["flight"] == "KLM281"          # trimmed
    assert result["r"] == "PH-BHA"
    assert result["t"] == "A332"
    assert result["alt_baro"] == 8125
    assert result["gs"] == 245.3
    assert result["track"] == 91.2
    assert result["lat"] == 52.333237
    assert result["lon"] == 4.445724
    assert result["squawk"] == "1000"


def test_map_aircraft_handles_ground_altitude():
    """adsb.fi reports alt_baro as the literal string "ground" for aircraft on the runway,
    not a number -- observed live on EZY85FV during the 2026-09-08 feasibility check."""
    ac = {"hex": "406012", "flight": "EZY85FV ", "alt_baro": "ground"}
    result = adsb.map_aircraft(ac)
    assert result["alt_baro"] == "ground"


def test_map_aircraft_returns_none_without_hex():
    """hex is the cache key -- an aircraft record without one cannot be cached, the same
    reasoning aishub.py's map_ship uses for a ship record without an MMSI."""
    assert adsb.map_aircraft({"flight": "KLM281"}) is None


def test_map_aircraft_defaults_missing_optional_fields_to_none():
    result = adsb.map_aircraft({"hex": "abc123"})
    assert result["flight"] == ""
    assert result["r"] is None
    assert result["t"] is None
    assert result["lat"] is None
    assert result["lon"] is None


def test_build_url_shape():
    url = adsb.build_url(52.15, 4.3, 40)
    assert url == "https://opendata.adsb.fi/api/v2/lat/52.15/lon/4.3/dist/40"


# ---------------------------------------------------------------------------
# ADSB_SOURCE (review finding 5): the on/off switch adsb.fi never had, mirroring AIS_SOURCE
# ---------------------------------------------------------------------------

def test_resolve_source_defaults_to_adsbfi(monkeypatch):
    monkeypatch.delenv("ADSB_SOURCE", raising=False)
    assert adsb._resolve_source() == "adsbfi"


def test_resolve_source_reads_off(monkeypatch):
    monkeypatch.setenv("ADSB_SOURCE", "off")
    assert adsb._resolve_source() == "off"


def test_resolve_source_is_case_and_whitespace_insensitive(monkeypatch):
    monkeypatch.setenv("ADSB_SOURCE", "  OFF  ")
    assert adsb._resolve_source() == "off"


# ---------------------------------------------------------------------------
# Env var parsing guards (review finding 6): a typo must fall back, not crash the import
# ---------------------------------------------------------------------------

def test_resolve_float_falls_back_to_default_on_malformed_value(monkeypatch):
    """whisper-proxy.py imports this module at load time -- a ValueError here would take
    down the entire proxy over a typo in a setting that only affects flight identification."""
    monkeypatch.setenv("ADSB_LAT", "not-a-number")
    assert adsb._resolve_float("ADSB_LAT", 52.15) == 52.15


def test_resolve_float_uses_the_env_var_when_it_parses(monkeypatch):
    monkeypatch.setenv("ADSB_LAT", "51.9")
    assert adsb._resolve_float("ADSB_LAT", 52.15) == 51.9


def test_resolve_poll_sec_falls_back_to_default_on_malformed_value(monkeypatch):
    monkeypatch.setenv("ADSB_POLL_SEC", "abc")
    assert adsb._resolve_poll_sec(15) == 15


def test_resolve_poll_sec_floors_a_too_low_value():
    """A malformed value isn't the only way to tight-loop the external API -- a valid but
    tiny value must be floored the same way aishub.py floors AISHUB_POLL_SEC."""
    assert adsb._resolve_poll_sec(1) == adsb.MIN_POLL_SEC


def test_resolve_poll_sec_keeps_a_reasonable_value(monkeypatch):
    monkeypatch.setenv("ADSB_POLL_SEC", "30")
    assert adsb._resolve_poll_sec(15) == 30


@pytest.fixture(autouse=True)
def _clear_cache():
    adsb._aircraft_cache.clear()
    adsb.reset_feed_state()
    yield
    adsb._aircraft_cache.clear()
    adsb.reset_feed_state()


def _fake_fetch(payload: bytes):
    return lambda url: payload


def test_poll_once_populates_the_cache():
    payload = json.dumps({"now": 1.0, "aircraft": [
        {"hex": "484443", "flight": "KLM281"},
        {"hex": "4caa5a", "flight": "RYR37DV"},
    ]}).encode("utf-8")

    count = adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(payload))

    assert count == 2
    cached = {ac["hex"]: ac["flight"] for ac in adsb.current_aircraft()}
    assert cached == {"484443": "KLM281", "4caa5a": "RYR37DV"}


def test_poll_once_replaces_rather_than_accumulates():
    """A full-replace snapshot, not an accumulating merge -- see the Global Constraints
    note in the plan for why aircraft don't get AIS-style staleness tracking."""
    first  = json.dumps({"now": 1.0, "aircraft": [{"hex": "111111", "flight": "AAA1"}]}).encode()
    second = json.dumps({"now": 2.0, "aircraft": [{"hex": "222222", "flight": "BBB2"}]}).encode()

    adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(first))
    adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(second))

    hexes = {ac["hex"] for ac in adsb.current_aircraft()}
    assert hexes == {"222222"}


def test_poll_once_skips_aircraft_without_hex_but_keeps_the_rest():
    payload = json.dumps({"now": 1.0, "aircraft": [
        {"flight": "NOHEX"}, {"hex": "abc123", "flight": "OK1"},
    ]}).encode("utf-8")

    count = adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(payload))

    assert count == 1
    assert [ac["hex"] for ac in adsb.current_aircraft()] == ["abc123"]


def test_poll_once_raises_and_leaves_cache_untouched_on_bad_response(monkeypatch):
    payload = json.dumps({"now": 1.0, "aircraft": [{"hex": "abc123", "flight": "OK1"}]}).encode()
    adsb.poll_once(52.15, 4.3, 40, fetch=_fake_fetch(payload))

    def _broken_fetch(url):
        return b"not json"

    with pytest.raises(adsb.AdsbError):
        adsb.poll_once(52.15, 4.3, 40, fetch=_broken_fetch)

    # Cache from the earlier good poll is untouched.
    assert [ac["hex"] for ac in adsb.current_aircraft()] == ["abc123"]


def test_poll_and_record_success_updates_feed_status():
    payload = json.dumps({"now": 1.0, "aircraft": [{"hex": "abc123", "flight": "OK1"}]}).encode()

    adsb.poll_and_record(52.15, 4.3, 40, fetch=_fake_fetch(payload))

    status = adsb.feed_status()
    assert status["last_ok_at"] is not None
    assert status["last_count"] == 1
    assert status["consecutive_failures"] == 0


def test_poll_and_record_never_raises_on_failure():
    def _broken_fetch(url):
        raise OSError("network is down")

    adsb.poll_and_record(52.15, 4.3, 40, fetch=_broken_fetch)  # must not raise

    status = adsb.feed_status()
    assert status["last_error"] is not None
    assert status["consecutive_failures"] == 1


def test_poll_and_record_never_raises_on_unexpected_exception():
    """poll_and_record is a daemon thread's entry point with nothing above it -- an escaped
    exception silently ends polling forever, the exact failure aishub.py's docstring
    describes for the eight-day aisstream outage. This must never happen here either."""
    def _exploding_fetch(url):
        raise ValueError("something else went wrong")

    adsb.poll_and_record(52.15, 4.3, 40, fetch=_exploding_fetch)  # must not raise

    assert adsb.feed_status()["consecutive_failures"] == 1


# ---------------------------------------------------------------------------
# _record_success console pacing (review finding 7): adsb.fi polls every ~15s, so printing
# on every success (as aishub.py does for its 900s polls) would flood the console.
# ---------------------------------------------------------------------------

def test_record_success_first_poll_prints():
    adsb._record_success(3)
    # _last_count is set regardless of whether a line was printed -- the state-tracking half
    # of "does it print" that this test suite can assert without capturing stdout.
    assert adsb.feed_status()["last_count"] == 3


def test_record_success_unchanged_count_does_not_print_again(capsys):
    adsb._record_success(3)
    capsys.readouterr()  # discard the first poll's line

    adsb._record_success(3)

    assert capsys.readouterr().out == ""


def test_record_success_changed_count_prints_again(capsys):
    adsb._record_success(3)
    capsys.readouterr()

    adsb._record_success(4)

    assert "4 aircraft" in capsys.readouterr().out


def test_record_success_after_a_failure_streak_prints_recovered(capsys):
    adsb._record_failure("network is down")
    adsb._record_failure("network is down")
    capsys.readouterr()

    adsb._record_success(3)

    assert "recovered after 2 failed poll(s)" in capsys.readouterr().out
