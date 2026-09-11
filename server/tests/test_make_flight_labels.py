"""Tests for make_flight_labels.py: the airband identification labelling worksheet.

The worksheet costs an hour of a human's listening to fill in, so the format has to be
readable by hand AND parseable by the scorer. The round-trip tests below are the ones that
protect that investment -- a format that renders nicely but cannot be read back turns the
labelling effort into wasted work.
"""

import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import make_flight_labels as mfl  # noqa: E402


def _row(**over):
    row = {"index": 42, "timestamp": "2026-09-10T11:23:14.8810000+02:00",
           "channel": "121,205", "durationSec": 3.84,
           "text": "Two thousand feet, ten nineteen, Delta one nine two."}
    row.update(over)
    return row


# --- channel filtering ------------------------------------------------------------------

def test_airband_channel_accepts_both_locale_decimal_forms():
    """SDR# renders the frequency in the current OS culture, so the same channel arrives as
    either form depending on the deployment -- the same reason APPROACH_TOWER_CHANNELS lists
    both spellings of every frequency."""
    assert mfl.is_airband("121,205")
    assert mfl.is_airband("121.205")


def test_airband_channel_rejects_the_maritime_channel():
    """CH01 is 160.650 and carries vessels, not aircraft. A mixed-band capture day must not
    put maritime transmissions in an aircraft worksheet."""
    assert not mfl.is_airband("160,650")


def test_airband_channel_rejects_unparseable_channel():
    assert not mfl.is_airband("")
    assert not mfl.is_airband("not-a-frequency")


# --- tag splitting ----------------------------------------------------------------------

def test_strip_tag_splits_a_live_identification():
    """index.jsonl stores the text the plugin displayed, tag included -- verified against a
    real capture on 2026-09-10."""
    text, tag = mfl.strip_tag("[DAL73/A333] New York Delta seven three two thousand.")
    assert text == "New York Delta seven three two thousand."
    assert tag == "DAL73/A333"


def test_strip_tag_handles_a_tag_without_an_aircraft_type():
    text, tag = mfl.strip_tag("[KLM281] Cleared ILS.")
    assert text == "Cleared ILS."
    assert tag == "KLM281"


def test_strip_tag_leaves_untagged_text_alone():
    text, tag = mfl.strip_tag("Two thousand feet, ten nineteen.")
    assert text == "Two thousand feet, ten nineteen."
    assert tag is None


def test_strip_tag_ignores_a_leading_bracket_that_is_not_a_tag():
    """Only the [CODE] / [CODE/TYPE] shape is a tag. Anything else is transcript text and
    must survive verbatim -- stripping it would silently corrupt a label."""
    text, tag = mfl.strip_tag("[unintelligible] descend two thousand.")
    assert text == "[unintelligible] descend two thousand."
    assert tag is None


# --- rendering --------------------------------------------------------------------------

def test_block_prefills_heard_with_the_machine_text():
    """The labeller edits rather than transcribes. Pre-filling biases them toward accepting
    the machine's version, which the worksheet header discloses."""
    block = mfl.render_block(_row())
    assert "heard    : Two thousand feet, ten nineteen, Delta one nine two." in block


def test_block_leaves_aircraft_empty_for_the_labeller():
    block = mfl.render_block(_row())
    assert "aircraft :\n" in block or block.rstrip().endswith("aircraft :")


def test_block_reports_a_live_identification_and_strips_it_from_the_text():
    """The tag is what the system decided; it must not leak into `machine` or `heard`, or the
    labeller would be correcting the system's own output back into the ground truth."""
    block = mfl.render_block(_row(text="[DAL73/A333] Delta seven three, climbing."))
    assert "identified DAL73/A333" in block
    assert "machine  : Delta seven three, climbing." in block
    assert "heard    : Delta seven three, climbing." in block


def test_block_reports_what_was_extracted_when_nothing_was_identified():
    """`Delta one nine two` extracts as DAL192 and matched nothing live. Showing the extracted
    candidate tells the labeller what the system thought it heard."""
    block = mfl.render_block(_row())
    assert "not identified" in block
    assert "DAL192" in block


def test_block_says_so_when_no_callsign_could_be_extracted():
    block = mfl.render_block(_row(text="Roger, good day, over."))
    assert "no callsign extracted" in block


def test_block_names_the_audio_clips_by_zero_padded_index():
    block = mfl.render_block(_row(index=7))
    assert "0007_sent.wav" in block
    assert "0007_raw.wav" in block


def test_block_preserves_the_full_text_however_long():
    """identification-labels-2026-08-07-verified.txt stored ~55-character excerpts, and when a
    later question needed the full text, zero of its 32 conversations could answer it. Full
    text is free; truncation is irreversible."""
    long_text = ("Amsterdam approach good morning this is a very long transmission " * 4).strip()
    block = mfl.render_block(_row(text=long_text))
    assert long_text in block


# --- round trip -------------------------------------------------------------------------

def test_worksheet_round_trips_through_the_parser():
    """The format has to survive being written, hand-edited and read back. If this breaks, an
    hour of listening is unreadable."""
    rows = [_row(index=1), _row(index=2, text="[KLM281] Cleared ILS.")]
    parsed = mfl.parse_worksheet(mfl.render_worksheet(rows))
    assert [p["index"] for p in parsed] == [1, 2]
    assert parsed[1]["heard"] == "Cleared ILS."


def test_parser_returns_the_edited_heard_line_not_the_prefill():
    text = mfl.render_worksheet([_row()]).replace(
        "heard    : Two thousand feet, ten nineteen, Delta one nine two.",
        "heard    : Two thousand feet, one zero one nine, Delta one six two.")
    assert mfl.parse_worksheet(text)[0]["heard"] == (
        "Two thousand feet, one zero one nine, Delta one six two.")


def test_parser_reads_the_aircraft_the_labeller_named():
    text = mfl.render_worksheet([_row()]).replace("aircraft :", "aircraft : DAL162")
    assert mfl.parse_worksheet(text)[0]["aircraft"] == "DAL162"


def test_parser_reports_an_unlabelled_row_as_empty_not_as_none_marker():
    """An untouched row and a row deliberately marked NONE mean opposite things -- one is
    "not labelled yet", the other is "no aircraft was named here". Conflating them would
    silently score unlabelled rows as ground truth."""
    assert mfl.parse_worksheet(mfl.render_worksheet([_row()]))[0]["aircraft"] == ""


def test_parser_reads_the_none_and_unsure_markers():
    for marker in ("NONE", "UNSURE"):
        text = mfl.render_worksheet([_row()]).replace("aircraft :", f"aircraft : {marker}")
        assert mfl.parse_worksheet(text)[0]["aircraft"] == marker


# --- generation from a capture index ----------------------------------------------------

def test_worksheet_keeps_only_airband_rows():
    rows = [_row(index=1), _row(index=2, channel="160,650"), _row(index=3)]
    parsed = mfl.parse_worksheet(mfl.render_worksheet(mfl.airband_rows(rows)))
    assert [p["index"] for p in parsed] == [1, 3]


def test_worksheet_header_discloses_the_prefill_bias():
    """The labeller is being nudged toward the machine's wording; saying so is the difference
    between a disclosed trade-off and a hidden one."""
    header = mfl.render_worksheet([_row()])
    assert "bias" in header.lower()


class TestReferenceExport:
    """The worksheet's hand-corrected `heard` lines are the only ear-verified airband
    reference set there is. bench.load_references wants clip_id<TAB>text."""

    def _sheet(self, heard: str) -> str:
        rows = [{"index": 18, "timestamp": "2026-09-10T11:00:00+02:00",
                 "channel": "121,205", "durationSec": 2.5, "text": "machine text"}]
        sheet = mfl.render_worksheet(rows)
        return sheet.replace("heard    : machine text", f"heard    : {heard}")

    def test_a_corrected_line_is_exported_against_its_clip_id(self):
        out = mfl.to_references(self._sheet("KLM one two bravo."))
        assert out.splitlines() == ["0018\tKLM one two bravo."]

    def test_a_question_mark_becomes_an_inaudible_marker(self):
        """bench._normalize already strips [bracketed] markers, so an unintelligible word
        costs nothing instead of counting as a wrong word against every arm equally."""
        out = mfl.to_references(self._sheet("Approach, ? good day."))
        assert out.splitlines() == ["0018\tApproach, [inaudible] good day."]

    def test_an_unlabelled_clip_exports_no_reference_line(self):
        assert mfl.to_references(self._sheet("")) == ""

    def test_a_tab_inside_the_text_cannot_break_the_format(self):
        out = mfl.to_references(self._sheet("one\ttwo"))
        assert out.splitlines() == ["0018\tone two"]
        assert out.count("\t") == 1


class TestCli:
    """`main` is where an hour of hand labelling can be destroyed by one flag.

    The original --references re-generated a fresh worksheet into --out and then derived the
    references from it, so pointing --out at the labelled file overwrote the labelling, and
    the references it produced were byte-identical to the decoder's own output -- a reference
    file that scores the decoder against itself. Both are pinned closed here.
    """

    def _captures(self, tmp_path):
        captures = tmp_path / "2026-09-10"
        captures.mkdir()
        (captures / "index.jsonl").write_text(json.dumps({
            "index": 18, "timestamp": "2026-09-10T11:00:00+02:00", "channel": "121,205",
            "durationSec": 2.5, "text": "machine text"}) + "\n", encoding="utf-8")
        return captures

    def _run(self, monkeypatch, *argv):
        monkeypatch.setattr(sys, "argv", ["make_flight_labels.py", *argv])
        mfl.main()

    def test_from_worksheet_writes_references_and_generates_nothing(self, tmp_path, monkeypatch):
        """The only correct way to get references: read the labelled worksheet as it stands."""
        sheet = tmp_path / "labels.txt"
        sheet.write_text(mfl.render_worksheet([_row(index=18)]).replace(
            "heard    : Two thousand feet, ten nineteen, Delta one nine two.",
            "heard    : Two thousand feet, one zero one nine, Delta one six two."),
            encoding="utf-8")
        before = sheet.read_text(encoding="utf-8")
        refs = tmp_path / "refs.txt"
        self._run(monkeypatch, "--from-worksheet", str(sheet), "--references", str(refs))
        assert refs.read_text(encoding="utf-8").splitlines() == [
            "0018\tTwo thousand feet, one zero one nine, Delta one six two."]
        assert sheet.read_text(encoding="utf-8") == before

    def test_from_worksheet_needs_no_captures_or_out(self, tmp_path, monkeypatch):
        sheet = tmp_path / "labels.txt"
        sheet.write_text(mfl.render_worksheet([_row(index=18)]), encoding="utf-8")
        self._run(monkeypatch, "--from-worksheet", str(sheet),
                  "--references", str(tmp_path / "refs.txt"))
        assert not (tmp_path / "out.txt").exists()

    def test_generating_over_an_existing_worksheet_is_refused(self, tmp_path, monkeypatch):
        """The hand labelling is an hour of the operator's listening and is not in git."""
        out = tmp_path / "labels.txt"
        out.write_text("PRECIOUS HAND LABELLING", encoding="utf-8")
        with pytest.raises(SystemExit) as excinfo:
            self._run(monkeypatch, "--captures", str(self._captures(tmp_path)), "--out", str(out))
        assert str(out) in str(excinfo.value)
        assert out.read_text(encoding="utf-8") == "PRECIOUS HAND LABELLING"

    def test_generating_into_a_fresh_path_still_works(self, tmp_path, monkeypatch):
        out = tmp_path / "new-labels.txt"
        self._run(monkeypatch, "--captures", str(self._captures(tmp_path)), "--out", str(out))
        assert mfl.parse_worksheet(out.read_text(encoding="utf-8"))[0]["index"] == 18

    def test_references_cannot_be_combined_with_generation(self, tmp_path, monkeypatch):
        """Generating and then exporting produces references identical to the machine text --
        a WER of zero against the decoder's own output, for every arm."""
        out = tmp_path / "new-labels.txt"
        with pytest.raises(SystemExit) as excinfo:
            self._run(monkeypatch, "--captures", str(self._captures(tmp_path)),
                      "--out", str(out), "--references", str(tmp_path / "refs.txt"))
        assert "--from-worksheet" in str(excinfo.value)
        assert not out.exists()
        assert not (tmp_path / "refs.txt").exists()

    def test_from_worksheet_without_references_is_refused(self, tmp_path, monkeypatch):
        sheet = tmp_path / "labels.txt"
        sheet.write_text(mfl.render_worksheet([_row(index=18)]), encoding="utf-8")
        with pytest.raises(SystemExit):
            self._run(monkeypatch, "--from-worksheet", str(sheet))

    def test_generating_without_captures_or_out_is_refused(self, tmp_path, monkeypatch):
        with pytest.raises(SystemExit):
            self._run(monkeypatch, "--out", str(tmp_path / "x.txt"))
        with pytest.raises(SystemExit):
            self._run(monkeypatch, "--captures", str(self._captures(tmp_path)))
