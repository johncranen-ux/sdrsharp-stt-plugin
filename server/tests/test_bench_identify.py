"""Tests for bench_identify.py -- scoring vessel identification against hand-labelled
conversations.

The transcription side has had bench.py and a WER figure since the beginning; identification
has been changed repeatedly on the strength of one-off scripts. Two bugs found by hand on
2026-08-04 (PECHORA STAR losing an exact callsign to a 76.9 name match, and one THULELAND
conversation resolving as three different ships) are what this exists to have caught.

Run with: py -m pytest server/tests -v
"""

import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import bench_identify as bi  # noqa: E402


# ---------------------------------------------------------------------------
# Label parsing
# ---------------------------------------------------------------------------

_LABELS = """\
# identification ground truth
# <start>\t<end>\t<mmsi or ->\t<note>
2026-08-04 13:58:14\t2026-08-04 13:59:08\t266248000\tTHULELAND, one exchange
2026-08-04 12:00:00\t2026-08-04 12:00:30\t-\tnobody identifiable

2026-08-04 10:03:36\t2026-08-04 10:04:04\t215760000\tPECHORA STAR
"""


def _write(tmp_path, text, name="labels.txt"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def test_labels_are_parsed(tmp_path):
    labels = bi.parse_labels(_write(tmp_path, _LABELS))
    assert len(labels) == 3
    assert labels[0].mmsi == "266248000"
    assert labels[0].note == "THULELAND, one exchange"


def test_a_dash_means_nobody_was_identifiable(tmp_path):
    """Distinct from an unlabelled conversation: this asserts that naming *anyone* is wrong,
    which is what stops a benchmark rewarding confident guesses."""
    labels = bi.parse_labels(_write(tmp_path, _LABELS))
    assert labels[1].mmsi is None
    assert labels[1].identifiable is False


def test_comments_and_blank_lines_are_skipped(tmp_path):
    assert len(bi.parse_labels(_write(tmp_path, "# only a comment\n\n\n"))) == 0


@pytest.mark.parametrize("bad", [
    "2026-08-04 13:58:14\t266248000\n",              # too few fields
    "not-a-date\t2026-08-04 13:59:08\t266248000\n",  # unparseable start
])
def test_a_malformed_label_is_rejected_loudly(bad, tmp_path):
    """A silently dropped label would quietly shrink the corpus and flatter the score."""
    with pytest.raises(ValueError):
        bi.parse_labels(_write(tmp_path, bad))


# A name is what you hear on the recording; an MMSI is a number you would have to go and
# look up for every line. Both are accepted, and a name is resolved by EXACT cache key --
# never fuzzily, or the ground truth would inherit the very matching it exists to measure.

_LOOKUP = {"THULELAND": "266248000", "PECHORA STAR": "215760000"}


def test_a_vessel_name_can_be_used_instead_of_an_mmsi(tmp_path):
    labels = bi.parse_labels(
        _write(tmp_path, "2026-08-04 13:58:14\t2026-08-04 13:59:08\tTHULELAND\theard it\n"),
        lookup=_LOOKUP)
    assert labels[0].mmsi == "266248000"


def test_a_name_is_matched_case_insensitively(tmp_path):
    labels = bi.parse_labels(
        _write(tmp_path, "2026-08-04 13:58:14\t2026-08-04 13:59:08\tPechora Star\tx\n"),
        lookup=_LOOKUP)
    assert labels[0].mmsi == "215760000"


def test_an_unknown_vessel_name_is_rejected(tmp_path):
    """Better to stop than to score against a vessel that is not in the cache: the label
    would silently never match and quietly depress recall."""
    with pytest.raises(ValueError, match="NOT A SHIP"):
        bi.parse_labels(
            _write(tmp_path, "2026-08-04 13:58:14\t2026-08-04 13:59:08\tNOT A SHIP\tx\n"),
            lookup=_LOOKUP)


# Tabs turned out to be the single biggest obstacle to actually labelling: an editor that
# inserts spaces silently breaks every line touched, and the whole file then fails to parse.
# The two timestamps have a fixed shape, so they can be recognised whatever separates them,
# which leaves the vessel unambiguous even when it contains spaces.

def test_space_separated_fields_are_accepted(tmp_path):
    labels = bi.parse_labels(
        _write(tmp_path, "2026-07-31 12:39:48 2026-07-31 12:39:52 THULELAND\n"), lookup=_LOOKUP)
    assert labels[0].mmsi == "266248000"
    assert labels[0].start.hour == 12 and labels[0].end.minute == 39


def test_a_multi_word_vessel_name_survives_space_separation(tmp_path):
    """'PECHORA STAR' contains a space itself, which is exactly why the timestamps have to
    be matched by shape rather than by splitting on whitespace."""
    labels = bi.parse_labels(
        _write(tmp_path, "2026-08-04 10:03:36 2026-08-04 10:04:04 PECHORA STAR\n"), lookup=_LOOKUP)
    assert labels[0].mmsi == "215760000"


def test_tabs_still_work(tmp_path):
    labels = bi.parse_labels(
        _write(tmp_path, "2026-07-31 12:39:48\t2026-07-31 12:39:52\tTHULELAND\tnote here\n"),
        lookup=_LOOKUP)
    assert labels[0].mmsi == "266248000" and labels[0].note == "note here"


def test_a_note_is_kept_off_the_vessel_by_a_tab(tmp_path):
    labels = bi.parse_labels(
        _write(tmp_path, "2026-07-31 12:39:48 2026-07-31 12:39:52 THULELAND\tmerged 0000+0001\n"),
        lookup=_LOOKUP)
    assert labels[0].mmsi == "266248000"
    assert labels[0].note == "merged 0000+0001"


def test_a_note_run_into_the_vessel_says_so(tmp_path):
    """The remaining ambiguity, and the one people actually hit: without a tab there is no
    way to tell where a multi-word vessel name ends and a note begins, so say that plainly
    rather than guessing at a ship."""
    with pytest.raises(ValueError, match="TAB"):
        bi.parse_labels(_write(
            tmp_path,
            "2026-07-31 12:39:48 2026-07-31 12:39:52 MISTRAL Maas Approach, entering your area\n"),
            lookup={"MISTRAL": "1"})


# A name two ships share cannot be ground truth
#
# ATLANTIC PRESTIGE, 2026-08-18: the cache held two of them -- 538010447 (callsign V7A6052,
# draught 10.1) and 244700991 (PB8309, draught 2.0, an inland barge). The label said the
# NAME, and resolution went through a dict holding one entry per name, so the truth silently
# became the barge. A candidate change was then scored as -0.9 precision for picking the
# ship that had just spelled out V7A6052 on air. The benchmark was wrong, not the change.
#
# A label that cannot distinguish two ships is not ground truth, and guessing between them
# is worse than refusing: it produces a number that looks like evidence.

_TWINS = {"ATLANTIC PRESTIGE": ["538010447", "244700991"], "THULELAND": "266248000"}


def test_a_name_carried_by_two_ships_is_not_scored(tmp_path):
    """Skipped, not resolved by guess. The file's own instructions set this policy: "an
    unlabelled conversation is simply not scored; a guessed one corrupts every number
    computed from this file"."""
    labels = bi.parse_labels(
        _write(tmp_path, "2026-08-13 19:36:22\t2026-08-13 19:36:46\tATLANTIC PRESTIGE\tx\n"
                         "2026-08-04 13:58:14\t2026-08-04 13:59:08\tTHULELAND\ty\n"),
        lookup=_TWINS)
    assert [l.mmsi for l in labels] == ["266248000"], "the rest of the file must still parse"


def test_skipping_an_ambiguous_label_is_loud(tmp_path, capsys):
    """Dropping a line silently would shrink the corpus and flatter every score computed
    from it -- the same reason a malformed line raises rather than being skipped."""
    bi.parse_labels(
        _write(tmp_path, "2026-08-13 19:36:22\t2026-08-13 19:36:46\tATLANTIC PRESTIGE\tx\n"),
        lookup=_TWINS)
    warned = capsys.readouterr().err
    assert "ATLANTIC PRESTIGE" in warned
    assert "538010447" in warned and "244700991" in warned, "say which ships, so it is fixable"
    assert "NOT SCORED" in warned, "the consequence has to be unmissable"


def test_an_mmsi_is_still_accepted_for_a_shared_name(tmp_path):
    """The fix the error asks for: name the ship by the one identifier that distinguishes."""
    labels = bi.parse_labels(
        _write(tmp_path, "2026-08-13 19:36:22\t2026-08-13 19:36:46\t538010447\tx\n"),
        lookup=_TWINS)
    assert labels[0].mmsi == "538010447"


def test_a_name_carried_by_exactly_one_ship_still_resolves(tmp_path):
    labels = bi.parse_labels(
        _write(tmp_path, "2026-08-04 13:58:14\t2026-08-04 13:59:08\tTHULELAND\tx\n"),
        lookup={"THULELAND": ["266248000"]})
    assert labels[0].mmsi == "266248000"


def test_a_name_without_a_lookup_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="AIS cache"):
        bi.parse_labels(
            _write(tmp_path, "2026-08-04 13:58:14\t2026-08-04 13:59:08\tTHULELAND\tx\n"))


# ---------------------------------------------------------------------------
# Scoring
#
# Scored per transmission rather than per stored exchange, because over-segmentation is one
# of the failures being measured: the reported THULELAND conversation produced three stored
# exchanges naming three different ships, and a per-exchange score would call that "one
# right, two wrong" while hiding that all five turns belonged to one vessel.
# ---------------------------------------------------------------------------

def _exchange(vessel, mmsi, times, start=None, end=None, channel="160,650"):
    return {"vessel": vessel, "mmsi": mmsi, "channel": channel,
            "start": start or f"2026-08-04 {times[0]}", "end": end or f"2026-08-04 {times[-1]}",
            "turns": [{"time": t.split(" ")[-1], "text": "x"} for t in times]}


_THULELAND_TRUTH = "2026-08-04 13:58:14\t2026-08-04 13:59:08\t266248000\tTHULELAND\n"


def test_a_fully_correct_conversation_scores_clean(tmp_path):
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [_exchange("THULELAND", "266248000",
                        ["13:58:14", "13:58:25", "13:58:55", "13:59:04", "13:59:08"])]
    r = bi.score(labels, stored)
    assert (r["correct"], r["wrong"], r["missed"]) == (5, 0, 0)
    assert r["fragments"] == 0


def test_the_reported_split_conversation_is_scored_as_it_really_went(tmp_path):
    """One conversation, three stored exchanges, three different ships. Only the opening
    turn was right."""
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [
        _exchange("THULELAND", "266248000", ["13:58:14"]),
        _exchange("SHALOM", "244810551", ["13:58:25"]),
        _exchange("GOOILAND", "244700270", ["13:58:55", "13:59:04", "13:59:08"]),
    ]
    r = bi.score(labels, stored)
    assert (r["correct"], r["wrong"]) == (1, 4)
    assert r["fragments"] == 1, "one conversation covered by more than one exchange"
    assert r["exchanges_per_conversation"] == 3.0


def test_naming_nobody_is_a_miss_not_an_error(tmp_path):
    """Distinguished deliberately: a miss costs recall, a wrong name costs precision, and
    the two have very different consequences on screen."""
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [_exchange(None, None, ["13:58:14", "13:58:25"])]
    r = bi.score(labels, stored)
    assert (r["correct"], r["wrong"], r["missed"]) == (0, 0, 2)


def test_naming_anyone_in_an_unidentifiable_conversation_is_wrong(tmp_path):
    labels = bi.parse_labels(tmp_path / "l.txt" if False else _write(
        tmp_path, "2026-08-04 12:00:00\t2026-08-04 12:00:30\t-\tnobody\n"))
    stored = [_exchange("SHALOM", "244810551", ["12:00:00", "12:00:30"])]
    r = bi.score(labels, stored)
    assert r["wrong"] == 2 and r["correct"] == 0


def test_correctly_naming_nobody_is_credited(tmp_path):
    labels = bi.parse_labels(_write(
        tmp_path, "2026-08-04 12:00:00\t2026-08-04 12:00:30\t-\tnobody\n"))
    stored = [_exchange(None, None, ["12:00:00", "12:00:30"])]
    r = bi.score(labels, stored)
    assert r["correct_null"] == 2
    assert r["wrong"] == 0


def test_precision_and_recall_are_reported(tmp_path):
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [
        _exchange("THULELAND", "266248000", ["13:58:14", "13:58:25", "13:58:55"]),
        _exchange("GOOILAND", "244700270", ["13:59:04"]),
    ]
    r = bi.score(labels, stored)
    assert r["precision"] == pytest.approx(0.75)   # 3 of 4 named turns right
    assert r["recall"] == pytest.approx(0.75)      # 3 of 4 identifiable turns found


def test_turns_outside_every_label_are_ignored(tmp_path):
    """Only labelled conversations are scored, so a partly-labelled store is still usable."""
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [
        _exchange("THULELAND", "266248000", ["13:58:14"]),
        _exchange("SHALOM", "244810551", ["09:00:00"]),
    ]
    r = bi.score(labels, stored)
    assert r["scored_turns"] == 1


def test_a_label_matching_no_stored_turn_is_reported(tmp_path):
    """Silently scoring 0/0 would hide a labels file that has drifted from the store."""
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    r = bi.score(labels, [_exchange("X", "1", ["09:00:00"])])
    assert r["labels_with_no_turns"] == 1


def test_channel_must_match(tmp_path):
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [_exchange("THULELAND", "266248000", ["13:58:14"], channel="161,650")]
    assert bi.score(labels, stored)["scored_turns"] == 0


# ---------------------------------------------------------------------------
# Per-transmission rows
#
# The aggregates alone cannot answer "which transmissions changed, and how", which is the
# only question worth asking when comparing two arms. On 2026-08-08 an A/B showed correct
# declines falling 41 -> 30 while `wrong` rose only 3, and there was no way to find the
# transmissions concerned: a turn's bucket family is fixed by its label, so the arithmetic
# only works if segmentation moved turns between label windows. Aggregates hid that.


def test_every_scored_turn_is_reported_individually(tmp_path):
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [_exchange("THULELAND", "266248000", ["13:58:14", "13:58:25"])]
    r = bi.score(labels, stored)
    assert len(r["turns"]) == r["scored_turns"] == 2
    assert {t["outcome"] for t in r["turns"]} == {"correct"}


def test_a_turn_row_carries_what_a_diff_needs(tmp_path):
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [_exchange("GOOILAND", "244700270", ["13:58:14"])]
    row = bi.score(labels, stored)["turns"][0]
    assert row["time"] == "2026-08-04 13:58:14"      # the join key between two arms
    assert row["label"] == "266248000"
    assert row["predicted"] == "244700270"
    assert row["outcome"] == "wrong"


def test_every_outcome_appears_in_the_rows(tmp_path):
    """correct / wrong / missed / correct_null must each be nameable, or a diff cannot
    say which way a transmission moved."""
    labels = bi.parse_labels(_write(
        tmp_path,
        "2026-08-04 13:58:14\t2026-08-04 13:58:30\t266248000\tTHULELAND\n"
        "2026-08-04 12:00:00\t2026-08-04 12:00:30\t-\tnobody\n"))
    stored = [
        _exchange("THULELAND", "266248000", ["13:58:14"]),   # correct
        _exchange("GOOILAND", "244700270", ["13:58:20"]),    # wrong
        _exchange(None, None, ["13:58:30"]),                 # missed
        _exchange(None, None, ["12:00:00"]),                 # correct_null
    ]
    rows = bi.score(labels, stored)["turns"]
    assert {r["outcome"] for r in rows} == {"correct", "wrong", "missed", "correct_null"}


def test_turn_rows_survive_the_json_round_trip(tmp_path):
    """They are written with --out-json and read back by whatever compares two arms."""
    labels = bi.parse_labels(_write(tmp_path, _THULELAND_TRUTH))
    stored = [_exchange("THULELAND", "266248000", ["13:58:14"])]
    assert json.loads(json.dumps(bi.score(labels, stored)))["turns"][0]["outcome"] == "correct"


# ---------------------------------------------------------------------------
# Looking a vessel up while labelling
#
# Field 3 has to be spelled the way AIS spells it, and what you hear on the radio often is
# not. This is a human convenience and is deliberately fuzzy; the label resolution it feeds
# stays exact, so a near-miss shown here still has to be typed correctly to be accepted.
# ---------------------------------------------------------------------------

_FLEET = [
    {"name": "THULELAND", "mmsi": "266248000", "callsign": "SKEI",
     "latitude": 51.90, "longitude": 3.09},
    {"name": "GOOILAND", "mmsi": "244700270", "callsign": "PF4616",
     "latitude": 51.96, "longitude": 5.94},
    {"name": "MSC TEMA VIII", "mmsi": "636024193", "callsign": "5LRK9"},
]


def test_a_substring_finds_the_vessel():
    hits = bi.find_vessels("thule", _FLEET)
    assert [h["name"] for h in hits] == ["THULELAND"]


def test_the_search_is_case_insensitive():
    assert bi.find_vessels("MSC tema", _FLEET)[0]["name"] == "MSC TEMA VIII"


def test_a_misheard_name_still_finds_it():
    """'Tulliland' is what the decoder produced for THULELAND -- no substring matches, so
    the fallback has to be fuzzy or the lookup is useless exactly when it is needed."""
    assert bi.find_vessels("Tulliland", _FLEET)[0]["name"] == "THULELAND"


def test_a_hopeless_query_returns_nothing():
    assert bi.find_vessels("zzzzzzzz", _FLEET) == []


def test_results_carry_what_is_needed_to_label():
    hit = bi.find_vessels("thule", _FLEET)[0]
    assert hit["mmsi"] == "266248000" and hit["callsign"] == "SKEI"


def test_distance_from_the_working_area_is_reported():
    """Two ships can share a name; the near one is the one on the radio. This is also the
    only place the 'matching ignores position' limitation is visible while labelling."""
    hits = bi.find_vessels("land", _FLEET)
    by_name = {h["name"]: h for h in hits}
    assert by_name["THULELAND"]["km"] < by_name["GOOILAND"]["km"]


def test_a_vessel_with_no_position_still_appears():
    hit = next(h for h in bi.find_vessels("tema", _FLEET))
    assert hit["km"] is None, "no position is not a reason to hide it from the search"


# ---------------------------------------------------------------------------
# Bootstrapping a labels file
# ---------------------------------------------------------------------------

def test_labels_are_bootstrapped_from_current_verdicts():
    """Same principle as make_references.py: correcting a draft beats typing from scratch."""
    lines = bi.make_labels([_exchange("THULELAND", "266248000", ["13:58:14", "13:58:25"])])
    body = [ln for ln in lines if not ln.startswith("#")]
    assert len(body) == 1
    assert body[0].startswith("2026-08-04 13:58:14\t2026-08-04 13:58:25\t")
    assert body[0].count("\t") >= 3


def test_an_unidentified_exchange_is_bootstrapped_as_a_dash():
    body = [ln for ln in bi.make_labels([_exchange(None, None, ["13:58:14"])])
            if not ln.startswith("#")]
    assert "\t-\t" in body[0]


def test_only_the_requested_days_are_drafted():
    """Only 07-31 and 08-04 have both stored conversations and their capture audio, so those
    are the only days worth labelling by ear."""
    stored = [_exchange("A", "1", ["13:58:14"], start="2026-07-31 13:58:14"),
              _exchange("B", "2", ["10:03:36"], start="2026-08-03 10:03:36")]
    body = [ln for ln in bi.make_labels(stored, days={"2026-07-31"}) if not ln.startswith("#")]
    assert len(body) == 1 and body[0].startswith("2026-07-31")


def test_the_draft_names_the_clips_to_listen_to():
    """The whole point of labelling these two days: the audio is still on disk, so ground
    truth comes from the recording rather than from the resolver's own guess."""
    stored = [_exchange("THULELAND", "266248000", ["13:58:14", "13:58:25"],
                        start="2026-08-04 13:58:14", end="2026-08-04 13:58:25")]
    clips = [("2026-08-04 13:58:14", "0231", "160,650"),
             ("2026-08-04 13:58:25", "0232", "160,650"),
             ("2026-08-04 15:00:00", "0299", "160,650")]
    body = [ln for ln in bi.make_labels(stored, clips=clips) if not ln.startswith("#")]
    assert "0231_sent.wav" in body[0]
    assert "0232_sent.wav" in body[0]
    assert "0299" not in body[0], "a clip outside the conversation must not be listed"


def test_the_draft_uses_the_vessel_name_not_the_mmsi():
    """So a correct line needs no edit, and a wrong one is corrected by typing what you hear."""
    body = [ln for ln in bi.make_labels(
        [_exchange("THULELAND", "266248000", ["13:58:14"])]) if not ln.startswith("#")]
    assert "\tTHULELAND\t" in body[0]


def test_bootstrapped_labels_round_trip(tmp_path):
    """What --make-labels emits must parse back, or the corrected file will not load."""
    stored = [_exchange("THULELAND", "266248000", ["13:58:14", "13:58:25"]),
              _exchange(None, None, ["14:00:00"])]
    p = _write(tmp_path, "\n".join(bi.make_labels(stored)) + "\n")
    labels = bi.parse_labels(p, lookup=_LOOKUP)
    assert [l.mmsi for l in labels] == ["266248000", None]


# ---------------------------------------------------------------------------
# Repeat runs: measuring the instrument's own noise
#
# The resolver is not reproducible, and temperature=0 does not make it so -- Anthropic's
# docs say plainly that temperature 0 "never guaranteed identical outputs", and there is no
# seed parameter. Observed on the 2026-08-07 corpus: 143/143 transmissions repeat identically
# except one conversation that flips between naming NOORDSTROOM and naming nobody, which
# alone is worth ~2.8 precision points -- larger than most changes anyone would want to
# measure. Scoring one run per arm therefore cannot distinguish a real difference from that
# flip. These summarise N runs into a mean, an observed spread, and the specific turns that
# moved, so the noise floor is a printed number instead of a footnote.
# ---------------------------------------------------------------------------

_REPEAT_LABELS = (
    "2026-08-04 13:58:00\t2026-08-04 13:58:30\t266248000\tTHULELAND\n"
    "2026-08-04 14:00:00\t2026-08-04 14:00:30\t244700270\tGOOILAND\n"
)


def _repeat_labels(tmp_path):
    return bi.parse_labels(_write(tmp_path, _REPEAT_LABELS))


def _run_both_correct():
    return [_exchange("THULELAND", "266248000", ["13:58:14"]),
            _exchange("GOOILAND", "244700270", ["14:00:14"])]


def _run_second_wrong():
    return [_exchange("THULELAND", "266248000", ["13:58:14"]),
            _exchange("SHALOM", "244810551", ["14:00:14"])]


def _run_second_declined():
    return [_exchange("THULELAND", "266248000", ["13:58:14"]),
            _exchange(None, None, ["14:00:14"])]


def test_identical_runs_have_no_spread_and_nothing_unstable(tmp_path):
    """The baseline claim: when the resolver does repeat itself, the summary must say so
    rather than manufacturing an error bar out of nothing."""
    labels = _repeat_labels(tmp_path)
    runs = [bi.score(labels, _run_both_correct()) for _ in range(3)]

    summary = bi.summarise_repeats(runs)
    assert summary["runs"] == 3
    assert summary["metrics"]["precision"]["spread"] == 0
    assert summary["unstable"] == []
    assert summary["turns_stable"] == summary["turns_seen"] == 2


def test_a_flipping_turn_is_named(tmp_path):
    """THE point of the flag. 'Something moved' is not actionable; 'this transmission moved,
    and here is what it was called each time' is -- that is what turned a story about
    adjudicator behaviour into 'five turns of one conversation'."""
    labels = _repeat_labels(tmp_path)
    runs = [bi.score(labels, _run_both_correct()),
            bi.score(labels, _run_second_wrong()),
            bi.score(labels, _run_both_correct())]

    summary = bi.summarise_repeats(runs)
    assert len(summary["unstable"]) == 1
    row = summary["unstable"][0]
    assert row["time"] == "2026-08-04 14:00:14"
    assert row["outcomes"] == {"correct": 2, "wrong": 1}
    assert summary["turns_stable"] == 1


def test_the_spread_is_the_observed_range(tmp_path):
    """An arm's number is only meaningful next to this. Two runs at 100% and 50% precision
    must report a 50-point spread, not an average that looks like a measurement."""
    labels = _repeat_labels(tmp_path)
    runs = [bi.score(labels, _run_both_correct()), bi.score(labels, _run_second_wrong())]

    precision = bi.summarise_repeats(runs)["metrics"]["precision"]
    assert precision["min"] == pytest.approx(0.5)
    assert precision["max"] == pytest.approx(1.0)
    assert precision["mean"] == pytest.approx(0.75)
    assert precision["spread"] == pytest.approx(0.5)


def test_a_turn_that_changes_between_named_and_declined_is_unstable(tmp_path):
    """The real observed failure: NOORDSTROOM vs nobody. The prediction changes from an MMSI
    to None, which a comparison keyed only on 'was it correct' could miss when both readings
    happen to score the same way."""
    labels = _repeat_labels(tmp_path)
    runs = [bi.score(labels, _run_both_correct()), bi.score(labels, _run_second_declined())]

    row, = bi.summarise_repeats(runs)["unstable"]
    assert set(row["predictions"]) == {"244700270", None}


def test_a_turn_missing_from_one_run_is_unstable_not_ignored(tmp_path):
    """Segmentation can move a turn out of a label window entirely, so it is scored in one
    run and absent from the next. Silently dropping it would understate the noise on exactly
    the runs that are least trustworthy."""
    labels = _repeat_labels(tmp_path)
    only_first = [_exchange("THULELAND", "266248000", ["13:58:14"])]
    runs = [bi.score(labels, _run_both_correct()), bi.score(labels, only_first)]

    summary = bi.summarise_repeats(runs)
    row, = summary["unstable"]
    assert row["time"] == "2026-08-04 14:00:14"
    assert row["runs_present"] == 1
    assert summary["turns_seen"] == 2


def test_a_single_run_reports_no_noise_rather_than_refusing(tmp_path):
    """--repeats 1 is the existing behaviour and must stay usable; one observation simply
    cannot show a spread."""
    labels = _repeat_labels(tmp_path)
    summary = bi.summarise_repeats([bi.score(labels, _run_both_correct())])
    assert summary["runs"] == 1
    assert summary["metrics"]["precision"]["spread"] == 0
    assert summary["unstable"] == []


def test_summarising_nothing_is_an_error(tmp_path):
    with pytest.raises(ValueError):
        bi.summarise_repeats([])


def test_repeats_without_resolve_is_refused(capsys):
    """--repeats only means something when the resolver actually re-runs; against stored
    verdicts every repeat reads the same JSON and would print a fake zero spread, which is
    exactly the false confidence this flag exists to remove."""
    assert bi.main(["--labels", "nonexistent.txt", "--repeats", "3"]) == 2
    assert "resolve" in capsys.readouterr().err


def test_repeats_must_be_at_least_one(capsys):
    with pytest.raises(SystemExit):
        bi.main(["--labels", "nonexistent.txt", "--resolve", "--repeats", "0"])
