"""Tests for ais_local.py -- AIS-catcher JSON into the shared recorder.

Field names below are from real AIS-catcher v0.66 `-o 5` output, captured 2026-08-09 by
feeding the standard AIVDM test sentences over UDP. They are not guessed.
"""

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import ais_local  # noqa: E402

POSITION = {"class": "AIS", "type": 1, "mmsi": 366053209, "status": 3, "speed": 0.0,
            "lon": -122.341614, "lat": 37.802120, "course": 219.3, "heading": 1,
            "channel": "B"}

STATIC = {"class": "AIS", "type": 5, "mmsi": 369190000, "imo": 6710932,
          "callsign": "WDA9674", "shipname": "MT.MITCHELL", "shiptype": 99,
          "to_bow": 90, "to_stern": 90, "to_port": 10, "to_starboard": 10,
          "draught": 6.0, "destination": "SEATTLE", "channel": "A"}


def test_a_position_report_maps_to_recorder_fields():
    f = ais_local.parse_message(POSITION)
    assert f["mmsi"] == "366053209"
    assert f["latitude"] == pytest.approx(37.80212)
    assert f["longitude"] == pytest.approx(-122.341614)
    assert f["sog"] == 0.0 and f["cog"] == pytest.approx(219.3) and f["heading"] == 1
    assert "name" not in f


def test_a_static_report_maps_name_callsign_and_dimensions():
    f = ais_local.parse_message(STATIC)
    assert f["name"] == "MT.MITCHELL"
    assert f["callsign"] == "WDA9674"
    assert f["imo"] == 6710932
    assert f["type"] == 99
    assert f["length"] == 180      # to_bow + to_stern
    assert f["beam"] == 20         # to_port + to_starboard
    assert f["draught"] == 6.0
    assert f["destination"] == "SEATTLE"


def test_the_mmsi_is_a_string_because_the_cache_stores_strings():
    """AIS-catcher emits mmsi as an integer; every cache lookup compares strings."""
    assert ais_local.parse_message(POSITION)["mmsi"] == "366053209"


def test_a_message_flagged_with_an_error_is_rejected():
    """AIS-catcher still decodes a sentence whose checksum failed, and flags it with an
    `error` field -- observed 2026-08-09, where a corrupted checksum produced a full and
    entirely plausible decode. A wrong vessel name from a corrupt payload is the failure
    that costs most here, so suspect messages are dropped rather than trusted."""
    assert ais_local.parse_message({**STATIC, "error": 2}) is None


def test_a_message_with_error_zero_is_accepted():
    """Guard checks truthiness, not presence: `error: 0` means no error and must be
    accepted. Observed v0.66 output had no `error` key for clean messages, but if
    AIS-catcher ever uses 0 to mean "no error", this prevents silent feed loss."""
    f = ais_local.parse_message({**POSITION, "error": 0})
    assert f is not None and f["mmsi"] == "366053209"


def test_a_base_station_report_is_ignored():
    """Type 4 is a shore station, not a vessel. It carries an MMSI and would otherwise
    create a cache entry that no transmission can ever refer to."""
    assert ais_local.parse_message({"class": "AIS", "type": 4, "mmsi": 2442006}) is None


def test_an_aid_to_navigation_is_ignored():
    """Type 21 is a buoy, and it carries a `name` -- so without this it would enter the
    name-keyed cache and become a candidate for vessel name matching."""
    assert ais_local.parse_message(
        {"class": "AIS", "type": 21, "mmsi": 992441000, "name": "MAAS CENTER"}) is None


def test_a_message_with_no_mmsi_is_ignored():
    assert ais_local.parse_message({"class": "AIS", "type": 1}) is None


def test_an_empty_shipname_is_not_recorded_as_a_name():
    """AIS pads unset strings; an empty name must not create a vessel called ''."""
    f = ais_local.parse_message({**STATIC, "shipname": "   "})
    assert "name" not in f


def test_a_class_b_position_is_accepted():
    """Type 18 is Class B -- smaller vessels, common in the approach."""
    f = ais_local.parse_message({"class": "AIS", "type": 18, "mmsi": 244010000,
                                 "lat": 52.0, "lon": 3.9, "speed": 4.2, "course": 90.0})
    assert f["mmsi"] == "244010000" and f["latitude"] == 52.0


import json
import socket

from stt_proxy import ais  # noqa: E402


@pytest.fixture
def local_state(monkeypatch):
    vessels = {}
    monkeypatch.setattr(ais, "_vessel_cache", vessels)
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "AIS_LOCAL_MAX_KM", 0.0)   # filter off for transport tests
    monkeypatch.setattr(ais_local, "_stats",
                        {"messages": 0, "last_message_at": None,
                         "rejected": 0, "errors": 0})
    return vessels


def test_a_datagram_reaches_the_cache(local_state):
    ais_local.handle_datagram(json.dumps(STATIC).encode())
    assert "MT.MITCHELL" in local_state


def test_malformed_json_is_counted_and_survived(local_state):
    """A garbled datagram must never kill the listener thread."""
    assert ais_local.handle_datagram(b"{not json") is False
    assert ais_local.stats()["errors"] == 1
    assert local_state == {}


def test_an_ignored_message_type_is_counted_as_rejected(local_state):
    assert ais_local.handle_datagram(
        json.dumps({"class": "AIS", "type": 4, "mmsi": 2442006}).encode()) is False
    assert ais_local.stats()["rejected"] == 1


def test_stats_track_messages_and_the_last_message_time(local_state):
    assert ais_local.stats()["last_message_at"] is None
    ais_local.handle_datagram(json.dumps(POSITION).encode())
    assert ais_local.stats()["messages"] == 1
    assert ais_local.stats()["last_message_at"] is not None


def test_binding_a_port_someone_else_owns_fails_loudly():
    """SO_REUSEADDR is deliberately NOT set. ThreadingHTTPServer sets it, and a second
    proxy once bound alongside the first, silently took the port, and left the original
    running as a zombie -- so "restart it" quietly did nothing. A listener that quietly
    binds a port someone else owns is that bug in a new place."""
    first = ais_local.bind(0)
    port = first.getsockname()[1]
    try:
        with pytest.raises(OSError):
            ais_local.bind(port)
    finally:
        first.close()


def test_a_bound_socket_receives_over_loopback(local_state):
    sock = ais_local.bind(0)
    try:
        port = sock.getsockname()[1]
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.sendto(json.dumps(STATIC).encode(), ("127.0.0.1", port))
        sender.close()
        sock.settimeout(2.0)
        raw, _ = sock.recvfrom(65535)
        assert ais_local.handle_datagram(raw) is True
        assert "MT.MITCHELL" in local_state
    finally:
        sock.close()
