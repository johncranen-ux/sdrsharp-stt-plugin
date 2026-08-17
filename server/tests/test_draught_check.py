"""Tests for draught_check.py's spoken-number reading and the physical check.

Every phrasing below is taken from real Maas Approach traffic in the capture corpus. The
parser is the whole risk in this tool: the first attempt at this measurement matched
"draught" but not "draft" and so found 8 stated draughts where there were 30, which would
have made the check look four times rarer than it is.

Run with: py -m pytest server/tests/test_draught_check.py -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from draught_check import IMPLAUSIBLE_LT, check, spoken_draughts  # noqa: E402


@pytest.mark.parametrize("text,expected", [
    # -- forms seen in the corpus -------------------------------------------------
    ("Sea-water draft seven point nine.", [7.9]),
    ("Present maximum draft is one seven decimal seven two, even kill, over.", [17.72]),
    ("Maximum draft is 3.4 meters, over.", [3.4]),
    ("maximum draught six decimal three metres", [6.3]),
    ("maximum draught five decimal nine, pit number for today", [5.9]),
    # The readback repeats the figure but not the keyword, so this yields one reading, not
    # two -- the check is keyword-anchored by design, to keep stray numbers out.
    ("Vessel maximum drop one six decimal one zero, repeat one six decimal one zero.",
     [16.10]),
    ("my present maximum draught five decimal three minutes, over", [5.3]),
    ("our draught is eleven point six", [11.6]),
    ("draught 5.85 meters", [5.85]),
    ("present maximum draft is six meters", [6.0]),
    # Readback: the figure is stated twice in one breath. Found in real traffic on
    # 2026-08-09 (FORTUNA) and 2026-08-11 (SUAPE EXPRESS); the first version of this
    # parser ran straight through the repeat and returned 5.353 and 6.969.
    ("Our present maximum draught in seawater is five decimal three, five decimal three, "
     "over.", [5.3]),
    ("My maximum draught is six decimal nine, six decimal nine metres, over.", [6.9]),
    # -- must NOT produce a reading -----------------------------------------------
    ("What is your present maximum draught, over?", []),          # question, no number
    ("Maas Approach, this is Eems Dundee, good morning.", []),     # no keyword at all
    ("maximum draught is not reported at this time", []),          # keyword, no number
])
def test_spoken_draughts(text, expected):
    assert spoken_draughts(text) == pytest.approx(expected)


def test_implausible_values_are_rejected_as_not_draughts():
    """A number after the keyword is not automatically a draught."""
    # An ETA read back right after the word would otherwise parse as 1430 m.
    assert spoken_draughts("maximum draught, ETA one four three zero") == []
    # Zero is "not reported", not a hull drawing nothing.
    assert spoken_draughts("maximum draught zero") == []


def _conv(vessel, length, text, confidence="high"):
    return {"start": "2026-08-13 10:00:00", "end": "2026-08-13 10:01:00",
            "vessel": vessel, "mmsi": "1", "callsign": "X", "type": "Tanker",
            "length": length, "draught": 1.4, "confidence": confidence,
            "evidence": "", "turns": [{"text": text}]}


def test_flags_the_pleasure_craft_case():
    """The live failure this exists for: 6.3 m stated, 20 m hull named at high confidence."""
    res = check([_conv("ENERGY", 20, "maximum draught six decimal three metres")])
    assert len(res["flagged"]) == 1
    assert res["flagged"][0]["lt"] == pytest.approx(20 / 6.3)
    assert res["flagged"][0]["lt"] < IMPLAUSIBLE_LT


def test_passes_a_real_merchant_hull():
    """EEMS DUNDEE: 108 m stating 4.7 m gives 23.0 and must not be flagged."""
    res = check([_conv("EEMS DUNDEE", 108, "maximum draught four decimal seven")])
    assert res["flagged"] == []
    assert res["scored"][0]["lt"] == pytest.approx(108 / 4.7)


def test_partitions_what_cannot_be_scored():
    """Only conversations with all three of vessel, length and a spoken draught count."""
    rows = [
        _conv("A", 100, "no numbers here at all"),          # no draught spoken
        _conv(None, 100, "maximum draught six decimal one"),  # nobody named
        _conv("C", None, "maximum draught six decimal one"),  # no hull length
        _conv("D", 100, "maximum draught six decimal one"),   # scorable
    ]
    res = check(rows)
    assert (res["no_draught"], res["unnamed"], res["no_length"]) == (1, 1, 1)
    assert len(res["scored"]) == 1


def test_day_filter_selects_only_requested_days():
    a = _conv("A", 20, "maximum draught six decimal three")
    b = dict(_conv("B", 20, "maximum draught six decimal three"),
             start="2026-08-14 10:00:00")
    assert len(check([a, b], days={"2026-08-14"})["scored"]) == 1


def test_uses_the_largest_stated_draught():
    """A readback repeats the figure; a garbled first pass must not win over a clean one."""
    res = check([_conv("A", 100, "maximum draught five decimal nine. "
                                 "maximum draught nine decimal five, correct")])
    assert res["scored"][0]["said"] == pytest.approx(9.5)
