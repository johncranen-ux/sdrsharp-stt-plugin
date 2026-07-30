"""Tests for make_references.py's enrichment stripping.

This matters more than its size suggests: on CH01 the plugin records the AIS-enriched
display string rather than the transcript, and any prefix left in becomes "ground truth"
nobody ever said -- silently corrupting every WER figure computed against it.

Run with: py -m pytest server/tests -v
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from make_references import strip_enrichment  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    # vessel tag + MMSI, the common CH01 shape
    ("[COR] (MMSI:244670731) Copy.", "Copy."),
    ("[CALLAO EXPRESS/tanker] (MMSI:218839000) Callao Express, over.", "Callao Express, over."),
    # vessel tag + callsign (the elif branch of format_for_plugin)
    ("[NEPTUNE/tanker] (PABC) Maas Approach, over.", "Maas Approach, over."),
    # vessel tag alone
    ("[INTERMEZZO] Understood, over.", "Understood, over."),
])
def test_strips_enrichment_prefixes(raw, expected):
    assert strip_enrichment(raw) == expected


@pytest.mark.parametrize("text", [
    "No prefix at all, over.",
    "this is [inaudible], calling on channel one",   # bracket, but not leading
    "(we think) he said something",                  # leading paren, no vessel tag
    "Maas Approach, this is Motortanker Neptune.",
])
def test_leaves_ordinary_transcripts_untouched(text):
    assert strip_enrichment(text) == text


def test_bare_mmsi_is_stripped_without_a_vessel_tag():
    assert strip_enrichment("(MMSI:244670731) Copy.") == "Copy."


def test_only_one_prefix_pair_is_removed():
    """Guards against a greedy pattern eating real speech that follows."""
    assert strip_enrichment("[SHIP] (MMSI:1) [inaudible] over.") == "[inaudible] over."


def test_empty_and_whitespace_are_safe():
    assert strip_enrichment("") == ""
    assert strip_enrichment("   ").strip() == ""
