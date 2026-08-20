"""Joining a conversation turn to the audio the plugin captured for it.

A turn stores HH:MM:SS and the capture stores a full timestamp per clip id, so the join is by
time within a tolerance -- there is no shared identifier. The proxy's own `chunk_ids` are a
different numbering entirely and cannot be used for this.
"""
import json
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import clips  # noqa: E402


def _capture_day(root: Path, day: str, entries: list[tuple[int, str]]) -> Path:
    """Write a capture directory the way the plugin writes one: WAVs plus a BOM'd index."""
    day_dir = root / day
    day_dir.mkdir(parents=True)
    lines = []
    for index, stamp in entries:
        (day_dir / f"{index:04d}_sent.wav").write_bytes(b"RIFF....WAVEfmt ")
        (day_dir / f"{index:04d}_raw.wav").write_bytes(b"RIFF....WAVEfmt ")
        lines.append(json.dumps({"index": index, "timestamp": stamp}))
    # utf-8-sig: the plugin writes a BOM, and reading it as plain utf-8 raises on line 1.
    (day_dir / "index.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8-sig")
    return day_dir


class TestTurnDay:
    def test_a_turn_takes_the_day_from_the_conversation_start(self):
        assert clips.turn_day("2026-08-14 23:52:58", "23:52:58") == "2026-08-14"

    def test_a_turn_after_midnight_belongs_to_the_next_day(self):
        # A conversation starting at 23:52 can run past midnight, and its later turns were
        # captured into the NEXT dated directory. Without this they resolve to nothing.
        assert clips.turn_day("2026-08-14 23:52:58", "00:01:12") == "2026-08-15"

    def test_a_turn_at_the_same_minute_stays_on_the_start_day(self):
        assert clips.turn_day("2026-08-14 12:00:00", "12:00:00") == "2026-08-14"

    def test_a_malformed_start_resolves_to_nothing_rather_than_guessing(self):
        assert clips.turn_day("", "12:00:00") is None
        assert clips.turn_day("not a date", "12:00:00") is None
        assert clips.turn_day("2026-08-14 12:00:00", "nonsense") is None


class TestResolve:
    def test_finds_the_clip_recorded_at_that_moment(self, tmp_path):
        _capture_day(tmp_path, "2026-08-14", [(0, "2026-08-14T23:52:58"),
                                              (1, "2026-08-14T23:53:40")])
        assert clips.resolve(tmp_path, "2026-08-14", "23:53:40") == "0001"

    def test_matches_within_the_tolerance(self, tmp_path):
        # The turn's stored time and the capture's timestamp are taken at different points in
        # the pipeline, so they are close rather than equal.
        _capture_day(tmp_path, "2026-08-14", [(7, "2026-08-14T10:00:00")])
        assert clips.resolve(tmp_path, "2026-08-14", "10:00:01") == "0007"

    def test_does_not_match_a_clip_seconds_away(self, tmp_path):
        _capture_day(tmp_path, "2026-08-14", [(7, "2026-08-14T10:00:00")])
        assert clips.resolve(tmp_path, "2026-08-14", "10:00:30") is None

    def test_a_day_that_was_never_captured_resolves_to_nothing(self, tmp_path):
        _capture_day(tmp_path, "2026-08-14", [(0, "2026-08-14T10:00:00")])
        assert clips.resolve(tmp_path, "2026-08-09", "10:00:00") is None

    def test_a_missing_captures_root_is_not_an_error(self, tmp_path):
        assert clips.resolve(tmp_path / "nope", "2026-08-14", "10:00:00") is None

    def test_a_clip_in_the_index_whose_file_is_gone_does_not_resolve(self, tmp_path):
        # Capture directories get pruned by hand; the index outlives the audio. Offering a
        # play button for a file that is not there is worse than offering none.
        day = _capture_day(tmp_path, "2026-08-14", [(3, "2026-08-14T10:00:00")])
        (day / "0003_sent.wav").unlink()
        assert clips.resolve(tmp_path, "2026-08-14", "10:00:00") is None


class TestPathSafety:
    def test_builds_the_path_for_a_valid_day_and_clip(self, tmp_path):
        _capture_day(tmp_path, "2026-08-14", [(2, "2026-08-14T10:00:00")])
        found = clips.clip_path(tmp_path, "2026-08-14", "0002")
        assert found is not None and found.name == "0002_sent.wav"

    def test_rejects_a_day_that_is_not_a_date(self, tmp_path):
        for day in ("..", "../..", "2026-08", "2026-8-14", "", "2026-08-14/x"):
            assert clips.clip_path(tmp_path, day, "0002") is None, day

    def test_rejects_a_clip_id_that_is_not_four_digits(self, tmp_path):
        for clip in ("..", "0002_raw", "2", "00002", "", "0002/../../x", "*"):
            assert clips.clip_path(tmp_path, "2026-08-14", clip) is None, clip

    def test_rejects_a_traversal_that_would_escape_the_captures_root(self, tmp_path):
        # Belt and braces with the format checks above: whatever the inputs, the resolved
        # path must stay under the root. This endpoint reads files off disk for a browser.
        assert clips.clip_path(tmp_path, "..%2F..", "0002") is None

    def test_returns_none_when_the_file_does_not_exist(self, tmp_path):
        (tmp_path / "2026-08-14").mkdir()
        assert clips.clip_path(tmp_path, "2026-08-14", "0002") is None


class TestAnnotateTurns:
    def test_marks_each_turn_with_its_clip(self, tmp_path):
        _capture_day(tmp_path, "2026-08-14", [(0, "2026-08-14T23:52:58"),
                                              (1, "2026-08-14T23:53:40")])
        record = {"start": "2026-08-14 23:52:58",
                  "turns": [{"time": "23:52:58"}, {"time": "23:53:40"}]}
        turns = clips.annotate(record["turns"], record["start"], tmp_path)
        assert [t["clip"] for t in turns] == ["0000", "0001"]
        assert [t["clip_day"] for t in turns] == ["2026-08-14", "2026-08-14"]

    def test_a_turn_with_no_capture_is_marked_as_having_none(self, tmp_path):
        _capture_day(tmp_path, "2026-08-14", [(0, "2026-08-14T23:52:58")])
        record = {"start": "2026-08-14 23:52:58",
                  "turns": [{"time": "23:52:58"}, {"time": "23:59:00"}]}
        turns = clips.annotate(record["turns"], record["start"], tmp_path)
        assert turns[0]["clip"] == "0000"
        assert turns[1]["clip"] is None

    def test_the_original_turn_fields_survive(self, tmp_path):
        _capture_day(tmp_path, "2026-08-14", [(0, "2026-08-14T10:00:00")])
        turns = clips.annotate([{"time": "10:00:00", "text": "Maas Approach"}],
                               "2026-08-14 10:00:00", tmp_path)
        assert turns[0]["text"] == "Maas Approach"

    def test_no_captures_directory_configured_leaves_every_turn_unplayable(self):
        turns = clips.annotate([{"time": "10:00:00"}], "2026-08-14 10:00:00", None)
        assert turns[0]["clip"] is None

    def test_one_day_index_is_read_once_for_the_whole_conversation(self, tmp_path, monkeypatch):
        # A conversation is a few dozen turns; re-reading index.jsonl for each would read the
        # same file a few dozen times per detail request.
        _capture_day(tmp_path, "2026-08-14",
                     [(n, f"2026-08-14T10:00:{n:02d}") for n in range(5)])
        import clip_index

        calls = []
        original = clip_index.load_clip_index

        def counting(day_dir):
            calls.append(str(day_dir))
            return original(day_dir)

        monkeypatch.setattr(clip_index, "load_clip_index", counting)
        turns = [{"time": f"10:00:{n:02d}"} for n in range(5)]
        clips.annotate(turns, "2026-08-14 10:00:00", tmp_path)
        assert len(calls) == 1


class TestTimestampSpellings:
    """The store writes "2026-08-14 23:52:58" / "23:52:58", but ISO-8601 also occurs.

    Being strict about one spelling would not fail loudly -- it would silently return no clip
    for every turn, which looks exactly like "capture was off".
    """

    def test_iso_timestamps_with_an_offset_resolve_the_same(self, tmp_path):
        _capture_day(tmp_path, "2026-08-19", [(0, "2026-08-19T10:15:05")])
        turns = clips.annotate([{"time": "2026-08-19T10:15:05+00:00"}],
                               "2026-08-19T10:15:00+00:00", tmp_path)
        assert turns[0]["clip"] == "0000"

    def test_the_plain_stored_spelling_still_resolves(self, tmp_path):
        _capture_day(tmp_path, "2026-08-19", [(0, "2026-08-19T10:15:05")])
        turns = clips.annotate([{"time": "10:15:05"}], "2026-08-19 10:15:00", tmp_path)
        assert turns[0]["clip"] == "0000"

    def test_midnight_rollover_works_for_iso_too(self):
        assert clips.turn_day("2026-08-14T23:52:58+00:00", "2026-08-15T00:01:12+00:00") \
            == "2026-08-15"
