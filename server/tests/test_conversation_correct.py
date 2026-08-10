"""Tests for conversation_correct.py: the pass that repairs a turn from its conversation."""

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import conversation_correct as cc  # noqa: E402


TURNS = [
    {"id": 1, "corrected": "Maas Approach, motor vision Example Trader."},
    {"id": 2, "corrected": "Motorvessel Example Trader, Maas Approach."},
]


def _reply(turns):
    return {"turns": turns}


def test_a_well_formed_reply_maps_id_to_text_and_changes():
    got = cc.validate_reply(_reply([
        {"id": 1, "text": "Maas Approach, Motorvessel Example Trader.",
         "changes": [{"from": "motor vision", "to": "Motorvessel", "reason": "shore station"}]},
        {"id": 2, "text": "Motorvessel Example Trader, Maas Approach.", "changes": []},
    ]), TURNS)
    assert got[1]["text"] == "Maas Approach, Motorvessel Example Trader."
    assert got[2]["changes"] == []


def test_a_dropped_turn_is_rejected():
    """Losing a turn silently truncates a conversation the operator is reading."""
    with pytest.raises(cc.CorrectionRejected, match="missing"):
        cc.validate_reply(_reply([
            {"id": 1, "text": "x", "changes": [{"from": "a", "to": "x", "reason": "r"}]},
        ]), TURNS)


def test_an_invented_turn_id_is_rejected():
    with pytest.raises(cc.CorrectionRejected, match="unknown id"):
        cc.validate_reply(_reply([
            {"id": 1, "text": TURNS[0]["corrected"], "changes": []},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
            {"id": 99, "text": "invented", "changes": []},
        ]), TURNS)


def test_a_duplicated_turn_id_is_rejected():
    with pytest.raises(cc.CorrectionRejected, match="twice"):
        cc.validate_reply(_reply([
            {"id": 1, "text": TURNS[0]["corrected"], "changes": []},
            {"id": 1, "text": "again", "changes": []},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_an_undeclared_rewrite_is_rejected():
    """The whole audit trail rests on this: no changes declared means nothing changed.
    Without it, a rewrite with an empty changes list is invisible forever."""
    with pytest.raises(cc.CorrectionRejected, match="undeclared"):
        cc.validate_reply(_reply([
            {"id": 1, "text": "something completely different", "changes": []},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_a_declared_change_with_identical_text_is_rejected():
    """The mirror case: claiming a change that was not made makes the audit trail lie."""
    with pytest.raises(cc.CorrectionRejected, match="declared"):
        cc.validate_reply(_reply([
            {"id": 1, "text": TURNS[0]["corrected"],
             "changes": [{"from": "motor vision", "to": "Motorvessel", "reason": "r"}]},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_a_missing_turns_key_is_rejected():
    with pytest.raises(cc.CorrectionRejected, match="no turns"):
        cc.validate_reply({"result": "ok"}, TURNS)


def test_empty_text_is_rejected():
    """Never remove content: an emptied turn is content removal in its purest form."""
    with pytest.raises(cc.CorrectionRejected, match="empty"):
        cc.validate_reply(_reply([
            {"id": 1, "text": "", "changes": [{"from": "a", "to": "", "reason": "r"}]},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_whitespace_only_text_is_rejected():
    """A turn stripped to nothing is content removal. The .strip() guard must be present."""
    with pytest.raises(cc.CorrectionRejected, match="empty"):
        cc.validate_reply(_reply([
            {"id": 1, "text": "   ", "changes": [{"from": "a", "to": "   ", "reason": "r"}]},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_non_list_changes_is_rejected():
    """If changes field is not a list, the audit trail format is broken."""
    with pytest.raises(cc.CorrectionRejected, match="no changes list"):
        cc.validate_reply(_reply([
            {"id": 1, "text": TURNS[0]["corrected"], "changes": "nope"},
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


def test_non_dict_turn_entry_is_rejected():
    """A turn entry that is not an object means the model output structure is corrupted."""
    with pytest.raises(cc.CorrectionRejected, match="not an object"):
        cc.validate_reply(_reply([
            {"id": 1, "text": TURNS[0]["corrected"], "changes": []},
            "this is a string, not a turn object",
            {"id": 2, "text": TURNS[1]["corrected"], "changes": []},
        ]), TURNS)


from stt_proxy import llm  # noqa: E402


def test_the_input_lists_turns_with_ids_and_the_resolved_vessel():
    text = cc.render_input(TURNS, "EXAMPLE TRADER")
    assert "1. Maas Approach, motor vision Example Trader." in text
    assert "EXAMPLE TRADER" in text


def test_the_input_says_so_when_nobody_was_identified():
    text = cc.render_input(TURNS, None)
    assert "unidentified" in text


def test_correct_conversation_returns_validated_corrections(monkeypatch):
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k: (
        '{"turns": [{"id": 1, "text": "Maas Approach, Motorvessel Example Trader.",'
        ' "changes": [{"from": "motor vision", "to": "Motorvessel", "reason": "shore"}]},'
        ' {"id": 2, "text": "Motorvessel Example Trader, Maas Approach.", "changes": []}]}'))
    got = cc.correct_conversation(TURNS, "EXAMPLE TRADER")
    assert got[1]["text"] == "Maas Approach, Motorvessel Example Trader."


def test_a_fenced_reply_is_still_accepted(monkeypatch):
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k: (
        '```json\n{"turns": [{"id": 1, "text": "Maas Approach, motor vision Example Trader.",'
        ' "changes": []}, {"id": 2, "text": "Motorvessel Example Trader, Maas Approach.",'
        ' "changes": []}]}\n```'))
    assert cc.correct_conversation(TURNS, None) is not None


def test_a_provider_failure_returns_none(monkeypatch):
    def boom(*a, **k):
        raise llm.LLMError("timeout")
    monkeypatch.setattr(cc.llm, "complete", boom)
    assert cc.correct_conversation(TURNS, "EXAMPLE TRADER") is None


def test_malformed_json_returns_none(monkeypatch):
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k: "not json at all")
    assert cc.correct_conversation(TURNS, None) is None


def test_a_contract_violation_returns_none(monkeypatch):
    """Rejected means the conversation is stored uncorrected, not partly corrected."""
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k:
                        '{"turns": [{"id": 1, "text": "x", "changes": []}]}')
    assert cc.correct_conversation(TURNS, None) is None


def test_no_turns_needs_no_call(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not call the model for an empty exchange")
    monkeypatch.setattr(cc.llm, "complete", boom)
    assert cc.correct_conversation([], None) is None


def test_the_prompt_forbids_naming_a_turn_that_named_nobody():
    assert "named nobody" in cc.SYSTEM_PROMPT.lower()


def test_the_prompt_keeps_digit_sequences_as_transcribed():
    assert "one three zero zero" in cc.SYSTEM_PROMPT.lower()


def test_malformed_examples_degrade_to_running_without_them(monkeypatch):
    """Failing to render examples must degrade, not return None or raise."""
    monkeypatch.setattr(cc.fewshot, "load_examples", lambda: [
        {"id": 1, "text": "example"}  # Missing required fields for render_examples
    ])
    monkeypatch.setattr(cc.llm, "complete", lambda *a, **k: (
        '{"turns": [{"id": 1, "text": "Maas Approach, motor vision Example Trader.",'
        ' "changes": []}, {"id": 2, "text": "Motorvessel Example Trader, Maas Approach.",'
        ' "changes": []}]}'))
    # Should return corrections, not None or raise.
    assert cc.correct_conversation(TURNS, None) is not None


def test_temperature_zero_is_passed_to_the_model(monkeypatch):
    """The A/B measurement was rendered uninterpretable by sampling noise.
    This hard requirement must be actively verified, not relied on llm.complete's default."""
    call_kwargs = {}
    def capture(*a, **k):
        call_kwargs.update(k)
        return '{"turns": [{"id": 1, "text": "x", "changes": []}, {"id": 2, "text": "y", "changes": []}]}'
    monkeypatch.setattr(cc.llm, "complete", capture)
    cc.correct_conversation(TURNS, None)
    assert call_kwargs["temperature"] == 0
