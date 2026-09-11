"""Tests for the airband identification scorer.

See docs/superpowers/specs/2026-09-10-airband-identification-measurement-design.md. The
success criterion this file exists to protect is the third one: if the harness and the live
record disagree on a row that has a valid snapshot, nothing built on the harness can be
trusted, so the join and the bucket rules are pinned here rather than eyeballed once.
"""
import json

import pytest

import bench_flight_identify as bench


def _write_snapshots(path, rows):
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    return path


def _snap(stamp, *flights):
    return {"t": stamp, "n": len(flights),
            "aircraft": [{"hex": f"a{i}", "flight": f, "t": "B738"}
                         for i, f in enumerate(flights)]}


class TestSnapshotJoin:
    def test_exact_timestamp_hit(self, tmp_path):
        snaps = bench.load_snapshots(_write_snapshots(tmp_path / "s.jsonl", [
            _snap("2026-09-10T11:00:00+02:00", "KLM1"),
            _snap("2026-09-10T11:00:15+02:00", "KLM2"),
        ]))
        got = bench.join_snapshot(snaps, "2026-09-10T11:00:15+02:00")
        assert [a["flight"] for a in got["aircraft"]] == ["KLM2"]

    def test_uses_the_nearest_preceding_snapshot(self, tmp_path):
        snaps = bench.load_snapshots(_write_snapshots(tmp_path / "s.jsonl", [
            _snap("2026-09-10T11:00:00+02:00", "KLM1"),
            _snap("2026-09-10T11:00:15+02:00", "KLM2"),
        ]))
        got = bench.join_snapshot(snaps, "2026-09-10T11:00:19+02:00")
        assert [a["flight"] for a in got["aircraft"]] == ["KLM2"]

    def test_never_joins_a_snapshot_taken_after_the_transmission(self, tmp_path):
        """A later poll can hold aircraft that had not arrived yet -- that is not evidence."""
        snaps = bench.load_snapshots(_write_snapshots(tmp_path / "s.jsonl", [
            _snap("2026-09-10T11:00:15+02:00", "KLM2"),
        ]))
        assert bench.join_snapshot(snaps, "2026-09-10T11:00:14+02:00") is None

    def test_outside_the_window_is_no_snapshot_not_an_empty_one(self, tmp_path):
        snaps = bench.load_snapshots(_write_snapshots(tmp_path / "s.jsonl", [
            _snap("2026-09-10T11:00:00+02:00", "KLM1"),
        ]))
        assert bench.join_snapshot(snaps, "2026-09-10T11:00:21+02:00") is None

    def test_empty_snapshot_file(self, tmp_path):
        path = tmp_path / "s.jsonl"
        path.write_text("", encoding="utf-8")
        assert bench.load_snapshots(path) == []
        assert bench.join_snapshot([], "2026-09-10T11:00:00+02:00") is None


class TestReadLabel:
    def test_a_callsign_is_the_label(self):
        assert bench.read_label("DAL162") == "DAL162"

    def test_case_and_padding_are_forgiven(self):
        assert bench.read_label("  klm1406 ") == "KLM1406"

    def test_none_means_no_aircraft_is_named_here(self):
        assert bench.read_label("NONE") == bench.LABEL_NONE

    def test_unsure_is_excluded_not_scored(self):
        assert bench.read_label("UNSURE") == bench.LABEL_UNSURE

    def test_a_trailing_question_mark_is_uncertainty_not_a_callsign(self):
        """The operator wrote `YZR7939?` on rows 0132/0133 and called it UNSURE."""
        assert bench.read_label("YZR7939?") == bench.LABEL_UNSURE

    def test_blank_is_none_by_default(self):
        """The operator's call 2026-09-10: blanks mean nothing was named, everywhere."""
        assert bench.read_label("") == bench.LABEL_NONE

    def test_blank_can_be_excluded_instead(self):
        """Kept switchable so the 7 tagged-but-blank rows can be scored both ways."""
        assert bench.read_label("", blank_means=bench.LABEL_UNSURE) == bench.LABEL_UNSURE


class TestSystemLine:
    def test_an_identified_row_reports_the_tagged_callsign(self):
        assert bench.parse_system("identified DAL73/A333") == ("DAL73", None)

    def test_a_tag_without_an_aircraft_type(self):
        assert bench.parse_system("identified KLM281") == ("KLM281", None)

    def test_an_extracted_but_unmatched_row_reports_the_candidate(self):
        assert bench.parse_system("not identified  (extracted DAL1361, no match)") == (None, "DAL1361")

    def test_a_row_with_no_candidate_at_all(self):
        assert bench.parse_system("not identified  (no callsign extracted)") == (None, None)


class TestBuckets:
    """One test per row of the outcome table in the design spec, from real transmissions."""

    def test_a_tag_matching_the_label_is_correct(self):
        assert bench.classify("DAL73", tagged="DAL73", candidate=None,
                              in_range={"DAL73"}) == bench.CORRECT

    def test_a_tag_naming_a_different_aircraft_is_a_wrong_match(self):
        """The BERGE TOWNSEND class: confidently identified, confidently wrong."""
        assert bench.classify("KLM281", tagged="KLM285", candidate=None,
                              in_range={"KLM285"}) == bench.WRONG_MATCH

    def test_an_aircraft_that_was_never_in_range_is_a_retrieval_miss(self):
        """TRA6084 -- four other Transavia aircraft nearby, none numbered 6084."""
        assert bench.classify("TRA6084", tagged=None, candidate="TRA6084",
                              in_range={"TRA1234"}) == bench.RETRIEVAL_MISS

    def test_in_range_but_no_candidate_produced_is_an_extraction_miss(self):
        """TFL367 -- "orange" is not in AIRLINE_TELEPHONY, so nothing is extracted at all."""
        assert bench.classify("TFL367", tagged=None, candidate=None,
                              in_range={"TFL367"}) == bench.EXTRACTION_MISS

    def test_in_range_with_a_candidate_that_did_not_match_is_a_selection_miss(self):
        """DAL192 heard 75s after DAL162 was exact-matched twice, same aircraft."""
        assert bench.classify("DAL162", tagged=None, candidate="DAL192",
                              in_range={"DAL162"}) == bench.SELECTION_MISS

    def test_a_none_row_left_untagged_is_a_correct_rejection(self):
        assert bench.classify(bench.LABEL_NONE, tagged=None, candidate=None,
                              in_range={"DAL73"}) == bench.CORRECT_REJECTION

    def test_a_none_row_that_got_tagged_is_a_wrong_match(self):
        """Tagging an aircraft where none was named costs precision like any other bad tag."""
        assert bench.classify(bench.LABEL_NONE, tagged="DAL57", candidate=None,
                              in_range={"DAL57"}) == bench.WRONG_MATCH

    def test_an_unsure_row_is_excluded(self):
        assert bench.classify(bench.LABEL_UNSURE, tagged=None, candidate=None,
                              in_range={"YZR7939"}) == bench.EXCLUDED

    def test_a_miss_with_no_snapshot_is_excluded_not_called_a_retrieval_miss(self):
        """Never let "the corpus cannot answer" masquerade as "nothing was in range"."""
        assert bench.classify("DAL73", tagged=None, candidate="DAL73",
                              in_range=None) == bench.EXCLUDED

    def test_a_correct_tag_still_scores_without_a_snapshot(self):
        """The tag and the label alone settle this row; no snapshot is needed to judge it."""
        assert bench.classify("DAL73", tagged="DAL73", candidate=None,
                              in_range=None) == bench.CORRECT

    def test_the_runway_designator_row_must_never_score_as_an_identification(self):
        """"is one eight Charlie available" -- 18C is a runway, not an aircraft."""
        heard = "Thank you, four zero, is one eight Charlie available, we're very late."
        assert bench.candidate_for(heard) is None


WORKSHEET = """\
--- 0000 ----------------------------------------------
audio    : 0000_sent.wav   (raw: 0000_raw.wav)
time     : 2026-09-10T11:00:05+02:00   channel: 121,205   2.5s
machine  : Delta seven three, New York, descend flight level seven zero.
system   : identified DAL73/A333

heard    : Delta seven three, New York, descend flight level seven zero.
aircraft : DAL73

--- 0001 ----------------------------------------------
audio    : 0001_sent.wav   (raw: 0001_raw.wav)
time     : 2026-09-10T11:00:06+02:00   channel: 121,205   2.5s
machine  : Continue present heading, QNH one zero one eight.
system   : not identified  (no callsign extracted)

heard    : Continue present heading, QNH one zero one eight.
aircraft : NONE

--- 0002 ----------------------------------------------
audio    : 0002_sent.wav   (raw: 0002_raw.wav)
time     : 2026-09-10T11:00:07+02:00   channel: 121,205   2.5s
machine  : Port Cremoros three six seven heavy, passing two thousand six hundred.
system   : not identified  (no callsign extracted)

heard    : Orange three six seven heavy, passing two thousand six hundred.
aircraft : TFL367

--- 0003 ----------------------------------------------
audio    : 0003_sent.wav   (raw: 0003_raw.wav)
time     : 2026-09-10T11:00:08+02:00   channel: 121,205   2.5s
machine  : Two thousand feet, Delta one nine two.
system   : not identified  (extracted DAL192, no match)

heard    : Two thousand feet, Delta one nine two.
aircraft : DAL162

--- 0004 ----------------------------------------------
audio    : 0004_sent.wav   (raw: 0004_raw.wav)
time     : 2026-09-10T11:00:09+02:00   channel: 121,205   2.5s
machine  : Transavia six zero eight four, good day.
system   : not identified  (extracted TRA6084, no match)

heard    : Transavia six zero eight four, good day.
aircraft : TRA6084
"""


@pytest.fixture
def corpus(tmp_path):
    labels = tmp_path / "labels.txt"
    labels.write_text(WORKSHEET, encoding="utf-8")
    snaps = _write_snapshots(tmp_path / "s.jsonl", [
        _snap("2026-09-10T11:00:00+02:00", "DAL73", "TFL367", "DAL162", "TRA1234"),
    ])
    return labels, bench.load_snapshots(snaps)


class TestScore:
    def test_every_row_lands_in_exactly_one_bucket(self, corpus):
        labels, snaps = corpus
        result = bench.score(labels.read_text(encoding="utf-8"), snaps)
        assert result.counts == {
            bench.CORRECT: 1,             # 0000 DAL73
            bench.CORRECT_REJECTION: 1,   # 0001 NONE, untagged
            bench.EXTRACTION_MISS: 1,     # 0002 TFL367 in range, "orange" not in the table
            bench.SELECTION_MISS: 1,      # 0003 DAL192 extracted, DAL162 in range
            bench.RETRIEVAL_MISS: 1,      # 0004 TRA6084 never in range
        }
        assert sum(result.counts.values()) == len(result.rows)

    def test_recall_is_against_the_achievable_ceiling_not_every_row(self, corpus):
        """Three rows named an in-range aircraft; the retrieval miss is the ceiling, not a miss
        anyone can fix, so it stays out of the denominator and is reported separately."""
        labels, snaps = corpus
        result = bench.score(labels.read_text(encoding="utf-8"), snaps)
        assert result.achievable == 3
        assert result.recall == pytest.approx(1 / 3)
        assert result.ceiling_missed == 1

    def test_precision_counts_every_tag_applied(self, corpus):
        labels, snaps = corpus
        result = bench.score(labels.read_text(encoding="utf-8"), snaps)
        assert result.precision == pytest.approx(1.0)

    def test_a_tag_on_a_none_row_costs_precision_but_not_recall(self, tmp_path):
        text = WORKSHEET.replace(
            "system   : not identified  (no callsign extracted)\n\n"
            "heard    : Continue present heading, QNH one zero one eight.",
            "system   : identified DAL57/A339\n\n"
            "heard    : Continue present heading, QNH one zero one eight.")
        snaps = bench.load_snapshots(_write_snapshots(tmp_path / "s.jsonl", [
            _snap("2026-09-10T11:00:00+02:00", "DAL73", "TFL367", "DAL162", "DAL57")]))
        result = bench.score(text, snaps)
        assert result.counts[bench.WRONG_MATCH] == 1
        assert result.precision == pytest.approx(0.5)
        assert result.achievable == 3

    def test_replay_reproduces_the_live_record_on_unchanged_code(self, corpus):
        """The spec's third success criterion. If these two disagree on rows that have a
        snapshot, the harness is wrong and nothing measured with it can be trusted."""
        labels, snaps = corpus
        live = bench.score(labels.read_text(encoding="utf-8"), snaps)
        replayed = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True)
        assert replayed.counts == live.counts

    def test_replay_can_score_the_corrected_text_to_size_the_asr_headroom(self, corpus):
        """Arm A's counterfactual: what the matcher would do if ASR had heard it right. Row
        0002 is the case -- "Port Cremoros three six seven" extracts nothing, while the
        corrected "Orange three six seven" reaches TFL367, which was in range."""
        labels, snaps = corpus
        over_machine = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True)
        over_heard = bench.score(labels.read_text(encoding="utf-8"), snaps,
                                 replay=True, text_source="heard")
        assert over_machine.counts[bench.EXTRACTION_MISS] == 1
        assert bench.EXTRACTION_MISS not in over_heard.counts
        assert over_heard.counts[bench.CORRECT] == over_machine.counts[bench.CORRECT] + 1


class TestCorpusIntegrity:
    """The `system` line was generated from the capture text, so it is a checksum over the
    `machine` line. On the 2026-09-10 corpus three machine lines had been hand-edited and one
    of those silently turned a replay miss into a replay hit -- exactly the disagreement
    between replay and the live record that is supposed to condemn the harness."""

    def test_an_untouched_worksheet_reports_nothing(self):
        assert bench.integrity_warnings(WORKSHEET) == []

    def test_an_edited_machine_line_is_reported_with_its_row(self):
        tampered = WORKSHEET.replace(
            "machine  : Continue present heading, QNH one zero one eight.",
            "machine  : Continue present heading, KLM two two six.")
        warnings = bench.integrity_warnings(tampered)
        assert len(warnings) == 1
        assert "0001" in warnings[0]

    def test_a_tagged_row_is_not_flagged_just_for_carrying_a_tag(self):
        """`identified DAL73/A333` cannot be recomputed from the text -- it is the live
        record -- so those rows must be left alone rather than reported every run."""
        assert all("0000" not in w for w in bench.integrity_warnings(WORKSHEET))


class TestRowsReportTheTextTheyWereScoredOn:
    """--rows printed the `heard` line whatever --text said, so a replay over the machine text
    listed misses beside a transcription that was never fed to the extractor."""

    def test_the_live_record_is_scored_on_the_machine_text(self, corpus):
        labels, snaps = corpus
        row = bench.score(labels.read_text(encoding="utf-8"), snaps).rows[2]
        assert row.text.startswith("Port Cremoros")

    def test_a_heard_replay_reports_the_heard_text(self, corpus):
        labels, snaps = corpus
        row = bench.score(labels.read_text(encoding="utf-8"), snaps,
                          replay=True, text_source="heard").rows[2]
        assert row.text.startswith("Orange")


class TestDigitRuns:
    """Tail-first matching arm. The airline word is what ASR destroys; the digits usually
    survive, so this asks whether the digits alone can find the aircraft. Decoding reuses
    flight_identify's own digit/phonetic decoder so the runs are identical to what production
    extraction would have built."""

    def test_a_trailing_spelled_run_with_a_phonetic_suffix(self):
        assert bench.digit_runs("One eight center with Fox, Roscoe, one two bravo.") == ["18", "12B"]

    def test_each_maximal_run_is_separate(self):
        assert bench.digit_runs(
            "Descend flight level seven zero, QNH one two bravo.") == ["70", "12B"]

    def test_a_run_stops_before_an_altitude_reading(self):
        """Same boundary rule production extraction uses -- "three two two thousand" is a
        callsign followed by an altitude, not a five-digit tail."""
        assert bench.digit_runs(
            "Ship hold, jet blue three two two thousand, flight level zero six zero."
        ) == ["32", "060"]

    def test_a_numeral_token_is_its_own_run(self):
        assert bench.digit_runs("Contact 123705, good day.") == ["123705"]

    def test_text_with_no_digits_has_no_runs(self):
        assert bench.digit_runs("Approach, good morning.") == []


class TestMatchByTail:
    def _fleet(self, *pairs):
        return [{"hex": f"h{i}", "flight": f, "t": "B738", "alt_baro": alt}
                for i, (f, alt) in enumerate(pairs)]

    def test_a_tail_unique_among_the_aircraft_in_range_resolves(self):
        fleet = self._fleet(("KLM12B", 4000), ("DAL73", 9000))
        assert bench.match_by_tail(["12B"], fleet, bench.TailFirst())["flight"] == "KLM12B"

    def test_a_tail_two_aircraft_share_resolves_to_nothing(self):
        """The BERGE TOWNSEND rule: an ambiguous match is worse than no match."""
        fleet = self._fleet(("KLM32", 4000), ("JBU32", 9000))
        assert bench.match_by_tail(["32"], fleet, bench.TailFirst()) is None

    def test_a_tail_shorter_than_the_minimum_is_refused(self):
        fleet = self._fleet(("KLM8", 4000))
        assert bench.match_by_tail(["8"], fleet, bench.TailFirst(min_tail=2)) is None
        assert bench.match_by_tail(["8"], fleet, bench.TailFirst(min_tail=1))["flight"] == "KLM8"

    def test_parked_aircraft_are_out_of_the_candidate_set_by_default(self):
        """Schiphol is 36 km away and its apron is full of aircraft that are not talking."""
        fleet = self._fleet(("KLM12B", "ground"))
        assert bench.match_by_tail(["12B"], fleet, bench.TailFirst()) is None
        assert bench.match_by_tail(["12B"], fleet,
                                   bench.TailFirst(include_ground=True))["flight"] == "KLM12B"

    def test_two_runs_resolving_to_two_different_aircraft_resolve_to_nothing(self):
        fleet = self._fleet(("KLM70", 4000), ("KLM12B", 9000))
        assert bench.match_by_tail(["70", "12B"], fleet, bench.TailFirst()) is None

    def test_only_the_trailing_run_is_tried_when_asked(self):
        fleet = self._fleet(("KLM70", 4000), ("KLM12B", 9000))
        opts = bench.TailFirst(runs="trailing")
        assert bench.match_by_tail(["70", "12B"], fleet, opts)["flight"] == "KLM12B"


TAIL_WORKSHEET = """\
--- 0000 ----------------------------------------------
audio    : 0000_sent.wav   (raw: 0000_raw.wav)
time     : 2026-09-10T11:00:05+02:00   channel: 121,205   2.5s
machine  : Descend flight level seven zero, QNH one two bravo.
system   : not identified  (no callsign extracted)

heard    : Descend flight level seven zero, KLM one two bravo.
aircraft : KLM12B

--- 0001 ----------------------------------------------
audio    : 0001_sent.wav   (raw: 0001_raw.wav)
time     : 2026-09-10T11:00:06+02:00   channel: 121,205   2.5s
machine  : Continue present heading, heading is three seven zero.
system   : not identified  (no callsign extracted)

heard    : Continue present heading, heading is three seven zero.
aircraft : NONE
"""


class TestTailFirstArmInScoring:
    @pytest.fixture
    def tail_corpus(self, tmp_path):
        labels = tmp_path / "t.txt"
        labels.write_text(TAIL_WORKSHEET, encoding="utf-8")
        snaps = _write_snapshots(tmp_path / "s.jsonl", [
            _snap("2026-09-10T11:00:00+02:00", "KLM12B", "DAL370")])
        return labels, bench.load_snapshots(snaps)

    def test_the_arm_is_off_unless_asked_for(self, tail_corpus):
        labels, snaps = tail_corpus
        result = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True)
        assert result.counts[bench.EXTRACTION_MISS] == 1
        assert bench.CORRECT not in result.counts

    def test_the_arm_recovers_a_miss_the_airline_word_destroyed(self, tail_corpus):
        labels, snaps = tail_corpus
        result = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True,
                             arm=bench.TailFirst())
        assert result.counts[bench.CORRECT] == 1

    def test_the_arm_is_charged_for_the_readbacks_it_invents(self, tail_corpus):
        """"heading is three seven zero" is a heading, and DAL370 is in range. This is the
        whole risk of the arm and it must land in the wrong-match bucket, not vanish."""
        labels, snaps = tail_corpus
        result = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True,
                             arm=bench.TailFirst())
        assert result.counts[bench.WRONG_MATCH] == 1
        assert result.precision == pytest.approx(0.5)


class TestScoringAnArmsTranscripts:
    """An arm is scored by replacing the worksheet's machine text with that arm's own
    transcription of the same clip, joined on clip id."""

    def _results_file(self, tmp_path, rows, config="air_shipped"):
        path = tmp_path / "arm.json"
        path.write_text(json.dumps({
            "model_label": "groq-whisper-large-v3",
            "results": {config: [{"clip_id": cid, "text": text, "reference": "", "wer": None}
                                 for cid, text in rows]},
        }), encoding="utf-8")
        return path

    def test_transcripts_load_keyed_by_clip_id(self, tmp_path):
        path = self._results_file(tmp_path, [("0000", "hello"), ("0001", "world")])
        assert bench.load_transcripts(path) == {"0000": "hello", "0001": "world"}

    def test_a_results_file_with_several_configs_needs_the_config_named(self, tmp_path):
        path = tmp_path / "two.json"
        path.write_text(json.dumps({"model_label": None, "results": {
            "air_shipped": [{"clip_id": "0000", "text": "shipped"}],
            "air_both": [{"clip_id": "0000", "text": "both"}],
        }}), encoding="utf-8")
        assert bench.load_transcripts(path, config="air_both") == {"0000": "both"}

    def test_an_ambiguous_results_file_with_no_config_named_refuses_to_guess(self, tmp_path):
        """A silent next(iter(results)) here would attribute one arm's transcriptions to
        another arm -- exactly the cross-contamination this design exists to prevent."""
        path = tmp_path / "two.json"
        path.write_text(json.dumps({"model_label": None, "results": {
            "air_shipped": [{"clip_id": "0000", "text": "shipped"}],
            "air_both": [{"clip_id": "0000", "text": "both"}],
        }}), encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            bench.load_transcripts(path)
        assert "air_shipped" in str(excinfo.value)
        assert "air_both" in str(excinfo.value)

    def test_the_arms_text_replaces_the_worksheet_text(self, corpus, tmp_path):
        """Row 0002 is "Port Cremoros three six seven" in the worksheet and extracts nothing.
        An arm that transcribed it as "Orange three six seven" must score as correct."""
        labels, snaps = corpus
        path = self._results_file(tmp_path, [
            ("0002", "Orange three six seven heavy, passing two thousand six hundred.")])
        result = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True,
                             transcripts=bench.load_transcripts(path))
        assert result.rows[2].bucket == bench.CORRECT

    def test_a_clip_missing_from_the_arm_is_excluded_and_named(self, corpus, tmp_path):
        """A dropped clip (a 429, a failed request) must never be scored on stale worksheet
        text -- that would credit one arm with another arm's transcription."""
        labels, snaps = corpus
        path = self._results_file(tmp_path, [("0000", "Delta seven three, New York.")])
        result = bench.score(labels.read_text(encoding="utf-8"), snaps, replay=True,
                             transcripts=bench.load_transcripts(path))
        assert result.missing_transcripts == [1, 2, 3, 4]
        assert all(r.bucket == bench.EXCLUDED for r in result.rows[1:])

    def test_transcripts_require_replay(self, corpus):
        labels, snaps = corpus
        with pytest.raises(ValueError):
            bench.score(labels.read_text(encoding="utf-8"), snaps, transcripts={"0000": "x"})
