import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from stt_proxy import aishub  # noqa: E402


def _envelope(records, error=False):
    return json.dumps([
        {"ERROR": error, "USERNAME": "X", "FORMAT": "HUMAN", "RECORDS": len(records)},
        records,
    ]).encode("utf-8")


SHIP = {
    "MMSI": 244123456, "TIME": "2026-08-12 10:02:58 GMT",
    "LONGITUDE": 3.95477, "LATITUDE": 52.06695, "COG": 51.3, "SOG": 4.9,
    "HEADING": 103, "IMO": 9406714, "NAME": "ORASUND", "CALLSIGN": "PBZL",
    "TYPE": 70, "A": 100, "B": 20, "C": 8, "D": 7, "DRAUGHT": 7.4,
    "DEST": "NLRTM",
}


def test_parse_response_returns_the_ships():
    ships = aishub.parse_response(_envelope([SHIP]))
    assert len(ships) == 1
    assert ships[0]["NAME"] == "ORASUND"


def test_parse_response_treats_the_error_flag_as_no_observation():
    # The rate-limit response: HTTP 200, valid JSON, ERROR true, no ships. Read as an
    # empty box it would mark every cached vessel out of scope.
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(_envelope([], error=True))


def test_parse_response_rejects_a_missing_ships_array():
    payload = json.dumps([{"ERROR": False, "RECORDS": 0}]).encode("utf-8")
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(payload)


def test_parse_response_rejects_malformed_json():
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(b"<html>502 Bad Gateway</html>")


def test_parse_response_allows_a_genuinely_empty_box():
    # Distinct from ERROR: a real observation that found nothing.
    assert aishub.parse_response(_envelope([])) == []


def test_map_ship_maps_every_field_record_understands():
    fields = aishub.map_ship(SHIP)
    assert fields["mmsi"] == "244123456"
    assert fields["name"] == "ORASUND"
    assert fields["callsign"] == "PBZL"
    assert fields["imo"] == 9406714
    assert fields["type"] == 70
    assert fields["length"] == 120      # A + B
    assert fields["beam"] == 15         # C + D
    assert fields["draught"] == 7.4
    assert fields["destination"] == "NLRTM"
    assert fields["latitude"] == 52.06695
    assert fields["longitude"] == 3.95477
    assert fields["sog"] == 4.9
    assert fields["cog"] == 51.3
    assert fields["heading"] == 103


def test_map_ship_drops_a_record_with_no_mmsi():
    assert aishub.map_ship({"NAME": "NO IDENTITY"}) is None


def test_map_ship_survives_absent_optional_fields():
    fields = aishub.map_ship({"MMSI": 1, "NAME": "SPARSE"})
    assert fields["mmsi"] == "1"
    assert fields["length"] is None
    assert fields["draught"] is None


def test_map_ship_strips_ais_destination_padding():
    fields = aishub.map_ship({**SHIP, "DEST": "ROTTERDAM@@@@@@@"})
    assert fields["destination"] == "ROTTERDAM"


def test_parse_time_reads_the_gmt_stamp():
    # Absolute epoch, so this assertion is timezone-independent.
    assert aishub.parse_time("2026-08-12 10:02:58 GMT") == 1786528978.0


def test_parse_time_returns_none_on_junk():
    assert aishub.parse_time("not a time") is None
    assert aishub.parse_time("") is None
