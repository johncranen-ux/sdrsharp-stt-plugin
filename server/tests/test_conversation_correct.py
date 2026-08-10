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
