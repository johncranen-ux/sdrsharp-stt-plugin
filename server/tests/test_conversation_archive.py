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


def test_a_comment_is_stored_and_read_back(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        stored = archive.upsert_comment(conn, "id-1", "246346000", "heard clearly",
                                        now="2026-08-24T12:30:00")
        assert stored["truth"] == "246346000"
        assert stored["note"] == "heard clearly"
        assert stored["created_at"] == "2026-08-24T12:30:00"
        assert archive.get_comment(conn, "id-1")["note"] == "heard clearly"


def test_editing_a_comment_keeps_created_at_and_moves_updated_at(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        archive.upsert_comment(conn, "id-1", None, "first", now="2026-08-24T12:00:00")
        stored = archive.upsert_comment(conn, "id-1", "-", "second", now="2026-08-24T13:00:00")
    assert stored["created_at"] == "2026-08-24T12:00:00"
    assert stored["updated_at"] == "2026-08-24T13:00:00"
    assert stored["truth"] == "-"


def test_unreviewed_and_nobody_are_different_answers(tmp_path):
    """NULL means nobody looked; '-' asserts that naming anyone would be wrong. Collapsing
    them would turn every conversation nobody reviewed into an assertion."""
    with archive.open_db(tmp_path / "a.db") as conn:
        archive.upsert_comment(conn, "unreviewed", None, "just a note")
        archive.upsert_comment(conn, "nobody", "-", "")
        assert archive.get_comment(conn, "unreviewed")["truth"] is None
        assert archive.get_comment(conn, "nobody")["truth"] == "-"


def test_saving_an_empty_comment_deletes_the_row(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        archive.upsert_comment(conn, "id-1", None, "something")
        assert archive.upsert_comment(conn, "id-1", None, "   ") is None
        assert archive.get_comment(conn, "id-1") is None


def test_an_empty_comment_that_never_existed_is_not_an_error(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        assert archive.upsert_comment(conn, "never", None, "") is None


def test_comments_for_fetches_a_page_in_one_query(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        archive.upsert_comment(conn, "a", "111", "note a")
        archive.upsert_comment(conn, "b", None, "note b")
        found = archive.comments_for(conn, ["a", "b", "missing"])
    assert set(found) == {"a", "b"}
    assert found["a"]["truth"] == "111"


def test_comments_for_handles_an_empty_page(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        assert archive.comments_for(conn, []) == {}


def _archived(conn, start, end, channel="160,650", **over):
    record = _record(start=start, end=end, channel=channel, **over)
    archive.insert_conversation(conn, record)
    return archive.conversation_id(record)


def test_only_reviewed_rows_export(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        reviewed = _archived(conn, "2026-08-24 12:10:55", "2026-08-24 12:11:23")
        noted = _archived(conn, "2026-08-24 13:00:00", "2026-08-24 13:00:30")
        archive.upsert_comment(conn, reviewed, "246346000", "clear")
        archive.upsert_comment(conn, noted, None, "could not tell")   # note only, no verdict
        text = archive.labels_text(conn)
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert lines == ["2026-08-24 12:10:55\t2026-08-24 12:11:23\t246346000\tclear"]


def test_nobody_identifiable_exports_as_a_dash(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        cid = _archived(conn, "2026-08-24 12:10:55", "2026-08-24 12:11:23")
        archive.upsert_comment(conn, cid, "-", "")
        text = archive.labels_text(conn)
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert lines == ["2026-08-24 12:10:55\t2026-08-24 12:11:23\t-"]


def test_a_day_filter_narrows_to_one_capture_day(tmp_path):
    with archive.open_db(tmp_path / "a.db") as conn:
        first = _archived(conn, "2026-08-23 09:00:00", "2026-08-23 09:00:30")
        second = _archived(conn, "2026-08-24 12:10:55", "2026-08-24 12:11:23")
        archive.upsert_comment(conn, first, "111111111", "")
        archive.upsert_comment(conn, second, "222222222", "")
        text = archive.labels_text(conn, day="2026-08-24")
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert lines == ["2026-08-24 12:10:55\t2026-08-24 12:11:23\t222222222"]


def test_the_export_is_accepted_by_bench_identify(tmp_path):
    """The whole point of the truth encoding. parse_labels raises on a malformed line, so this
    proves the export is consumable rather than merely well shaped.

    MMSI and '-' only, deliberately -- do NOT "improve" this by adding a vessel name.
    bench_identify._resolve_expected raises for a name when called with lookup=None, because
    resolving one needs the AIS cache; bench_identify.py's own main() supplies that, a bare
    parse_labels cannot. This is also the strongest argument for the UI storing the MMSI a
    vessel search returns rather than the name it displays.
    """
    import bench_identify

    with archive.open_db(tmp_path / "a.db") as conn:
        cid = _archived(conn, "2026-08-24 12:10:55", "2026-08-24 12:11:23")
        nobody = _archived(conn, "2026-08-24 13:00:00", "2026-08-24 13:00:30")
        archive.upsert_comment(conn, cid, "246346000", "heard clearly")
        archive.upsert_comment(conn, nobody, "-", "too much static")
        text = archive.labels_text(conn)

    out = tmp_path / "labels.txt"
    out.write_text(text, encoding="utf-8")
    labels = bench_identify.parse_labels(out)

    assert len(labels) == 2
    assert labels[0].note == "heard clearly"


def test_two_connections_write_different_tables_without_locking(tmp_path):
    """The live arrangement: the proxy inserts conversations while the panel upserts comments,
    from two separate processes into one file. WAL is what makes that safe -- without it the
    second writer raises 'database is locked' and the archive silently loses records."""
    db = tmp_path / "a.db"
    with archive.open_db(db) as proxy_conn, archive.open_db(db) as panel_conn:
        for minute in range(20):
            archive.insert_conversation(
                proxy_conn, _record(start=f"2026-08-24 12:{minute:02d}:00"))
            archive.upsert_comment(panel_conn, f"id-{minute}", "246346000", "note")
        assert proxy_conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 20
        assert panel_conn.execute("SELECT count(*) FROM comments").fetchone()[0] == 20
