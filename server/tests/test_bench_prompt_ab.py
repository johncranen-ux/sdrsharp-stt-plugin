"""Tests for the paired comparison in bench_prompt_ab.py.

The arithmetic here decides whether a prompt change ships, so the failure that matters is a
silent one: an arm flattered by clips the other arm lost, or a confidence interval that
reports certainty the clip set cannot support.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import bench_prompt_ab as ab  # noqa: E402


def _row(clip_id, reference, text, error=None):
    return {"clip_id": clip_id, "reference": reference, "text": text, "error": error,
            "elapsed": 0.1, "wer": None}


def _arm(*rows):
    return {r["clip_id"]: r for r in rows}


# ---------------------------------------------------------------------------
# Scored-set selection
# ---------------------------------------------------------------------------

def test_clip_errored_in_one_arm_is_dropped_from_both():
    arms = {
        "a": _arm(_row("1", "roger over", "roger over"),
                  _row("2", "standing by", "standing by")),
        "b": _arm(_row("1", "roger over", "roger over"),
                  _row("2", "standing by", "", error="HTTP 429: rate limited")),
    }
    scored, _no_ref, errored = ab.select_scored(arms)
    assert scored == ["1"]
    assert errored == ["2"]


def test_clip_without_a_reference_is_not_scored():
    arms = {
        "a": _arm(_row("1", "", "something"), _row("2", "roger", "roger")),
        "b": _arm(_row("1", "", "something else"), _row("2", "roger", "roger")),
    }
    scored, no_ref, _errored = ab.select_scored(arms)
    assert scored == ["2"]
    assert no_ref == ["1"]


def test_clips_missing_from_one_arm_are_not_scored():
    arms = {
        "a": _arm(_row("1", "roger", "roger"), _row("2", "over", "over")),
        "b": _arm(_row("1", "roger", "roger")),
    }
    scored, _no_ref, _errored = ab.select_scored(arms)
    assert scored == ["1"]


# ---------------------------------------------------------------------------
# Pooled WER
# ---------------------------------------------------------------------------

def test_pooled_wer_weights_by_reference_length_not_by_clip():
    # One 1-word clip wrong and one 9-word clip right is 10% pooled, not the 50% a
    # per-clip average would report.
    rows = _arm(_row("short", "over", "roger"),
                _row("long", "maas approach this is neptune calling on channel one six",
                     "maas approach this is neptune calling on channel one six"))
    wer, edits, words = ab.pooled_wer(["short", "long"], rows)
    assert (edits, words) == (1, 11)
    assert abs(wer - 1 / 11) < 1e-9


def test_pooled_wer_ignores_clips_outside_the_scored_list():
    rows = _arm(_row("1", "roger", "roger"), _row("2", "over", "wrong"))
    wer, _edits, _words = ab.pooled_wer(["1"], rows)
    assert wer == 0.0


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------

def test_identical_arms_give_a_zero_width_interval():
    rows = [_row(str(i), "maas approach this is neptune over", "mass approach this is neptune over")
            for i in range(20)]
    arms = {"a": _arm(*rows), "b": _arm(*[dict(r) for r in rows])}
    scored, _, _ = ab.select_scored(arms)
    lo, hi = ab.bootstrap_ci(scored, arms["a"], arms["b"], iterations=200, seed=1)
    assert lo == hi == 0.0


def test_interval_spans_zero_when_one_clip_differs_out_of_many():
    good = [_row(str(i), "roger copy over", "roger copy over") for i in range(19)]
    arms = {
        "a": _arm(*good, _row("odd", "roger copy over", "roger copy over")),
        "b": _arm(*[dict(r) for r in good], _row("odd", "roger copy over", "mass copy over")),
    }
    scored, _, _ = ab.select_scored(arms)
    lo, hi = ab.bootstrap_ci(scored, arms["a"], arms["b"], iterations=2000, seed=7)
    assert lo <= 0.0 <= hi, "a single differing clip in 20 must not read as a real difference"


def test_bootstrap_is_reproducible_for_a_given_seed():
    rows_a = [_row(str(i), "maas approach over", "mass approach over") for i in range(15)]
    rows_b = [_row(str(i), "maas approach over", "maas approach over") for i in range(15)]
    arms = {"a": _arm(*rows_a), "b": _arm(*rows_b)}
    scored, _, _ = ab.select_scored(arms)
    first = ab.bootstrap_ci(scored, arms["a"], arms["b"], iterations=500, seed=42)
    second = ab.bootstrap_ci(scored, arms["a"], arms["b"], iterations=500, seed=42)
    assert first == second


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def test_load_arm_flattens_configs_and_keys_on_clip_id(tmp_path):
    path = tmp_path / "results.json"
    path.write_text(json.dumps({
        "model_label": "groq-whisper-large-v3",
        "results": {"groq_prompt": [_row("0001", "roger", "roger")]},
    }), encoding="utf-8")
    label, rows = ab.load_arm(f"shipped={path}")
    assert label == "shipped"
    assert list(rows) == ["0001"]


# ---------------------------------------------------------------------------
# Echo filter
# ---------------------------------------------------------------------------

_PROMPT = ("Maas Approach, this is Motortanker Neptune, callsign PABC, requesting permission "
           "to enter the Botlek, over.")


def test_echo_filter_blanks_output_that_is_the_prompt_read_back():
    rows = _arm(_row("1", "understood standby zero one", "Motortanker Neptune, callsign PABC, over."))
    rows["1"]["_arm_prompt"] = _PROMPT
    suppressed = ab.apply_echo_filter({"a": rows})
    assert suppressed["a"] == ["1"]
    assert rows["1"]["text"] == ""


def test_echo_filter_leaves_real_speech_alone():
    rows = _arm(_row("1", "maas approach over", "Maas Approach, over."))
    rows["1"]["_arm_prompt"] = _PROMPT
    suppressed = ab.apply_echo_filter({"a": rows})
    assert suppressed["a"] == []
    assert rows["1"]["text"] == "Maas Approach, over."


def test_echo_filter_uses_each_arms_own_prompt():
    # Text echoing arm A's prompt is ordinary output for arm B, whose prompt lacks those words.
    echo = "Motortanker Neptune, callsign PABC, over."
    a = _arm(_row("1", "understood standby zero one", echo))
    b = _arm(_row("1", "understood standby zero one", echo))
    a["1"]["_arm_prompt"] = _PROMPT
    b["1"]["_arm_prompt"] = "Rotterdam VTS, standing by on channel one six."
    suppressed = ab.apply_echo_filter({"a": a, "b": b})
    assert suppressed == {"a": ["1"], "b": []}


def test_echo_filter_is_a_noop_when_no_prompt_was_recorded():
    rows = _arm(_row("1", "roger", "Motortanker Neptune, callsign PABC, over."))
    rows["1"]["_arm_prompt"] = ""
    assert ab.apply_echo_filter({"a": rows})["a"] == []
