"""Tests for the reference-free prompt-biasing metric.

Counts how often an arm writes a given word. This is the primary metric for the prompt
measurement precisely because it needs no reference: 51 of the corpus's 136 `heard` lines are
byte-identical to the machine text, so a reference-based metric would partly measure the
decoder agreeing with itself.
"""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import bench_word_production as bwp  # noqa: E402


def test_counts_one_row_per_occurrence_of_each_word():
    counts = bwp.count_words(["QNH one zero one three", "KLM one two bravo"], ("QNH", "KLM"))
    assert counts == {"QNH": 1, "KLM": 1}


def test_a_word_is_counted_once_per_row_not_once_per_repetition():
    """The corpus figures are per-transmission ("27 rows wrote QNH"), so the arms must be
    counted the same way or they are not comparable to the published baseline."""
    assert bwp.count_words(["QNH QNH QNH"], ("QNH",)) == {"QNH": 1}


def test_matching_is_case_insensitive():
    assert bwp.count_words(["klm one two bravo"], ("KLM",)) == {"KLM": 1}


def test_a_word_inside_a_longer_token_does_not_count():
    """Without word boundaries "QNH" would match inside a hyphenated or run-together token
    and quietly inflate the very number the measurement turns on."""
    assert bwp.count_words(["QNHX one zero", "unKLMlike"], ("QNH", "KLM")) == {"QNH": 0, "KLM": 0}


def test_an_empty_corpus_counts_zero_rather_than_failing():
    assert bwp.count_words([], ("QNH",)) == {"QNH": 0}


class TestTheOperatorRow:
    """EAR_COUNTS is one hour of one day's listening, not a standing baseline. Printed as a
    bare "(operator heard)" row it invites exactly the wrong reading when the tool is run on a
    later corpus: the numbers underneath are still 2026-09-10's, over a different set of
    clips, and nothing on the line says so."""

    def test_the_comparison_row_names_the_corpus_it_came_from(self, tmp_path, monkeypatch,
                                                              capsys):
        import json
        path = tmp_path / "arm.json"
        path.write_text(json.dumps({"model_label": None, "results": {"air_shipped": [
            {"clip_id": "0000", "text": "QNH one zero one three", "error": None}]}}),
            encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["bench_word_production.py", str(path)])
        bwp.main()
        row = [ln for ln in capsys.readouterr().out.splitlines() if "heard" in ln]
        assert len(row) == 1
        assert bwp.EAR_CORPUS in row[0]

    def test_the_corpus_size_is_a_named_constant_not_a_literal(self):
        """The 136 printed beside those counts is that corpus's clip count; a later corpus has
        a different one, and the two must not be able to drift apart silently."""
        assert bwp.EAR_CLIPS == 136
