"""Tests for clip_index.py: joining capture clip ids to timestamps."""

import datetime
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from clip_index import clip_for_time, load_clip_index  # noqa: E402


def _write_index(tmp_path, rows, bom=True):
    text = "\n".join(rows)
    data = text.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    (tmp_path / "index.jsonl").write_bytes(data)
    return tmp_path


def test_reads_an_index_written_with_a_bom(tmp_path):
    """The plugin writes UTF-8 with a BOM; plain utf-8 decoding raises on the first line."""
    _write_index(tmp_path, [
        '{"index": 0, "timestamp": "2026-08-07T10:14:15.215+02:00", "channel": "160,650"}',
    ])
    assert load_clip_index(tmp_path) == {
        "0000": datetime.datetime(2026, 8, 7, 10, 14, 15, 215000)}


def test_clip_ids_are_zero_padded_to_match_reference_keys(tmp_path):
    """index is a number in the JSON; references are keyed '0007'."""
    _write_index(tmp_path, [
        '{"index": 7, "timestamp": "2026-08-07T10:20:00+02:00"}',
    ])
    assert list(load_clip_index(tmp_path)) == ["0007"]


def test_a_turn_time_finds_its_clip(tmp_path):
    _write_index(tmp_path, [
        '{"index": 0, "timestamp": "2026-08-07T10:14:15+02:00"}',
        '{"index": 1, "timestamp": "2026-08-07T10:14:19+02:00"}',
    ])
    index = load_clip_index(tmp_path)
    when = datetime.datetime(2026, 8, 7, 10, 14, 19)
    assert clip_for_time(index, when) == "0001"


def test_a_time_outside_tolerance_matches_nothing(tmp_path):
    """Better no clip than the wrong clip: a wrong join silently scores one turn's text
    against another turn's reference, which reads as a quality change that never happened."""
    _write_index(tmp_path, [
        '{"index": 0, "timestamp": "2026-08-07T10:14:15+02:00"}',
    ])
    index = load_clip_index(tmp_path)
    assert clip_for_time(index, datetime.datetime(2026, 8, 7, 10, 30, 0)) is None


def test_a_malformed_row_is_skipped_not_fatal(tmp_path):
    _write_index(tmp_path, [
        '{"index": 0, "timestamp": "2026-08-07T10:14:15+02:00"}',
        'not json at all',
        '{"index": 2, "timestamp": "2026-08-07T10:14:31+02:00"}',
    ])
    assert sorted(load_clip_index(tmp_path)) == ["0000", "0002"]


def test_a_missing_index_file_is_an_empty_mapping(tmp_path):
    assert load_clip_index(tmp_path) == {}
