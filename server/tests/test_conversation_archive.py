"""The SQLite archive: schema, inserts, and the id that joins it to the live store."""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import conversation_archive as archive  # noqa: E402


def _record(**over):
    record = {
        "start": "2026-08-24 12:10:55", "end": "2026-08-24 12:11:23",
        "channel": "160,650", "vessel": "CAPEWATER", "mmsi": "246346000",
        "confidence": "high", "type_code": 80,
        "turns": [{"time": "12:10:55", "text": "Maas Approach, Cape Water, under way."}],
    }
    record.update(over)
    return record


def test_the_id_is_the_start_and_the_channel():
    assert archive.conversation_id(_record()) == "2026-08-24 12:10:55|160,650"


def test_ensure_schema_is_idempotent(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        archive.ensure_schema(conn)          # a second time, on the same connection
        archive.ensure_schema(conn)
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"conversations", "comments"} <= names


def test_a_conversation_round_trips_with_its_record_verbatim(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        assert archive.insert_conversation(conn, _record()) is True
        row = conn.execute("SELECT id, start, channel, vessel, mmsi, confidence, record "
                           "FROM conversations").fetchone()
    assert row["id"] == "2026-08-24 12:10:55|160,650"
    assert row["start"] == "2026-08-24 12:10:55"
    assert row["channel"] == "160,650"
    assert row["vessel"] == "CAPEWATER"
    assert row["mmsi"] == "246346000"
    assert row["confidence"] == "high"
    # Verbatim: every field the proxy chose to record survives, including ones this schema
    # has no column for. type_code arrived on 2026-08-20 and would have been lost by a
    # column-per-field design.
    import json
    assert json.loads(row["record"])["type_code"] == 80


def test_inserting_the_same_conversation_twice_is_ignored(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        assert archive.insert_conversation(conn, _record()) is True
        assert archive.insert_conversation(conn, _record()) is False
        assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 1


def test_insert_many_counts_only_what_was_new(tmp_path):
    records = [_record(), _record(start="2026-08-24 13:00:00")]
    with archive.open_db(tmp_path / "a.db") as conn:
        assert archive.insert_many(conn, records) == 2
        assert archive.insert_many(conn, records) == 0


def test_wal_is_on_so_two_processes_can_write(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
