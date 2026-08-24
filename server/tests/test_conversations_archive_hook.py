"""The proxy's archive hook: it must record everything, and must never break a resolve."""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import conversation_archive as archive  # noqa: E402
from stt_proxy import conversations  # noqa: E402


def _rows():
    return [{"start": "2026-08-24 12:10:55", "end": "2026-08-24 12:11:23",
             "channel": "160,650", "vessel": "CAPEWATER", "mmsi": "246346000",
             "confidence": "high", "turns": []}]


def test_resolved_rows_reach_the_archive(tmp_path, monkeypatch):
    db = tmp_path / "a.db"
    monkeypatch.setattr(conversations, "CONVERSATIONS_DB", db)
    conversations._archive_rows(_rows())
    with archive.open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1


def test_the_archive_keeps_records_the_rolling_window_has_dropped(tmp_path, monkeypatch):
    """The whole point: conversations.json keeps 300, the archive keeps everything."""
    db = tmp_path / "a.db"
    monkeypatch.setattr(conversations, "CONVERSATIONS_DB", db)
    for minute in range(5):
        conversations._archive_rows(
            [{"start": f"2026-08-24 12:{minute:02d}:00", "end": "2026-08-24 12:11:23",
              "channel": "160,650", "turns": []}])
    with archive.open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 5


def test_an_archive_failure_does_not_raise_into_the_resolve_path(tmp_path, monkeypatch, capsys):
    """Archiving must never be able to stop transcription. Same posture as
    _save_conversations, which has swallowed its own failures since it was written."""
    monkeypatch.setattr(conversations, "CONVERSATIONS_DB", tmp_path / "a.db")

    def explode(_path):
        raise OSError("disk gone")

    monkeypatch.setattr(archive, "connect", explode)
    conversations._archive_rows(_rows())        # must not raise
    assert "could not archive" in capsys.readouterr().out


def test_store_resolved_archives_what_it_stores(tmp_path, monkeypatch):
    """The hook is wired into _store_resolved, not merely present in the module."""
    db = tmp_path / "a.db"
    monkeypatch.setattr(conversations, "CONVERSATIONS_DB", db)
    monkeypatch.setattr(conversations, "CONVERSATIONS_FILE", str(tmp_path / "conversations.json"))
    monkeypatch.setattr(conversations, "_resolved", [])

    import datetime
    window = [{"id": 1, "channel": "160,650", "text": "Maas Approach, Cape Water",
               "raw": "Maas Approach, Cape Water",
               "time": datetime.datetime(2026, 8, 24, 12, 10, 55)}]
    exchanges = [{"chunk_ids": [1], "vessel": "CAPEWATER", "mmsi": "246346000",
                  "confidence": "high", "evidence": "self-identified"}]
    conversations._store_resolved(window, exchanges)

    with archive.open_db(db) as conn:
        row = conn.execute("SELECT vessel FROM conversations").fetchone()
    assert row["vessel"] == "CAPEWATER"
