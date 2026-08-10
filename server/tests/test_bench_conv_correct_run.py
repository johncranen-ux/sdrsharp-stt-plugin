"""Tests for bench_conv_correct_run.py: the missing driver for the bake-off.

bench_conversation_correct.py can only READ a "conv" key that is already on stored turns --
it has no way to produce one. This tool is what runs correct_conversation() offline over a
captured conversations.json and writes an annotated copy, so the bake-off has something to
score. No network here: `correct` is always a fake, injected exactly the way annotate() allows
production code to inject the real conversation_correct.correct_conversation.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from bench_conv_correct_run import annotate, main  # noqa: E402


def _exchange(start, turns, vessel="EXAMPLE TRADER"):
    """One stored exchange, shaped the way conversations.py::_store_resolved writes it.

    Stored turns have "time"/"text"/"raw"/"live_vessel" -- deliberately no "id". annotate()
    has to invent one per call, which is the whole reason the id-mapping test below exists.
    """
    return {"start": start, "vessel": vessel,
            "turns": [{"time": t, "text": text, "raw": text, "live_vessel": None}
                      for t, text in turns]}


def _rows():
    return [
        _exchange("2026-08-07 10:14:15", [
            ("10:14:15", "Maas Approach, motor vision Example Trader."),
            ("10:14:19", "Motorvessel Example Trader, Maas Approach."),
        ]),
        _exchange("2026-08-08 09:00:00", [
            ("09:00:00", "Rotterdam Pilot, Example Carrier inbound."),
        ], vessel="EXAMPLE CARRIER"),
    ]


def _fake_correct_all_unchanged(turns, vessel):
    return {t["id"]: {"text": t["corrected"], "changes": []} for t in turns}


def _fake_correct_none():
    """A stand-in for correct_conversation's documented failure signal."""
    def _fn(turns, vessel):
        return None
    return _fn


# ---------------------------------------------------------------------------
# annotate()
# ---------------------------------------------------------------------------

def test_a_correction_with_changes_is_written_onto_the_matching_turn():
    def fake_correct(turns, vessel):
        # Only the middle of three turns is corrected -- id 2.
        return {2: {"text": "Motorvessel Example Trader.", "changes": [
            {"from": "motor vision", "to": "Motorvessel", "reason": "shore"}]}}

    rows = [_exchange("2026-08-07 10:14:15", [
        ("10:14:15", "first, untouched"),
        ("10:14:19", "second, motor vision garble"),
        ("10:14:23", "third, untouched"),
    ])]

    new_rows, stats = annotate(rows, correct=fake_correct)

    turns = new_rows[0]["turns"]
    assert "conv" not in turns[0]
    assert turns[1]["conv"] == "Motorvessel Example Trader."
    assert turns[1]["changes"] == [{"from": "motor vision", "to": "Motorvessel", "reason": "shore"}]
    assert "conv" not in turns[2]
    assert stats == {"exchanges": 1, "corrected_exchanges": 1, "corrected_turns": 1, "failed": 0}


def test_ids_are_call_local_starting_at_one_and_map_back_by_position():
    """The id space correct() sees is 1-based per exchange, not a global turn index.

    Getting this wrong would attach one turn's correction to a different turn -- exactly the
    failure this test exists to catch, by asserting on which turn got the "conv" key, not
    just that some turn did.
    """
    seen_ids = []

    def fake_correct(turns, vessel):
        seen_ids.append([t["id"] for t in turns])
        assert seen_ids[-1] == [1, 2, 3]
        return {2: {"text": "MIDDLE FIXED", "changes": [{"from": "x", "to": "y"}]}}

    rows = [_exchange("2026-08-07 10:14:15", [
        ("10:14:15", "alpha"),
        ("10:14:19", "beta"),
        ("10:14:23", "gamma"),
    ])]

    new_rows, stats = annotate(rows, correct=fake_correct)
    turns = new_rows[0]["turns"]
    assert "conv" not in turns[0]
    assert turns[1]["conv"] == "MIDDLE FIXED"
    assert "conv" not in turns[2]


def test_a_turn_with_no_changes_declared_is_left_alone():
    """Matches _store_resolved's rule: absent, not equal-to-text, so 'not corrected' stays
    distinguishable from 'corrected to the same thing'."""
    rows = [_exchange("2026-08-07 10:14:15", [("10:14:15", "clean already")])]
    new_rows, stats = annotate(rows, correct=_fake_correct_all_unchanged)
    assert "conv" not in new_rows[0]["turns"][0]
    assert stats["corrected_turns"] == 0
    assert stats["corrected_exchanges"] == 0
    assert stats["exchanges"] == 1
    assert stats["failed"] == 0


def test_correct_returning_none_counts_as_failed_and_leaves_turns_untouched():
    rows = [_exchange("2026-08-07 10:14:15", [("10:14:15", "garbled beyond repair")])]
    new_rows, stats = annotate(rows, correct=_fake_correct_none())
    assert "conv" not in new_rows[0]["turns"][0]
    assert stats["failed"] == 1
    assert stats["corrected_exchanges"] == 0
    assert stats["exchanges"] == 1


def test_correct_never_raises_out_of_annotate():
    """A single malformed exchange must not abort the whole run."""
    def blows_up(turns, vessel):
        raise RuntimeError("boom")

    rows = [_exchange("2026-08-07 10:14:15", [("10:14:15", "x")])]
    new_rows, stats = annotate(rows, correct=blows_up)
    assert stats["failed"] == 1
    assert "conv" not in new_rows[0]["turns"][0]


def test_the_input_rows_are_never_mutated():
    rows = _rows()
    before = copy.deepcopy(rows)

    def fake_correct(turns, vessel):
        return {t["id"]: {"text": "REWRITTEN", "changes": [{"from": "a", "to": "b"}]}
                for t in turns}

    annotate(rows, correct=fake_correct)
    assert rows == before


def test_vessel_is_passed_through_from_the_row():
    seen_vessels = []

    def fake_correct(turns, vessel):
        seen_vessels.append(vessel)
        return None

    rows = _rows()
    annotate(rows, correct=fake_correct)
    assert seen_vessels == ["EXAMPLE TRADER", "EXAMPLE CARRIER"]


def test_an_exchange_with_no_turns_is_skipped_without_calling_correct():
    calls = []

    def fake_correct(turns, vessel):
        calls.append(turns)
        return None

    rows = [{"start": "2026-08-07 10:14:15", "vessel": "EXAMPLE TRADER", "turns": []}]
    new_rows, stats = annotate(rows, correct=fake_correct)
    assert calls == []
    assert stats["exchanges"] == 1
    assert stats["failed"] == 0


def test_default_correct_falls_back_to_conversation_correct_correct_conversation(monkeypatch):
    """annotate(rows) with no `correct` argument must reach for the real pass -- looked up on
    the module at call time (not bound at import time) so a test can monkeypatch it here
    without needing network."""
    import bench_conv_correct_run as mod
    from stt_proxy import conversation_correct

    calls = []
    monkeypatch.setattr(conversation_correct, "correct_conversation",
                        lambda turns, vessel: calls.append((turns, vessel)) or None)

    rows = [_exchange("2026-08-07 10:14:15", [("10:14:15", "x")])]
    mod.annotate(rows)
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

def _write_conversations(tmp_path, rows):
    path = tmp_path / "conversations.json"
    path.write_text(json.dumps(rows), encoding="utf-8")
    return path


def _stub_out_the_real_pass(monkeypatch):
    """main() has no CLI flag to inject `correct` (the spec's argv contract doesn't include
    one), so its only path to the network is the module-level conversation_correct.
    correct_conversation that annotate() falls back to. Patch that, same as the
    default-fallback test above, so main() tests stay network-free."""
    from stt_proxy import conversation_correct
    monkeypatch.setattr(conversation_correct, "correct_conversation",
                        lambda turns, vessel: None)


def test_main_writes_an_annotated_copy_and_leaves_the_input_untouched(tmp_path, capsys, monkeypatch):
    _stub_out_the_real_pass(monkeypatch)
    src = _write_conversations(tmp_path, _rows())
    original_text = src.read_text(encoding="utf-8")
    out = tmp_path / "out.json"

    rc = main(["--conversations", str(src), "--out", str(out)])

    assert rc == 0
    assert src.read_text(encoding="utf-8") == original_text
    written = json.loads(out.read_text(encoding="utf-8"))
    assert len(written) == 2
    captured = capsys.readouterr()
    assert "exchanges" in captured.out


def test_main_filters_by_day_before_applying_limit(tmp_path, monkeypatch):
    """--limit is a cost control and must apply AFTER --day filtering, not before -- otherwise
    --limit could eat the budget on days that --day was meant to exclude."""
    _stub_out_the_real_pass(monkeypatch)
    rows = [
        _exchange("2026-08-07 10:00:00", [("10:00:00", "a")]),
        _exchange("2026-08-07 11:00:00", [("11:00:00", "b")]),
        _exchange("2026-08-08 09:00:00", [("09:00:00", "c")]),
    ]
    src = _write_conversations(tmp_path, rows)
    out = tmp_path / "out.json"

    rc = main(["--conversations", str(src), "--out", str(out),
               "--day", "2026-08-07", "--limit", "1"])

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    # Both 08-07 rows exist; --limit 1 must cut the 08-07-filtered set down to 1, never let a
    # limit applied first admit the 08-08 row instead.
    assert len(written) == 1
    assert written[0]["start"].startswith("2026-08-07")


def test_main_day_is_repeatable(tmp_path, monkeypatch):
    _stub_out_the_real_pass(monkeypatch)
    rows = [
        _exchange("2026-08-07 10:00:00", [("10:00:00", "a")]),
        _exchange("2026-08-08 09:00:00", [("09:00:00", "b")]),
        _exchange("2026-08-09 09:00:00", [("09:00:00", "c")]),
    ]
    src = _write_conversations(tmp_path, rows)
    out = tmp_path / "out.json"

    rc = main(["--conversations", str(src), "--out", str(out),
               "--day", "2026-08-07", "--day", "2026-08-09"])

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    starts = {row["start"][:10] for row in written}
    assert starts == {"2026-08-07", "2026-08-09"}


def test_main_requires_conversations_and_out():
    # argparse enforces required=True itself: a missing required flag exits via SystemExit(2)
    # rather than returning, same as every other required flag in these bench_*.py tools.
    with pytest.raises(SystemExit):
        main([])
