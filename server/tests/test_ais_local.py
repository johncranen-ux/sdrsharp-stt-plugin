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
