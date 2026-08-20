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


# -- wildcards ------------------------------------------------------------------
#
# Callsigns are spelled out letter by letter over VHF and a single character is the usual
# thing to lose, so "the callsign was P-something-Q-Q" has to be expressible. `?` standing for
# exactly one character is the part that matters; `*` is the convenience.


def test_a_question_mark_stands_for_exactly_one_character():
    entries = [_vessel(mmsi="1", callsign="PBQQ"), _vessel(mmsi="2", callsign="PQQ"),
               _vessel(mmsi="3", callsign="PBBQQ")]
    assert [r["mmsi"] for r in view.search(entries, text="P?QQ").rows] == ["1"]


def test_a_star_stands_for_any_run_including_none():
    entries = [_vessel(mmsi="1", callsign="PBQQ"), _vessel(mmsi="2", callsign="PQQ"),
               _vessel(mmsi="3", callsign="PBBBQQ"), _vessel(mmsi="4", callsign="XYZ")]
    found = {r["mmsi"] for r in view.search(entries, text="P*QQ").rows}
    assert found == {"1", "2", "3"}


def test_a_wildcard_search_still_matches_inside_a_field():
    # Substring semantics, not full-match: plain text already behaves this way, and a pattern
    # that suddenly demanded the whole field would be a trap rather than a refinement.
    entries = [_vessel(mmsi="1", callsign="PBQQ1")]
    assert view.search(entries, text="P?QQ").total == 1


def test_wildcards_work_across_every_searchable_field():
    entries = [_vessel(mmsi="244123456", name="PASHA", callsign="PBZL", destination="NLRTM")]
    for needle in ("244*456", "P?SHA", "PB?L", "NL*M"):
        assert view.search(entries, text=needle).total == 1, needle


def test_wildcard_matching_is_case_insensitive_like_plain_text():
    assert view.search([_vessel(callsign="PBQQ")], text="p?qq").total == 1


def test_regex_metacharacters_are_literal_not_patterns():
    # A vessel name really can contain brackets or a dot, and a needle like "." must not
    # quietly become "match any character" and return the whole cache.
    entries = [_vessel(mmsi="1", name="CONDOR (II)"), _vessel(mmsi="2", name="PASHA")]
    assert [r["mmsi"] for r in view.search(entries, text="(II)").rows] == ["1"]
    assert view.search(entries, text=".").total == 0
    assert view.search(entries, text="c+").total == 0


def test_a_plain_search_is_unchanged_by_the_wildcard_support():
    entries = [_vessel(mmsi="1", name="PASHA"), _vessel(mmsi="2", name="CONDOR")]
    assert [r["mmsi"] for r in view.search(entries, text="pash").rows] == ["1"]


def test_a_pattern_that_matches_nothing_returns_nothing_rather_than_everything():
    entries = [_vessel(mmsi="1", callsign="PBQQ")]
    assert view.search(entries, text="Z?ZZ").total == 0


def test_a_bare_star_matches_every_entry():
    entries = [_vessel(mmsi="1"), _vessel(mmsi="2")]
    assert view.search(entries, text="*").total == 2


def test_a_wildcard_pattern_is_compiled_once_not_per_entry():
    # ~6000 cache entries x 4 fields on every debounced keystroke: recompiling inside the loop
    # would put 24000 compiles behind each one.
    import re

    entries = [_vessel(mmsi=str(n)) for n in range(50)]
    original, calls = re.compile, []

    def counting_compile(*args, **kwargs):
        calls.append(args[0])
        return original(*args, **kwargs)

    re.compile = counting_compile
    try:
        view.search(entries, text="P?SHA")
    finally:
        re.compile = original
    assert len(calls) == 1


# -- type tooltips ----------------------------------------------------------------
#
# The Vessels screen shows the bare AIS code, which is unreadable on its own. The row carries
# the full ITU reading so the UI can put it in a tooltip without shipping the table twice.


def test_a_row_carries_the_readable_type_beside_the_code():
    page = view.search([_vessel(type=80)])
    assert page.rows[0]["type"] == 80
    assert page.rows[0]["type_detail"] == "Tanker — all ships of this type (AIS type 80)"


def test_the_hazard_digit_survives_into_the_tooltip():
    page = view.search([_vessel(type=82)])
    assert "category B" in page.rows[0]["type_detail"]


def test_a_vessel_broadcasting_no_type_has_no_tooltip_rather_than_a_guess():
    page = view.search([_vessel(type=None)])
    assert page.rows[0]["type_detail"] is None


def test_a_string_type_code_is_read_the_same_as_an_int():
    # AISHub delivers TYPE as a string.
    assert view.search([_vessel(type="70")]).rows[0]["type_detail"] == \
           view.search([_vessel(type=70)]).rows[0]["type_detail"]
