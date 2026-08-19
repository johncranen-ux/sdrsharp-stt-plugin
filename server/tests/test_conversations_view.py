"""Turning 300 conversation records into something a phone can be sent.

The projection is deliberately lossy for the list and complete for one record: the list is
polled, the detail is opened once.
"""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import conversations_view as view  # noqa: E402


def _record(**over):
    record = {
        "vessel": "PASHA", "mmsi": "244123456", "callsign": "PBZL", "type": "tanker",
        "via_callsign": None, "evidence": "name heard in turn 1", "confidence": "high",
        "channel": "CH01", "start": "2026-08-19T10:00:00", "end": "2026-08-19T10:01:00",
        "destination": "NLRTM", "draught": 7.4, "latitude": 52.0, "longitude": 4.0,
        "resolver_candidates": [], "candidates": [],
        "turns": [{"time": "10:00:00", "text": "Pasha, Maas Approach", "raw": "Pasha, Mass Approach",
                   "live_vessel": "PASHA", "live_mmsi": "244123456"}],
    }
    record.update(over)
    return record


def test_a_summary_carries_what_the_list_shows_and_no_transcript():
    row = view.summarise(_record())
    assert row["vessel"] == "PASHA"
    assert row["turn_count"] == 1
    assert row["identified"] is True
    assert "turns" not in row and "resolver_candidates" not in row


def test_an_unidentified_row_is_marked_and_keeps_no_confidence():
    """Spec Section 5: "high confidence" on an unidentified row reads as a contradiction.
    The confidence describes the reasoning, not an identification that was not made."""
    row = view.summarise(_record(vessel=None, mmsi=None, confidence="high"))
    assert row["identified"] is False
    assert row["confidence"] is None


def test_the_id_is_stable_across_two_reads_of_the_same_record():
    assert view.conversation_id(_record()) == view.conversation_id(_record())


def test_two_conversations_on_different_channels_at_one_instant_get_different_ids():
    a = view.conversation_id(_record(channel="CH01"))
    b = view.conversation_id(_record(channel="CH16"))
    assert a != b


def test_detail_exposes_the_three_layer_text_chain():
    """raw -> text -> conv: what the regex pass and the LLM pass each changed. Only visible
    through the API until now."""
    turn = {"time": "10:00", "raw": "Mass Aproach", "text": "Maas Approach",
            "conv": "Maas Approach, over", "live_vessel": None, "live_mmsi": None}
    chain = view.detail(_record(turns=[turn]))["turns"][0]
    assert chain["raw"] == "Mass Aproach"
    assert chain["text"] == "Maas Approach"
    assert chain["conv"] == "Maas Approach, over"
    assert chain["changed_by_regex"] is True
    assert chain["changed_by_llm"] is True


def test_a_turn_the_correction_pass_left_alone_says_so_rather_than_inventing_a_layer():
    turn = {"time": "10:00", "raw": "Maas Approach", "text": "Maas Approach",
            "live_vessel": None, "live_mmsi": None}
    chain = view.detail(_record(turns=[turn]))["turns"][0]
    assert chain["conv"] is None
    assert chain["changed_by_regex"] is False
    assert chain["changed_by_llm"] is False


def test_a_heard_name_with_no_ais_match_is_distinguished_from_a_confirmed_one():
    """live_vessel set with live_mmsi null means the name was heard and AIS had no such ship."""
    turns = [{"time": "10:00", "raw": "x", "text": "x", "live_vessel": "GHOST", "live_mmsi": None},
             {"time": "10:01", "raw": "y", "text": "y", "live_vessel": "PASHA",
              "live_mmsi": "244123456"}]
    out = view.detail(_record(turns=turns))["turns"]
    assert out[0]["live_match"] == "heard-only"
    assert out[1]["live_match"] == "ais-confirmed"


def test_filtering_by_identified_and_by_channel():
    records = [_record(), _record(vessel=None, mmsi=None), _record(channel="CH16")]
    assert view.query(records, identified=True).total == 2
    assert view.query(records, identified=False).total == 1
    assert view.query(records, channel="CH16").total == 1


def test_free_text_search_covers_the_transcript_and_the_vessel():
    records = [_record(), _record(vessel="CONDOR", turns=[
        {"time": "1", "text": "buoy one six", "raw": "buoy 16",
         "live_vessel": None, "live_mmsi": None}])]
    assert view.query(records, text="condor").total == 1
    assert view.query(records, text="BUOY").total == 1      # case-insensitive, transcript too
    assert view.query(records, text="nothing here").total == 0


def test_rows_are_newest_first_and_paged():
    records = [_record(start=f"2026-08-19T10:{n:02d}:00") for n in range(5)]
    page = view.query(records, limit=2, offset=0)
    assert [r["start"] for r in page.rows] == ["2026-08-19T10:04:00", "2026-08-19T10:03:00"]
    assert page.total == 5
    assert view.query(records, limit=2, offset=4).rows[0]["start"] == "2026-08-19T10:00:00"


def test_a_shared_name_is_reported_with_its_mmsi_rather_than_by_name_alone():
    """Spec Section 5: seven labelled conversations were distorted by a name collision."""
    row = view.summarise(_record(vessel="SEA STAR", mmsi="311000111"))
    assert row["label"] == "SEA STAR (311000111)"


def test_detail_drops_confidence_on_unidentified_records():
    """Spec Section 5: the confidence describes the resolver's reasoning, and printed
    beside "unidentified" it reads as a contradiction."""
    detail_out = view.detail(_record(vessel=None, mmsi=None, confidence="high"))
    assert detail_out["identified"] is False
    assert detail_out["confidence"] is None


def test_records_with_missing_start_sort_to_the_end():
    """The sort key and conversation_id both null-guard start: records without a start
    should sort to the end rather than crashing."""
    records = [
        _record(start="2026-08-19T10:02:00"),
        _record(start=None),
        _record(start="2026-08-19T10:01:00"),
    ]
    page = view.query(records)
    # Newest first (reverse sort), but None sorts to the end
    assert page.rows[0]["start"] == "2026-08-19T10:02:00"
    assert page.rows[1]["start"] == "2026-08-19T10:01:00"
    assert page.rows[2]["start"] is None
