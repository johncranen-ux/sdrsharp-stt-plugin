"""Tests for fewshot.py: runtime-loaded examples with a synthetic fallback."""

import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import fewshot  # noqa: E402


def test_a_missing_file_falls_back_to_the_synthetic_set():
    """A fresh checkout has no references file and must still work."""
    got = fewshot.load_examples("no/such/file.json")
    assert got == fewshot.SYNTHETIC_EXAMPLES
    assert got, "the synthetic set must not be empty"


def test_no_path_configured_falls_back_to_the_synthetic_set(monkeypatch):
    monkeypatch.delenv("CONVERSATION_FEWSHOT_FILE", raising=False)
    assert fewshot.load_examples() == fewshot.SYNTHETIC_EXAMPLES


def test_examples_load_from_the_configured_file(tmp_path):
    payload = [{
        "vessel": "EXAMPLE TRADER",
        "turns": [{"id": 1, "text": "Maas Approach, motor vision Example Trader."}],
        "output": {"turns": [{"id": 1, "text": "Maas Approach, Motorvessel Example Trader.",
                              "changes": [{"from": "motor vision", "to": "Motorvessel",
                                           "reason": "shore station rendition"}]}]},
    }]
    path = tmp_path / "examples.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    got = fewshot.load_examples(str(path))
    assert got[0]["vessel"] == "EXAMPLE TRADER"
    assert got[0]["output"]["turns"][0]["changes"][0]["to"] == "Motorvessel"


def test_a_malformed_file_falls_back_rather_than_crashing(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    assert fewshot.load_examples(str(path)) == fewshot.SYNTHETIC_EXAMPLES


def test_the_synthetic_examples_name_no_real_vessel():
    """Examples must teach patterns, not a roster. A real cached name in an example invites
    the model to reach for it elsewhere -- the failure mode the live prompt's rule 5 already
    guards against for AIS hints."""
    for example in fewshot.SYNTHETIC_EXAMPLES:
        assert "EXAMPLE" in (example["vessel"] or "").upper()


def test_rendering_produces_one_block_per_example():
    text = fewshot.render_examples(fewshot.SYNTHETIC_EXAMPLES)
    assert text.count("[EXAMPLE INPUT]") == len(fewshot.SYNTHETIC_EXAMPLES)
    assert text.count("[EXAMPLE OUTPUT]") == len(fewshot.SYNTHETIC_EXAMPLES)


def test_rendering_nothing_is_an_empty_string():
    assert fewshot.render_examples([]) == ""
