"""The proxy's archive hook: it must record everything, and must never break a resolve."""
import json
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


def test_load_conversations_archives_what_it_loads(tmp_path, monkeypatch):
    """The restart gap: everything resolved since the backfill exists only in the truncated
    300-row conversations.json until the proxy restarts. _load_conversations must re-archive
    what it reads, so the archive is self-healing across every future restart -- not just
    fixed once by hand."""
    conv_file = tmp_path / "conversations.json"
    conv_file.write_text(json.dumps(_rows()), encoding="utf-8")
    db = tmp_path / "a.db"
    monkeypatch.setattr(conversations, "CONVERSATIONS_FILE", str(conv_file))
    monkeypatch.setattr(conversations, "CONVERSATIONS_DB", db)
    monkeypatch.setattr(conversations, "_resolved", [])

    conversations._load_conversations()

    with archive.open_db(db) as conn:
        assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1


def test_archive_rows_warns_on_a_genuine_duplicate(tmp_path, monkeypatch, capsys):
    """_store_resolved always hands _archive_rows fresh rows, so a shortfall there means two
    exchanges collided on start|channel -- a silently lost conversation, which must be
    logged rather than swallowed without a trace."""
    monkeypatch.setattr(conversations, "CONVERSATIONS_DB", tmp_path / "a.db")
    row = _rows()[0]
    conversations._archive_rows([row])
    capsys.readouterr()          # discard the first call's output
    conversations._archive_rows([row])   # same start|channel: INSERT OR IGNORE drops it
    out = capsys.readouterr().out
    assert "archive ignored 1 of 1" in out


def test_archive_rows_startup_rearchival_does_not_warn(tmp_path, monkeypatch, capsys):
    """_load_conversations re-archives rows that are usually already present -- that is the
    entire point of fix 2 -- so the same shortfall that is a genuine collision from
    _store_resolved must not print a false alarm here."""
    monkeypatch.setattr(conversations, "CONVERSATIONS_DB", tmp_path / "a.db")
    row = _rows()[0]
    conversations._archive_rows([row])
    capsys.readouterr()
    conversations._archive_rows([row], startup=True)
    out = capsys.readouterr().out
    assert "archive ignored" not in out
