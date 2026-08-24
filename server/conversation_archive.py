"""The durable conversation archive, shared by the proxy and the control panel.

Top level rather than inside stt_proxy/ or webapp/ for the same reason ship_types.py is: two
processes need it, and neither package may import the other. Every line of SQL in the project
lives here.

Why this exists at all: stt_proxy/conversations.py rewrites conversations.json whole on every
resolve, keeping only the newest CONVERSATIONS_KEEP=300 records, and truncates again on load.
History was being destroyed -- everything before 2026-08-13 survives only in a backup someone
happened to take. This archive is append-only and never deletes.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime
import json
import os
import sqlite3
import sys
from collections.abc import Iterable
from pathlib import Path

# Beside conversations.json, so that moving the data off the install directory is a copy of one
# directory -- the same argument CONVERSATIONS_FILE's description makes.
DEFAULT_DB_NAME = "conversations.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
  id         TEXT PRIMARY KEY,
  start      TEXT NOT NULL,
  "end"      TEXT,
  channel    TEXT,
  vessel     TEXT,
  mmsi       TEXT,
  confidence TEXT,
  record     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS conversations_start ON conversations(start);

CREATE TABLE IF NOT EXISTS comments (
  conversation_id TEXT PRIMARY KEY,
  truth           TEXT,
  note            TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
"""


def conversation_id(record: dict) -> str:
    """Start instant plus channel. The store has no id of its own and index position shifts as
    conversations are added, so this is the only stable handle on a record."""
    return f"{record.get('start') or ''}|{record.get('channel') or ''}"


def default_db_path(server_dir) -> Path:
    return Path(server_dir) / "stt_proxy" / DEFAULT_DB_NAME


def resolve_db_path(configured: str | None, server_dir) -> Path:
    """The configured path, or the default beside conversations.json when it is empty."""
    text = (configured or "").strip()
    return Path(os.path.normpath(text)) if text else default_db_path(server_dir)


def connect(path) -> sqlite3.Connection:
    """The single place the archive file is opened.

    Everything goes through here so that one monkeypatch in tests/conftest.py can make reaching
    the operator's real database impossible -- the same posture the Supervisor and captures
    guards take, and for the same reason.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    # WAL: the proxy and the panel are separate processes writing this one file, and SQLite's
    # write lock is database-level -- their writing separate tables buys nothing on its own.
    # WAL is what actually lets a reader (or the other writer) proceed while one process holds
    # the write lock, and busy_timeout covers the rare moment both try to write at once: at
    # roughly 33 conversations/day from the proxy and comments saved only on click from the
    # panel, real contention is close to nil.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextlib.contextmanager
def open_db(path):
    """A schema-ensured connection that closes itself.

    ensure_schema on every open rather than once at startup: the panel can be started before
    the proxy has ever run, and a comment UI that fails because the file does not exist yet
    would be a silly failure. CREATE TABLE IF NOT EXISTS makes the race harmless.
    """
    conn = connect(path)
    try:
        ensure_schema(conn)
        yield conn
    finally:
        conn.close()


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def insert_conversation(conn: sqlite3.Connection, record: dict) -> bool:
    """Archive one conversation. Returns whether it was new.

    INSERT OR IGNORE rather than REPLACE: stored records are immutable once written --
    _store_resolved only ever appends, and _update_buffer_entry edits the buffer before storage
    -- so a conflict means the same conversation, not a newer version of it.
    """
    cursor = conn.execute(
        'INSERT OR IGNORE INTO conversations '
        '(id, start, "end", channel, vessel, mmsi, confidence, record) '
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (conversation_id(record), record.get("start") or "", record.get("end"),
         record.get("channel"), record.get("vessel"), record.get("mmsi"),
         record.get("confidence"),
         json.dumps(record, ensure_ascii=False)))
    conn.commit()
    return cursor.rowcount > 0


def insert_many(conn: sqlite3.Connection, records: Iterable[dict]) -> int:
    return sum(1 for record in records if insert_conversation(conn, record))


def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _row_to_comment(row) -> dict:
    return {"conversation_id": row["conversation_id"], "truth": row["truth"],
            "note": row["note"], "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


def get_comment(conn: sqlite3.Connection, conversation_id: str) -> dict | None:
    row = conn.execute("SELECT * FROM comments WHERE conversation_id = ?",
                       (conversation_id,)).fetchone()
    return _row_to_comment(row) if row else None


def _sanitise_newlines(text: str) -> str:
    """Collapse \\r\\n, \\r and \\n to a single space.

    Used on `truth` at write time (upsert_comment) and on both fields at export time
    (labels_text) -- see each call site for why those are different cases. NOT used on `note`
    at write time: the panel's note field is a multi-line textarea and newlines there are a
    wanted part of the input, not an accident to be silently flattened on save.
    """
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def upsert_comment(conn: sqlite3.Connection, conversation_id: str, truth: str | None,
                   note: str, now: str | None = None) -> dict | None:
    """Store a comment, or delete it when it has become empty.

    An empty note with no verdict is not a comment: leaving the row would put a
    "has a comment" marker on a list row that says nothing. Deleting is therefore the correct
    response to clearing both fields, and is how the UI removes one.

    `truth` is sanitised of newlines here, at the source: a verdict is a bare tab-separated
    field in the labels_text export, so a raw newline in it would split one logical row across
    two physical lines and make bench_identify.parse_labels raise on the WHOLE file -- not a
    shape a "vessel name, MMSI, or -" should ever take, so it's rejected before it is stored
    rather than merely cleaned up on the way out. `note` is deliberately NOT sanitised here:
    the panel's note field is a multi-line textarea with no Enter-key interception, so a
    multi-line note is an intended input the operator typed on purpose, and get_comment must
    hand it back exactly as entered. Only labels_text -- where the one-line-per-label
    constraint actually applies -- collapses a note's newlines, and only in the export.
    """
    truth = _sanitise_newlines((truth or "").strip()) or None
    note = (note or "").strip()
    if truth is None and not note:
        conn.execute("DELETE FROM comments WHERE conversation_id = ?", (conversation_id,))
        conn.commit()
        return None

    stamp = now or _now()
    conn.execute(
        "INSERT INTO comments (conversation_id, truth, note, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(conversation_id) DO UPDATE SET "
        "  truth = excluded.truth, note = excluded.note, updated_at = excluded.updated_at",
        (conversation_id, truth, note, stamp, stamp))
    conn.commit()
    return get_comment(conn, conversation_id)


def comments_for(conn: sqlite3.Connection, ids: Iterable[str]) -> dict[str, dict]:
    """Every comment for a page of conversation ids, in one query.

    One query rather than one per row: the list is polled every few seconds, and a page is up
    to 200 rows.
    """
    wanted = list(ids)
    if not wanted:
        return {}
    marks = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT * FROM comments WHERE conversation_id IN ({marks})", wanted).fetchall()
    return {row["conversation_id"]: _row_to_comment(row) for row in rows}


_LABELS_HEADER = """\
# Identification ground truth, exported from the control panel's conversation comments.
#
# Format: <start>\t<end>\t<vessel name, MMSI, or - >\t<note>
#
# Only REVIEWED conversations appear. A conversation with a note but no verdict is not a
# label: an empty field 3 is a parse error in bench_identify, not an abstention.
# '-' is a real answer -- it asserts that naming anyone at all would be wrong.
"""


def labels_text(conn: sqlite3.Connection, day: str | None = None) -> str:
    """The bench_identify ground-truth file for every reviewed conversation.

    start and end come from the archived conversation rather than the comment, so a label can
    never disagree with the record it describes.
    """
    sql = ('SELECT c.start AS start, c."end" AS end, m.truth AS truth, m.note AS note '
           "FROM comments m JOIN conversations c ON c.id = m.conversation_id "
           "WHERE m.truth IS NOT NULL "
           # Rows with a missing end timestamp cannot emit a valid label line: parse_labels
           # requires two consecutive timestamp tokens and would reject "<start>\t\t<truth>".
           # Rather than guess a zero-length window (which would corrupt scoring), we skip such
           # rows entirely. Currently unreachable from _store_resolved (which always sets end),
           # but reachable by import in the next task's backfill, and the schema permits NULL.
           "AND c.\"end\" IS NOT NULL AND c.\"end\" != '' ")
    params: list[str] = []
    if day:
        sql += "AND c.start LIKE ? "
        params.append(f"{day}%")
    sql += "ORDER BY c.start"

    lines = [_LABELS_HEADER]
    for row in conn.execute(sql, params):
        # A tab ends the vessel and begins the note, so a row with no note must not emit a
        # trailing tab -- parse_labels would read the empty remainder as the note, which is
        # harmless, but a file people hand-edit should not carry invisible whitespace.
        #
        # Both fields are sanitised here, on the way out. For `truth` this is belt-and-braces
        # -- upsert_comment already sanitises it on the way in, so this only matters for a row
        # written before that fix existed, or written directly against the database. For
        # `note` this is the ONLY sanitisation point: a multi-line note is a wanted input in
        # the panel's textarea and upsert_comment deliberately stores it verbatim, so the
        # one-line-per-label constraint is enforced here, at export, and nowhere earlier.
        # parse_labels joins rows with \n and matches one line at a time, so an internal
        # newline in EITHER field would otherwise emit a stray unparseable line and raise on
        # the whole file, not just this row.
        fields = [row["start"], row["end"], _sanitise_newlines(row["truth"])]
        if row["note"]:
            fields.append(_sanitise_newlines(row["note"]))
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"


def import_files(db_path, json_paths) -> tuple[int, int]:
    """Backfill the archive from conversations.json files. Returns (records read, inserted).

    Idempotent by construction: insert_conversation ignores an id already present, so running
    this again after another backup surfaces adds only what is genuinely new.
    """
    read = 0
    inserted = 0
    with open_db(db_path) as conn:
        for path in json_paths:
            try:
                records = json.loads(Path(path).read_text(encoding="utf-8"))
            except Exception as exc:
                raise ValueError(f"{path}: could not be read as JSON: {exc}") from exc
            if not isinstance(records, list):
                raise ValueError(f"{path}: expected a list of conversations, "
                                 f"got {type(records).__name__}")
            read += len(records)
            inserted += insert_many(conn, records)
    return read, inserted


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="", help="archive path (default: beside conversations.json)")
    parser.add_argument("--import", dest="sources", nargs="+", required=True,
                        metavar="FILE", help="conversations.json files to backfill from")
    args = parser.parse_args(argv)

    server_dir = Path(__file__).resolve().parent
    db_path = resolve_db_path(args.db, server_dir)
    read, inserted = import_files(db_path, args.sources)
    print(f"{db_path}: read {read}, inserted {inserted}, already present {read - inserted}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
