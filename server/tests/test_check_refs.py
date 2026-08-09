"""Tests for check_refs.py -- validating a hand-edited reference file before it is scored.

bench.load_references skips any line with neither a tab nor a colon, silently: no error, no
warning, the clip simply disappears from the run and pooled WER is computed over fewer clips
than the operator thinks. An editor configured to insert spaces instead of tabs does exactly
that to every line it touches. This is the same failure family as everything else the harness
has produced -- a confident-looking number computed over the wrong data -- so the check exists
to make it loud.
"""

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import check_refs  # noqa: E402


def _refs(tmp_path, text, name="refs.txt"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def _captures(tmp_path, clip_ids):
    d = tmp_path / "arm"
    d.mkdir(exist_ok=True)
    for cid in clip_ids:
        (d / f"{cid}_sent.wav").write_bytes(b"")
    return d


def test_a_clean_file_passes(tmp_path):
    p = _refs(tmp_path, "# a comment\n\n0000\tMaas Approach\n0001\tRoger over\n")
    r = check_refs.check(p)
    assert r["ok"] is True
    assert r["data_lines"] == r["parsed"] == 2
    assert r["dropped"] == []


def test_a_line_whose_tab_became_spaces_is_reported(tmp_path):
    """THE case. load_references strips the line and then finds no separator, so it skips
    it and returns a dict that looks perfectly healthy."""
    p = _refs(tmp_path, "0000\tMaas Approach\n0001    Roger over\n")
    r = check_refs.check(p)
    assert r["ok"] is False
    assert r["parsed"] == 1 and r["data_lines"] == 2
    assert len(r["dropped"]) == 1 and "Roger over" in r["dropped"][0]


def test_the_colon_form_is_accepted(tmp_path):
    """Documented as the tab-safe alternative, so it must not be flagged."""
    p = _refs(tmp_path, "0000: Maas Approach\n")
    r = check_refs.check(p)
    assert r["ok"] is True and r["parsed"] == 1 and r["dropped"] == []


def test_an_explicitly_empty_reference_is_reported_but_not_dropped(tmp_path):
    """'0066:' is how a static-only clip is marked. It parses (the colon survives .strip(),
    unlike a trailing tab), and it is worth surfacing so it stays a decision rather than an
    accident -- but it is not a broken line."""
    p = _refs(tmp_path, "0000\tMaas Approach\n0066:\n")
    r = check_refs.check(p)
    assert r["ok"] is True
    assert r["empty"] == ["0066"]
    assert r["dropped"] == []


def test_a_trailing_tab_alone_does_NOT_survive(tmp_path):
    """The trap that produced three silently-missing clips in the real 2026-08-09 draft:
    load_references strips the line first, which removes a trailing tab, leaving no
    separator at all. Use the colon form instead."""
    p = _refs(tmp_path, "0066\t\n")
    r = check_refs.check(p)
    assert r["ok"] is False and r["parsed"] == 0


def test_comments_and_blank_lines_are_not_counted_as_data(tmp_path):
    p = _refs(tmp_path, "# header\n\n#   --- DIFFERS ---\n0000\tSomething\n\n")
    r = check_refs.check(p)
    assert r["data_lines"] == 1 and r["ok"] is True


def test_clips_without_a_reference_are_listed(tmp_path):
    """Those clips are excluded from scoring entirely, which changes the denominator."""
    p = _refs(tmp_path, "0000\tSomething\n")
    caps = _captures(tmp_path, ["0000", "0001", "0002"])
    r = check_refs.check(p, caps)
    assert r["clips"] == 3 and r["with_reference"] == 1
    assert r["missing"] == ["0001", "0002"]


def test_a_reference_id_matching_no_clip_is_listed(tmp_path):
    """A typo'd id scores nothing and would otherwise never be noticed."""
    p = _refs(tmp_path, "0000\tSomething\n9999\tTypo\n")
    caps = _captures(tmp_path, ["0000"])
    r = check_refs.check(p, caps)
    assert r["extra"] == ["9999"]


def test_without_a_captures_directory_the_clip_checks_are_skipped(tmp_path):
    p = _refs(tmp_path, "0000\tSomething\n")
    r = check_refs.check(p)
    assert r["clips"] is None and r["missing"] == [] and r["extra"] == []


def test_a_missing_file_is_an_error_not_a_pass(tmp_path):
    """load_references returns {} for a path that does not exist, which would otherwise
    render as a clean run over zero clips."""
    r = check_refs.check(tmp_path / "nope.txt")
    assert r["ok"] is False
