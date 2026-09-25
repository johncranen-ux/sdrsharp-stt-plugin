"""Tests for the panel's reader of the proxy's ADS-B snapshot log (the "New flight..." list)."""
import datetime
import json

from webapp import air_aircraft

# 2026-09-25 09:56:02 local, whatever the machine's zone: the file name follows the local date.
AT = datetime.datetime(2026, 9, 25, 9, 56, 2).astimezone()
DAY = AT.date().isoformat()

KLM67F = {"hex": "4864eb", "flight": "KLM67F", "r": "PH-NXN", "t": "E295", "alt_baro": 10150,
          "lat": 52.287689, "lon": 4.490738, "nav_altitude_mcp": None, "nav_heading": None,
          "track": 328.86}
EZY67KZ = {"hex": "4007f1", "flight": "EZY67KZ", "r": "G-EZOA", "t": "A320", "alt_baro": 5850,
           "lat": 52.55, "lon": 4.3, "nav_altitude_mcp": 6000, "nav_heading": 180.0,
           "track": 180.0}


def _write(log_dir, day, *snapshots, name="adsb-{day}.jsonl"):
    path = log_dir / name.format(day=day)
    with path.open("a", encoding="utf-8") as handle:
        for when, aircraft in snapshots:
            handle.write(json.dumps({"t": when.isoformat(), "aircraft": aircraft}) + "\n")
    return path


def _at(seconds):
    return AT + datetime.timedelta(seconds=seconds)


def test_the_nearest_snapshot_wins(tmp_path):
    _write(tmp_path, DAY, (_at(-20), [EZY67KZ]), (_at(4), [KLM67F]), (_at(19), []))
    snap = air_aircraft.nearest_snapshot(tmp_path, AT.timestamp())
    assert [a["flight"] for a in snap["aircraft"]] == ["KLM67F"]
    assert snap["t"] == _at(4).isoformat()


def test_nothing_within_thirty_seconds_is_no_snapshot(tmp_path):
    _write(tmp_path, DAY, (_at(-45), [KLM67F]), (_at(31), [KLM67F]))
    assert air_aircraft.nearest_snapshot(tmp_path, AT.timestamp()) is None


def test_a_missing_day_file_is_no_snapshot(tmp_path):
    assert air_aircraft.nearest_snapshot(tmp_path, AT.timestamp()) is None


def test_the_09_10_side_car_name_is_read_too(tmp_path):
    _write(tmp_path, DAY, (_at(2), [KLM67F]), name="adsb-snapshots-{day}.jsonl")
    assert air_aircraft.nearest_snapshot(tmp_path, AT.timestamp()) is not None


def test_a_snapshot_just_past_midnight_is_found_from_the_day_before(tmp_path):
    midnight = datetime.datetime(2026, 9, 26, 0, 0, 5).astimezone()
    _write(tmp_path, "2026-09-26", (midnight, [KLM67F]))
    before = (midnight - datetime.timedelta(seconds=10)).timestamp()
    assert air_aircraft.nearest_snapshot(tmp_path, before) is not None


def test_a_torn_last_line_is_skipped(tmp_path):
    path = _write(tmp_path, DAY, (_at(3), [KLM67F]))
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"t": "' + _at(1).isoformat() + '", "aircr')
    snap = air_aircraft.nearest_snapshot(tmp_path, AT.timestamp())
    assert snap["t"] == _at(3).isoformat()


def test_listing_is_nearest_first_and_only_named_icao_aircraft():
    snap = {"t": AT.isoformat(), "aircraft": [
        EZY67KZ, KLM67F,
        {"hex": "4bb299", "flight": "", "t": "B738", "alt_baro": 3000, "lat": 52.2, "lon": 4.4},
        {"hex": "~2a1b3c", "flight": "TISB1", "t": None, "alt_baro": 3000, "lat": 52.2,
         "lon": 4.4}]}
    got = air_aircraft.listing(snap)
    assert [a["flight"] for a in got] == ["KLM67F", "EZY67KZ"]
    assert got[0] == {"hex": "4864eb", "flight": "KLM67F", "type": "E295", "reg": "PH-NXN",
                      "alt": 10150, "km": 20.1}


def test_state_of_carries_what_a_strip_shows():
    snap = {"t": AT.isoformat(), "aircraft": [EZY67KZ, KLM67F]}
    state = air_aircraft.state_of(snap, "4864EB")
    assert state == {"flight": "KLM67F", "t": "E295", "r": "PH-NXN", "alt_baro": 10150,
                     "nav_altitude_mcp": None, "nav_heading": None, "track": 328.86,
                     "airline": "KLM"}
    assert air_aircraft.state_of(snap, "abcdef") is None
