"""Searching the AIS cache.

"Is this vessel in the cache and when was it last seen" came up repeatedly through August and
currently takes a Python one-liner. That is the whole reason this screen exists.
"""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import vessels_view as view  # noqa: E402


def _vessel(**over):
    entry = {"mmsi": "244123456", "name": "PASHA", "callsign": "PBZL", "type": "tanker",
             "destination": "NLRTM", "draught": 7.4, "latitude": 52.0, "longitude": 4.0,
             "last_seen": "2026-08-19T10:00:00+00:00", "source": "aishub"}
    entry.update(over)
    return entry


def test_search_matches_name_mmsi_and_callsign_case_insensitively():
    entries = [_vessel(), _vessel(mmsi="311000111", name="CONDOR", callsign="PA2864")]
    assert view.search(entries, text="pasha").total == 1
    assert view.search(entries, text="311000").total == 1
    assert view.search(entries, text="pa2864").total == 1


def test_an_empty_search_returns_everything_newest_first():
    entries = [_vessel(mmsi="1", last_seen="2026-08-19T09:00:00+00:00"),
               _vessel(mmsi="2", last_seen="2026-08-19T11:00:00+00:00")]
    page = view.search(entries)
    assert [r["mmsi"] for r in page.rows] == ["2", "1"]


def test_a_vessel_with_no_last_seen_sorts_last_rather_than_crashing():
    entries = [_vessel(mmsi="1", last_seen=None),
               _vessel(mmsi="2", last_seen="2026-08-19T11:00:00+00:00")]
    assert [r["mmsi"] for r in view.search(entries).rows] == ["2", "1"]


def test_detail_returns_the_whole_entry_for_one_mmsi():
    entries = [_vessel(), _vessel(mmsi="311000111", name="CONDOR")]
    assert view.detail(entries, "311000111")["name"] == "CONDOR"
    assert view.detail(entries, "999") is None


def test_a_name_shared_by_two_mmsis_is_reported_as_shared():
    """A shared name is not an identification. The Vessels screen must show which names
    cannot be trusted on their own."""
    entries = [_vessel(mmsi="1", name="SEA STAR"), _vessel(mmsi="2", name="SEA STAR"),
               _vessel(mmsi="3", name="CONDOR")]
    shared = view.duplicate_names(entries)
    assert shared == {"SEA STAR": ["1", "2"]}
    assert view.search(entries, text="sea star").rows[0]["name_shared"] is True
    assert view.search(entries, text="condor").rows[0]["name_shared"] is False


def test_conversations_for_a_vessel_are_found_by_mmsi_not_by_name():
    records = [{"mmsi": "244123456", "vessel": "PASHA", "start": "2026-08-19T10:00:00",
                "channel": "CH01", "turns": []},
               {"mmsi": "311000111", "vessel": "CONDOR", "start": "2026-08-19T09:00:00",
                "channel": "CH01", "turns": []}]
    found = view.conversations_for(records, "244123456")
    assert len(found) == 1 and found[0]["vessel"] == "PASHA"
