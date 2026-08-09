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


def test_word_alignment_ops_identifies_substitution():
    ops = bench.word_alignment_ops("mass approach over", "maas approach over")
    subs = [(o[1], o[2]) for o in ops if o[0] == "sub"]
    assert ("mass", "maas") in subs


def test_word_alignment_ops_identifies_insertion_and_deletion():
    ins_ops = bench.word_alignment_ops("roger copy over", "roger copy over standing by")
    assert sum(1 for o in ins_ops if o[0] == "ins") == 2

    del_ops = bench.word_alignment_ops("roger copy over standing by", "roger copy over")
    assert sum(1 for o in del_ops if o[0] == "del") == 2


def test_word_alignment_ops_matches_are_marked():
    ops = bench.word_alignment_ops("roger copy over", "roger copy over")
    assert all(o[0] == "match" for o in ops)
    assert len(ops) == 3


def test_word_alignment_ops_no_reference_returns_none():
    assert bench.word_alignment_ops("", "anything") is None


def test_word_alignment_ops_edit_count_matches_word_error_counts():
    ref, hyp = "maas approach this is neptune over", "mass approach this is fjordstrom over standing by"
    ops = bench.word_alignment_ops(ref, hyp)
    non_matches = sum(1 for o in ops if o[0] != "match")
    edits, _ = bench.word_error_counts(ref, hyp)
    assert non_matches == edits


# ---------------------------------------------------------------------------
# Prompt reconciliation
#
# bench.py used to define its own MARITIME_PROMPT, which drifted from the one the proxy
# sends -- so every WER figure on record was measured against text that was never deployed.
# These pin the two together so the drift cannot silently come back.
# ---------------------------------------------------------------------------

from stt_proxy import backends  # noqa: E402


def test_bench_prompt_is_the_prompt_the_proxy_actually_sends():
    assert bench.MARITIME_PROMPT == backends.DEFAULT_MARITIME_PROMPT


def test_prompt_bearing_configs_all_use_the_shipped_prompt():
    prompted = [c for c in bench.CONFIGS.values() if "prompt" in c]
    assert prompted, "expected at least one prompt-bearing config"
    assert all(c["prompt"] == backends.DEFAULT_MARITIME_PROMPT for c in prompted)


def test_legacy_prompt_is_kept_distinct_and_selectable():
    # It only earns its place in the tree while it differs from the shipped prompt; once
    # the A/B is written up and it is retired, this test should go with it.
    assert bench.LEGACY_BENCH_PROMPT != bench.MARITIME_PROMPT
    assert bench.PROMPTS["shipped"] == bench.MARITIME_PROMPT
    assert bench.PROMPTS["legacy"] == bench.LEGACY_BENCH_PROMPT


def test_every_prompt_fits_groqs_length_cap():
    # Over-length is a hard 400 from Groq, which costs a real chunk of radio audio.
    # _truncate_prompt would save the request but silently bench a different prompt.
    for name, text in bench.PROMPTS.items():
        assert len(text.split()) <= backends.GROQ_PROMPT_MAX_WORDS, name


def test_shipped_prompt_contains_no_invented_vessel_name_or_callsign():
    # A name in the prompt can be echoed into output and then matched against AIS, which is
    # how a phantom vessel with a real MMSI is born -- measured happening on clips 0068 and
    # 0188 under the v1 prompt. This is the regression guard for that.
    for name in ("shipped", "no_names"):
        words = set(bench.PROMPTS[name].lower().split())
        assert "neptune" not in words, name
        assert "pabc" not in words, name


def test_superseded_prompts_are_kept_for_reproducibility():
    # Every figure on record was measured against one of these; deleting them would make
    # those numbers unreproducible.
    assert bench.PROMPTS["v1_names"] != bench.MARITIME_PROMPT
    assert "neptune" in bench.PROMPTS["v1_names"].lower()


# ---------------------------------------------------------------------------
# Refusing to present a broken run as a result
#
# On 2026-08-09 every clip of a 97-clip run failed before the request left the machine
# (Git Bash rewrote the --path argument into a Windows path), and bench.py printed its
# usual summary table, wrote the JSON and the HTML, and exited 0. It did warn -- but a
# warning scrolls past, the exit code said success, and the written JSON is indistinguishable
# to any downstream tool from a run that genuinely found nothing to transcribe. Comparing two
# such runs shows them "identical", which reads as a clean null result.
#
# The distinction that matters and was missing: a clip that ERRORED is a broken run, while a
# clip that transcribed to empty text may simply have held no speech -- the replay harness
# writes silence for a segment past a short arm's end, so empty output is expected sometimes.
# ---------------------------------------------------------------------------

def _row(clip_id, text="something said", error=None):
    return {"clip_id": clip_id, "text": text, "elapsed": 0.4,
            "wer": None, "error": error, "reference": None}


def test_a_healthy_run_says_nothing():
    rows = [_row(f"{i:04d}") for i in range(10)]
    assert bench.run_health(rows) is None


def test_a_run_where_every_clip_errored_is_fatal():
    """THE case. All 97 failed identically and the run still exited 0."""
    rows = [_row(f"{i:04d}", text="", error="InvalidURL: bad path") for i in range(97)]
    fatal, message = bench.run_health(rows)
    assert fatal is True
    assert "97" in message and "InvalidURL" in message


def test_a_few_failures_are_reported_but_not_fatal():
    """One clip timing out does not invalidate the other ninety-six."""
    rows = [_row(f"{i:04d}") for i in range(20)]
    rows[3] = _row("0003", text="", error="timeout")
    fatal, message = bench.run_health(rows)
    assert fatal is False
    assert "1" in message and "20" in message


def test_every_clip_empty_without_an_error_is_still_fatal():
    """A server that answers 200 with nothing produces no error and no text. The numbers
    from that run are not a result either."""
    rows = [_row(f"{i:04d}", text="") for i in range(30)]
    fatal, message = bench.run_health(rows)
    assert fatal is True
    assert "empty" in message.lower()


def test_some_empty_clips_are_normal_and_not_flagged():
    """segments.cut() yields an empty clip for a segment past a shorter arm's end, and
    iq_replay writes silence for it. Silence transcribing to nothing is correct behaviour,
    not a broken run -- flagging it would train the operator to ignore the warning."""
    rows = [_row(f"{i:04d}") for i in range(20)]
    for i in (2, 7, 11):
        rows[i] = _row(f"{i:04d}", text="")
    assert bench.run_health(rows) is None


def test_no_clips_at_all_is_fatal():
    fatal, message = bench.run_health([])
    assert fatal is True and "no clips" in message.lower()
