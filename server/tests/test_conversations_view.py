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
    # "ais-matched", not "ais-confirmed", since 2026-08-20: neither turn here carries
    # live_seen, and without it the age of the ship's last fix is unknown. A match to a ship
    # that was days away is not a confirmation, and not knowing which it is cannot be allowed
    # to read as the good case. See _live_match.
    assert out[1]["live_match"] == "ais-matched"


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


def test_detail_also_carries_the_shared_name_safe_label():
    """A caller that only ever fetches detail() -- the Vessels screen's conversation links are
    exactly this -- must see the same NAME (MMSI) form summarise() gives the list, not the bare
    `vessel` field. Regression pin: detail() used to omit `label` entirely, and the one caller
    that reached it without also holding a fresh list fell back to the ambiguous bare name."""
    detail_out = view.detail(_record(vessel="SEA STAR", mmsi="311000111"))
    assert detail_out["label"] == "SEA STAR (311000111)"


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


# -- type tooltips ----------------------------------------------------------------
#
# A conversation stores the type CATEGORY as a word ("Tanker"), which loses the hazard digit.
# The proxy now stores type_code alongside it, so the panel can offer the full reading. Records
# written before that simply have no code, and must degrade rather than invent one.


def test_a_row_carries_the_full_type_reading_when_the_code_was_stored():
    row = view.summarise({"vessel": "PASHA", "mmsi": "244123456",
                          "type": "Tanker", "type_code": 82})
    assert row["type"] == "Tanker"
    assert "category B" in row["type_detail"]


def test_an_older_record_without_a_code_has_no_tooltip_rather_than_a_wrong_one():
    row = view.summarise({"vessel": "PASHA", "mmsi": "244123456", "type": "Tanker"})
    assert row["type"] == "Tanker"
    assert row["type_detail"] is None


def test_the_detail_view_offers_the_same_reading_as_the_row():
    record = {"vessel": "PASHA", "mmsi": "244123456", "type": "Cargo", "type_code": 70}
    assert view.detail(record)["type_detail"] == view.summarise(record)["type_detail"]


# -- how confident the per-turn AIS match may sound ---------------------------------
#
# The per-turn matcher runs at AIS_NAME_MIN_SCORE=76 with no recency check at all, while the
# resolver retrieves at 85 and refuses to promote a live match whose ship was not seen inside
# LIVE_MATCH_MAX_AGE_MIN. Measured on the live store on 2026-08-20: 34 of 161 turns labelled
# "AIS-confirmed" (21%) named a ship the resolver would have refused as stale -- AUGUSTA seven
# days old, VIPER 109 hours, NELLIE 108. The word "confirmed" has to be earned.


def _turn_row(**over):
    turn = {"time": "14:41:59", "raw": "Kung Gustav", "text": "Kung Gustav",
            "live_vessel": "AUGUSTA", "live_mmsi": "244850771",
            "live_seen": "2026-08-20 14:30:00"}
    turn.update(over)
    return view.detail({"start": "2026-08-20 14:41:59", "turns": [turn]})["turns"][0]


def test_a_recent_match_is_confirmed():
    assert _turn_row()["live_match"] == "ais-confirmed"


def test_a_stale_match_is_not_called_confirmed():
    # AUGUSTA's real case: matched at 76.9 off "Gustav", last seen seven days earlier.
    row = _turn_row(live_seen="2026-08-13 09:02:51")
    assert row["live_match"] == "ais-stale"
    assert round(row["live_age_hours"]) == 174


def test_the_boundary_is_the_resolvers_own_freshness_rule():
    assert _turn_row(live_seen="2026-08-20 08:45:00")["live_match"] == "ais-confirmed"
    assert _turn_row(live_seen="2026-08-20 08:35:00")["live_match"] == "ais-stale"


def test_a_record_stored_before_the_age_was_kept_claims_no_confirmation():
    # Every turn written before 2026-08-20 lacks live_seen. Silence about the age is not
    # evidence of freshness, so those must not keep the strongest wording.
    row = _turn_row(live_seen=None)
    assert row["live_match"] == "ais-matched"
    assert row["live_age_hours"] is None


def test_a_name_with_no_mmsi_is_still_heard_only():
    assert _turn_row(live_mmsi=None, live_seen=None)["live_match"] == "heard-only"


def test_no_live_vessel_means_no_claim_at_all():
    assert _turn_row(live_vessel=None, live_mmsi=None)["live_match"] is None


def test_an_unparseable_timestamp_does_not_become_a_confirmation():
    assert _turn_row(live_seen="not a date")["live_match"] == "ais-matched"


def test_a_match_seen_after_the_call_is_still_confirmed():
    # The cache is written asynchronously, so a fix stamped a little after the turn is normal
    # and must not read as a negative age that fails the comparison.
    assert _turn_row(live_seen="2026-08-20 14:45:00")["live_match"] == "ais-confirmed"


# -- the confirmation threshold follows the operator's setting --------------------
#
# AIS_LIVE_MATCH_MAX_AGE_MIN is what the RESOLVER uses to decide a live match is too stale to
# promote. The screen must use the same number, or changing the setting makes the two disagree
# silently: the resolver would refuse a match the screen still calls confirmed.


def _row_with(threshold_h, live_seen):
    turn = {"time": "14:41:59", "live_vessel": "AUGUSTA", "live_mmsi": "244850771",
            "live_seen": live_seen}
    return view.detail({"start": "2026-08-20 14:41:59", "turns": [turn]},
                       confirm_max_age_h=threshold_h)["turns"][0]


def test_a_tightened_setting_tightens_the_label():
    # 120 minutes: a two-and-a-half-hour-old fix is no longer a confirmation.
    assert _row_with(2.0, "2026-08-20 12:11:00")["live_match"] == "ais-stale"
    assert _row_with(2.0, "2026-08-20 13:11:00")["live_match"] == "ais-confirmed"


def test_a_loosened_setting_loosens_it_too():
    assert _row_with(24.0, "2026-08-19 20:00:00")["live_match"] == "ais-confirmed"


def test_the_default_is_the_resolvers_own_360_minutes():
    assert view.confirm_max_age_hours({}) == 6.0
    assert view.confirm_max_age_hours({"AIS_LIVE_MATCH_MAX_AGE_MIN": ""}) == 6.0


def test_the_setting_is_read_in_minutes():
    assert view.confirm_max_age_hours({"AIS_LIVE_MATCH_MAX_AGE_MIN": "120"}) == 2.0


def test_disabling_the_resolvers_bound_does_not_make_the_screen_assert_more():
    # 0 means "no bound" to the resolver -- a rollback lever for identification behaviour.
    # It is not an instruction to call a week-old fix a confirmation, so the DISPLAY keeps the
    # default. The alternative would restore exactly the misleading label this replaced.
    assert view.confirm_max_age_hours({"AIS_LIVE_MATCH_MAX_AGE_MIN": "0"}) == 6.0


def test_a_nonsense_setting_falls_back_rather_than_crashing_the_screen():
    for bad in ("abc", "-5", "  ", None):
        assert view.confirm_max_age_hours({"AIS_LIVE_MATCH_MAX_AGE_MIN": bad}) == 6.0
