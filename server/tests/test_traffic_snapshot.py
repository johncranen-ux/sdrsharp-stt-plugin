"""The accumulation rule in tools/traffic_snapshot.py.

GitHub's traffic API returns only the last 14 days and deletes what falls out. The whole
point of the script is to keep what GitHub discards, so the merge has exactly one way to be
catastrophically wrong: treating the fetched window as authoritative and dropping stored days
it no longer mentions. That would wipe the history on every run while still printing a
plausible-looking summary -- a silent failure, which is the kind this project pins with tests.
"""
import json
import sys
from pathlib import Path

import pytest

_TOOLS = Path(__file__).resolve().parent.parent.parent / "tools"

# tools/ is repo tooling and is deliberately NOT in the release archive, which ships server/
# from the tracked file list. Skip rather than fail when running the suite from that archive.
pytestmark = pytest.mark.skipif(not (_TOOLS / "traffic_snapshot.py").exists(),
                                reason="tools/ is not present (running from a release archive)")

if (_TOOLS / "traffic_snapshot.py").exists():
    sys.path.insert(0, str(_TOOLS))
    import traffic_snapshot as ts


def _bucket(day, count, uniques):
    return {"timestamp": f"{day}T00:00:00Z", "count": count, "uniques": uniques}


# --- the rule that must not break -----------------------------------------------------

def test_a_day_outside_the_fetched_window_is_never_dropped():
    """The reason this script exists. GitHub stops reporting a day after 14 days; the stored
    copy is then the ONLY copy."""
    stored = {"2026-08-01": {"count": 99, "uniques": 40}}
    ts.merge_days(stored, [_bucket("2026-08-25", 5, 3)], "views")
    assert stored["2026-08-01"] == {"count": 99, "uniques": 40}
    assert stored["2026-08-25"] == {"count": 5, "uniques": 3}


def test_merging_an_empty_window_keeps_everything():
    """A quiet fortnight returns no buckets. That must not read as 'delete the history'."""
    stored = {"2026-08-01": {"count": 99, "uniques": 40}}
    added, updated = ts.merge_days(stored, [], "views")
    assert stored == {"2026-08-01": {"count": 99, "uniques": 40}}
    assert (added, updated) == (0, 0)


def test_a_partial_day_is_revised_upward_by_a_later_run():
    """Today's numbers grow during the day, so a later read of the same date wins."""
    stored = {"2026-08-25": {"count": 2, "uniques": 1}}
    added, updated = ts.merge_days(stored, [_bucket("2026-08-25", 17, 9)], "views")
    assert stored["2026-08-25"] == {"count": 17, "uniques": 9}
    assert (added, updated) == (0, 1)


def test_an_unchanged_day_counts_as_neither_added_nor_revised():
    stored = {"2026-08-25": {"count": 17, "uniques": 9}}
    added, updated = ts.merge_days(stored, [_bucket("2026-08-25", 17, 9)], "views")
    assert (added, updated) == (0, 0)


def test_new_days_are_reported_as_added():
    stored = {}
    added, updated = ts.merge_days(stored, [_bucket("2026-08-24", 1, 1),
                                            _bucket("2026-08-25", 2, 2)], "clones")
    assert (added, updated) == (2, 0)
    assert sorted(stored) == ["2026-08-24", "2026-08-25"]


def test_history_survives_many_runs_with_a_sliding_window():
    """Simulate 30 daily runs where the API only ever offers a 14-day tail."""
    all_days = [f"2026-08-{d:02d}" for d in range(1, 31)]
    stored = {}
    for i, _ in enumerate(all_days):
        window = [_bucket(d, 1, 1) for d in all_days[max(0, i - 13):i + 1]]
        ts.merge_days(stored, window, "views")
    assert sorted(stored) == all_days, "days that aged out of the window were lost"


# --- persistence ----------------------------------------------------------------------

def test_load_returns_an_empty_shape_when_there_is_no_file(tmp_path):
    data = ts.load(str(tmp_path / "nope.json"))
    assert data["views"] == {} and data["clones"] == {}
    assert data["repo"] is None


def test_a_corrupt_history_is_refused_rather_than_overwritten(tmp_path):
    """Silently starting over would destroy exactly what cannot be re-fetched."""
    p = tmp_path / "history.json"
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        ts.load(str(p))
    assert "Refusing to overwrite" in str(exc.value)


def test_save_then_load_round_trips(tmp_path):
    p = str(tmp_path / "sub" / "history.json")
    data = ts.load(p)
    data["repo"] = "owner/repo"
    data["views"]["2026-08-25"] = {"count": 3, "uniques": 2}
    ts.save(p, data)
    assert ts.load(p)["views"]["2026-08-25"] == {"count": 3, "uniques": 2}
    assert json.loads(Path(p).read_text(encoding="utf-8"))["repo"] == "owner/repo"


def test_save_leaves_no_temp_file_behind(tmp_path):
    p = str(tmp_path / "history.json")
    ts.save(p, ts.load(p))
    assert [f.name for f in tmp_path.iterdir()] == ["history.json"]


def test_a_history_missing_newer_keys_is_upgraded_not_rejected(tmp_path):
    """An older file predates the releases/totals series. It must still load."""
    p = tmp_path / "history.json"
    p.write_text(json.dumps({"repo": "o/r", "views": {"2026-08-01": {"count": 1, "uniques": 1}}}),
                 encoding="utf-8")
    data = ts.load(str(p))
    assert data["views"]["2026-08-01"]["count"] == 1
    assert data["releases"] == {} and data["totals"] == {}


# --- the report -----------------------------------------------------------------------

def test_report_on_an_empty_history_does_not_crash():
    text = ts.report(ts.load("/nonexistent/history.json"))
    assert "nothing recorded yet" in text


def test_report_totals_the_whole_history_not_just_the_window():
    data = ts.load("/nonexistent/history.json")
    data["repo"] = "o/r"
    data["views"] = {f"2026-08-{d:02d}": {"count": 10, "uniques": 4} for d in range(1, 21)}
    text = ts.report(data)
    assert "200 total over 20 recorded days" in text


def test_the_report_never_calls_a_sum_of_daily_uniques_a_visitor_count():
    """Summing per-day uniques over-counts: GitHub dedupes WITHIN a day, so one cloner
    returning on four days contributes four. The live repo showed 52 clones / 1 unique over
    the window while the daily buckets summed to 4 -- quoting that 4 as "unique" would be
    wrong in the direction that flatters the number."""
    data = ts.load("/nonexistent/history.json")
    data["repo"] = "o/r"
    data["clones"] = {"2026-08-17": {"count": 4, "uniques": 1},
                      "2026-08-18": {"count": 8, "uniques": 1},
                      "2026-08-20": {"count": 28, "uniques": 1},
                      "2026-08-24": {"count": 12, "uniques": 1}}
    text = ts.report(data)
    assert "52 total over 4 recorded days" in text
    assert "NOT a distinct-visitor count" in text
    assert "4 unique" not in text
