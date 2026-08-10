"""Tests for bench_conversation_correct.py: scoring the pass on three numbers, not one."""

import datetime
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from bench_conversation_correct import score_turns, wer_counts  # noqa: E402


INDEX = {"0000": datetime.datetime(2026, 8, 7, 10, 14, 15),
         "0001": datetime.datetime(2026, 8, 7, 10, 14, 19)}
REFERENCES = {"0000": "Maas Approach, Motorvessel Example Trader.",
              "0001": "Motorvessel Example Trader, Maas Approach."}


def _rows(conv=None):
    turn = {"time": "10:14:15", "text": "Maas Approach, motor vision Example Trader."}
    if conv:
        turn = {**turn, "conv": conv, "changes": [{"from": "motor vision", "to": "Motorvessel",
                                                   "reason": "shore"}]}
    return [{"start": "2026-08-07 10:14:15", "turns": [
        turn,
        {"time": "10:14:19", "text": "Motorvessel Example Trader, Maas Approach."},
    ]}]


def test_wer_counts_substitutions_insertions_and_deletions():
    assert wer_counts(["a", "b", "c"], ["a", "b", "c"]) == (0, 3)
    assert wer_counts(["a", "b", "c"], ["a", "x", "c"]) == (1, 3)
    assert wer_counts(["a", "b", "c"], ["a", "c"]) == (1, 3)
    assert wer_counts(["a", "b"], ["a", "b", "c"]) == (1, 2)


def test_the_baseline_scores_the_live_text():
    got = score_turns(_rows(), REFERENCES, INDEX, use_conv=False)
    assert got["scored"] == 2
    assert got["errors"] == 2      # "motor vision" against "Motorvessel"
    assert got["unmatched"] == 0


def test_the_corrected_arm_scores_the_conv_text():
    got = score_turns(_rows(conv="Maas Approach, Motorvessel Example Trader."),
                      REFERENCES, INDEX, use_conv=True)
    assert got["errors"] == 0
    assert got["wer"] == 0.0


def test_a_turn_with_no_clip_is_reported_not_silently_dropped():
    """A turn scored against the wrong clip reads as a quality change that never happened."""
    rows = [{"start": "2026-08-07 10:14:15",
             "turns": [{"time": "23:59:59", "text": "orphan"}]}]
    got = score_turns(rows, REFERENCES, INDEX, use_conv=False)
    assert got["unmatched"] == 1
    assert got["scored"] == 0


def test_invented_words_are_counted_separately():
    """WER barely notices a fluent wrong answer, which is this feature's central risk."""
    got = score_turns(_rows(conv="Maas Approach, Motorvessel Example Trader proceeding inbound."),
                      REFERENCES, INDEX, use_conv=True)
    assert got["invented"] >= 2   # "proceeding", "inbound"


def test_the_corrected_arm_falls_back_to_live_text_when_a_turn_was_not_corrected():
    got = score_turns(_rows(), REFERENCES, INDEX, use_conv=True)
    assert got["scored"] == 2
