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


def test_parse_response_rejects_a_non_dict_envelope():
    # body[0] not a dict used to fall back to envelope = {}, which skips the ERROR check
    # entirely and returns body[1] when it happens to be a list -- feeding ghost ships
    # straight into the cache.
    payload = json.dumps([[1, 2, 3], [{"MMSI": 1, "NAME": "GHOST"}]]).encode("utf-8")
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(payload)


def test_build_url_carries_the_box_and_asks_for_json():
    url = aishub.build_url("USER", (51.0, 53.2, 2.0, 6.0))
    assert url.startswith(aishub.API_URL + "?")
    assert "username=USER" in url
    assert "output=json" in url
    assert "format=1" in url
    assert "latmin=51.0" in url and "latmax=53.2" in url
    assert "lonmin=2.0" in url and "lonmax=6.0" in url


def test_poll_once_records_every_named_vessel(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    payload = _envelope([SHIP, {**SHIP, "MMSI": 999, "NAME": "SECOND"}])
    count = aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: payload)

    assert count == 2
    assert ais._mmsi_index["244123456"]["name"] == "ORASUND"
    assert ais._mmsi_index["999"]["name"] == "SECOND"


def test_poll_once_uses_the_report_time_as_last_seen(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    import datetime as _dt
    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))

    # SHIP's TIME is 2026-08-12 10:02:58 GMT. Stored as local wall-clock, like every other
    # last_seen in this codebase, so compute rather than hardcode.
    expected = _dt.datetime.fromtimestamp(1786528978.0).strftime("%Y-%m-%d %H:%M:%S")
    assert ais._vessel_cache["ORASUND"]["last_seen"] == expected


def test_poll_once_publishes_the_in_scope_set(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    payload = _envelope([SHIP, {**SHIP, "MMSI": 999, "NAME": "SECOND"}])
    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: payload)

    assert ais.get_in_scope() == {"244123456", "999"}


def test_a_failed_poll_leaves_the_cache_and_the_scope_alone(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))
    before_cache = dict(ais._vessel_cache)
    before_scope = set(ais.get_in_scope())

    with pytest.raises(aishub.AisHubError):
        aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0),
                         fetch=lambda url: _envelope([], error=True))

    assert ais._vessel_cache == before_cache
    assert ais.get_in_scope() == before_scope


def test_the_poll_interval_cannot_be_configured_below_the_rate_limit(monkeypatch):
    monkeypatch.setenv("AISHUB_POLL_SEC", "5")
    assert aishub._resolve_poll_sec() == aishub.MIN_INTERVAL_SEC


def test_the_poll_interval_honours_a_legal_setting(monkeypatch):
    monkeypatch.setenv("AISHUB_POLL_SEC", "900")
    assert aishub._resolve_poll_sec() == 900
