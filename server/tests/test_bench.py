"""Tests for bench.py's WER scoring, in particular the [inaudible]/uncertain-word
conventions used when hand-transcribing real (often noisy) VHF captures.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bench


def test_wer_identical_is_zero():
    assert bench.word_error_rate("roger copy over", "Roger, copy, over.") == 0.0


def test_wer_no_reference_is_excluded():
    assert bench.word_error_rate("", "anything") is None


def test_wer_bracketed_inaudible_span_is_not_penalized_as_a_missing_word():
    # The hypothesis word(s) standing in for the inaudible span show up as an "insertion"
    # against the shortened reference, not as Whisper failing to literally say "inaudible".
    ref = "Maas Approach, this is [inaudible], calling on channel one"
    hyp_matching_rest = "Maas Approach, this is Motortanker Neptune, calling on channel one"
    hyp_totally_wrong = "static static static static static static static static"

    wer_matching = bench.word_error_rate(ref, hyp_matching_rest)
    wer_wrong = bench.word_error_rate(ref, hyp_totally_wrong)

    assert wer_matching is not None and wer_matching < 0.5
    assert wer_wrong > wer_matching  # still distinguishes good vs. bad transcription elsewhere


def test_wer_uncertain_word_marker_scores_as_a_normal_word():
    assert bench.word_error_rate("Motortanker Fjordstrom?", "Motortanker Fjordstrom") == 0.0
    assert bench.word_error_rate("Motortanker Fjordstrom?", "Motortanker Neptune") == 0.5


def test_wer_whole_clip_inaudible_is_excluded_like_an_empty_reference():
    assert bench.word_error_rate("[inaudible]", "anything at all") is None


def test_word_error_counts_matches_word_error_rate():
    ref, hyp = "roger copy over standing by", "roger copy over"
    counts = bench.word_error_counts(ref, hyp)
    assert counts is not None
    edits, ref_len = counts
    assert ref_len == 5
    assert edits / ref_len == bench.word_error_rate(ref, hyp)


def test_pooled_wer_weights_by_reference_length_not_clip_count():
    # One 1-word clip that's totally wrong, one 10-word clip that's perfect.
    # Macro-average would show 50% (average of 100% and 0%); pooled should show ~9%
    # (1 error out of 11 total reference words) since it weights by actual word count.
    rows = [
        {"reference": "correct", "text": "wrong"},
        {"reference": "one two three four five six seven eight nine ten", "text": "one two three four five six seven eight nine ten"},
    ]
    pooled = bench._pooled_wer(rows)
    assert pooled is not None
    assert abs(pooled - (1 / 11)) < 1e-9


def test_pooled_wer_excludes_rows_without_a_reference():
    rows = [
        {"reference": None, "text": "irrelevant"},
        {"reference": "hello world", "text": "hello world"},
    ]
    assert bench._pooled_wer(rows) == 0.0
