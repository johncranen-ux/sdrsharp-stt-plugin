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

import contextlib
import json
import os
import sqlite3
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
    # WAL: the proxy and the panel are separate processes writing separate tables in this one
    # file. WAL lets a reader run while a writer holds the write lock.
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
