"""Tests for analyze_errors.py's substitution-frequency aggregation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import analyze_errors


def test_aggregate_substitutions_counts_recurring_pattern():
    results = {
        "beam5_prompt": [
            {"clip_id": "0001", "reference": "maas approach", "text": "mass approach"},
            {"clip_id": "0002", "reference": "maas control", "text": "mass control"},
            {"clip_id": "0003", "reference": "roger copy", "text": "roger copy"},
        ]
    }
    counts = analyze_errors.aggregate_substitutions(results)
    assert (("mass", "maas"), 2) in counts


def test_aggregate_substitutions_skips_rows_without_reference():
    results = {
        "beam5_prompt": [
            {"clip_id": "0001", "reference": None, "text": "anything"},
        ]
    }
    assert analyze_errors.aggregate_substitutions(results) == []


def test_aggregate_substitutions_sorted_by_count_descending():
    results = {
        "beam5_prompt": [
            {"clip_id": "0001", "reference": "buoy one", "text": "boy one"},
            {"clip_id": "0002", "reference": "buoy two", "text": "boy two"},
            {"clip_id": "0003", "reference": "buoy three", "text": "boy three"},
            {"clip_id": "0004", "reference": "ladder down", "text": "letter down"},
        ]
    }
    counts = analyze_errors.aggregate_substitutions(results)
    assert counts[0] == (("boy", "buoy"), 3)
    assert (("letter", "ladder"), 1) in counts
