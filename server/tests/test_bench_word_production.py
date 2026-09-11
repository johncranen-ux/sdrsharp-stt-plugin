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
