# Conversation Archive and Comments Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop conversation history being destroyed by the 300-record rolling window, and let the operator record — from the control panel — who a vessel really was and a free-text note, in a form the identification benchmark can consume directly.

**Architecture:** A SQLite archive at `server/stt_proxy/conversations.db`, written by the proxy (conversations, never deleted) and by the panel (comments). `conversations.json` and its 300-record cap are left completely untouched, so the live Conversations screen and the 15 s snapshot fetch keep working exactly as they do today. The panel opens the database file directly rather than adding proxy HTTP endpoints. One shared module, `server/conversation_archive.py`, holds every line of SQL and is imported by both processes — the same top-level-shared-module pattern as `server/ship_types.py`.

**Tech Stack:** Python 3.10+ (`sqlite3` from the stdlib — no new dependency), FastAPI for the panel routes, vanilla JS for the UI.

**Spec:** `docs/superpowers/specs/2026-08-24-conversation-archive-and-comments-design.md`

## Global Constraints

- **No new runtime dependency.** `requirements.txt` must not change. `sqlite3` is stdlib on the 3.10 and 3.12 that CI runs and on the local 3.14.
- **`conversations.json` is not modified.** Not its format, not `CONVERSATIONS_KEEP=300`, not `_save_conversations`, not the 15 s `proxy_data` fetch. Any task that changes them has gone wrong.
- **Archiving must never break transcription.** Every archive call from `stt_proxy/` is wrapped, logs on failure, and returns normally.
- **`truth` semantics are three-valued and must stay so.** `NULL` = not reviewed · `'-'` = nobody identifiable · any other string = the real vessel name or MMSI. `NULL` and `'-'` must never be collapsed.
- **Database path default:** `server/stt_proxy/conversations.db`, overridable by the `CONVERSATIONS_DB` environment variable / setting. Empty string means the default.
- **Every new mutating route goes on the `mutating` router** in `webapp/app.py`, never on `guarded`, so the existing enumeration test covers its session and CSRF guards.
- **Existing test suite is 1170 tests and green.** Run `cd server && py -m pytest -q` after every task; the count only goes up.

---

### Task 1: The archive module — connection, schema, conversation insert

**Files:**
- Create: `server/conversation_archive.py`
- Create: `server/tests/test_conversation_archive.py`
- Modify: `server/webapp/conversations_view.py:22-25` (delegate `conversation_id` to the shared module)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `conversation_id(record: dict) -> str` — `f"{start}|{channel}"`
  - `connect(path) -> sqlite3.Connection` — the single choke point that opens the file; sets WAL and `busy_timeout`
  - `open_db(path)` — context manager yielding a schema-ensured connection, closes on exit
  - `ensure_schema(conn: sqlite3.Connection) -> None`
  - `insert_conversation(conn, record: dict) -> bool` — True if a row was inserted, False if the id was already present
  - `insert_many(conn, records: Iterable[dict]) -> int` — count actually inserted

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_conversation_archive.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_conversation_archive.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'conversation_archive'`

- [ ] **Step 3: Write minimal implementation**

Create `server/conversation_archive.py`:

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && py -m pytest tests/test_conversation_archive.py -q`
Expected: PASS, 6 tests

- [ ] **Step 5: Point `conversations_view.conversation_id` at the shared definition**

The id is now computed in two places, and the AIS ship-type table is the standing reminder of what that costs. Replace the body in `server/webapp/conversations_view.py` (lines 22-25) with a delegation, keeping the name importable exactly as it is today:

```python
import conversation_archive


def conversation_id(record: dict) -> str:
    """Stable within one proxy run. Start instant plus channel: the store has no id of its own,
    and index position shifts as conversations are added.

    Defined in conversation_archive so the proxy (which cannot import webapp) and this module
    cannot drift apart -- the archive's primary key and this screen's row id are the same fact.
    """
    return conversation_archive.conversation_id(record)
```

- [ ] **Step 6: Run the whole suite to prove nothing regressed**

Run: `cd server && py -m pytest -q`
Expected: PASS, previous count + 6

- [ ] **Step 7: Commit**

```bash
git add server/conversation_archive.py server/tests/test_conversation_archive.py server/webapp/conversations_view.py
git commit -m "Add the conversation archive's schema and conversation insert"
```

---

### Task 2: Comments — upsert, read, and delete-when-empty

**Files:**
- Modify: `server/conversation_archive.py`
- Modify: `server/tests/test_conversation_archive.py`

**Interfaces:**
- Consumes: `open_db`, `ensure_schema` from Task 1.
- Produces:
  - `upsert_comment(conn, conversation_id: str, truth: str | None, note: str, now: str | None = None) -> dict | None` — returns the stored comment, or `None` when it deleted the row
  - `get_comment(conn, conversation_id: str) -> dict | None`
  - `comments_for(conn, ids: Iterable[str]) -> dict[str, dict]` — one query for a page of rows

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_conversation_archive.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_conversation_archive.py -q`
Expected: FAIL — `AttributeError: module 'conversation_archive' has no attribute 'upsert_comment'`

- [ ] **Step 3: Write minimal implementation**

Append to `server/conversation_archive.py` (add `import datetime` to the imports at the top):

```python
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


def upsert_comment(conn: sqlite3.Connection, conversation_id: str, truth: str | None,
                   note: str, now: str | None = None) -> dict | None:
    """Store a comment, or delete it when it has become empty.

    An empty note with no verdict is not a comment: leaving the row would put a
    "has a comment" marker on a list row that says nothing. Deleting is therefore the correct
    response to clearing both fields, and is how the UI removes one.
    """
    truth = (truth or "").strip() or None
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && py -m pytest tests/test_conversation_archive.py -q`
Expected: PASS, 14 tests

- [ ] **Step 5: Commit**

```bash
git add server/conversation_archive.py server/tests/test_conversation_archive.py
git commit -m "Store, edit and clear a conversation comment"
```

---

### Task 3: The labels export

**Files:**
- Modify: `server/conversation_archive.py`
- Modify: `server/tests/test_conversation_archive.py`

**Interfaces:**
- Consumes: `open_db`, `insert_conversation`, `upsert_comment` from Tasks 1-2.
- Produces: `labels_text(conn, day: str | None = None) -> str` — the complete `bench_identify` ground-truth file, header comment included.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_conversation_archive.py`. The round-trip test is the important one: `bench_identify.parse_labels` raises on a malformed line, so parsing our own output is a real assertion that the export is consumable, not that it merely looks right.

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_conversation_archive.py -q`
Expected: FAIL — `AttributeError: module 'conversation_archive' has no attribute 'labels_text'`

- [ ] **Step 3: Write minimal implementation**

Append to `server/conversation_archive.py`:

```python
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
           "WHERE m.truth IS NOT NULL ")
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
        fields = [row["start"], row["end"] or "", row["truth"]]
        if row["note"]:
            fields.append(row["note"])
        lines.append("\t".join(fields))
    return "\n".join(lines) + "\n"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && py -m pytest tests/test_conversation_archive.py -q`
Expected: PASS, 18 tests

- [ ] **Step 5: Commit**

```bash
git add server/conversation_archive.py server/tests/test_conversation_archive.py
git commit -m "Export reviewed comments as a bench_identify ground-truth file"
```

---

### Task 4: The backfill CLI

**Files:**
- Modify: `server/conversation_archive.py`
- Modify: `server/tests/test_conversation_archive.py`

**Interfaces:**
- Consumes: `open_db`, `insert_many` from Task 1.
- Produces: `import_files(db_path, json_paths: list) -> tuple[int, int]` returning `(read, inserted)`, and `main(argv=None) -> int` behind `if __name__ == "__main__"`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_conversation_archive.py`:

```python
import json as _json


def _write_json(path, records):
    path.write_text(_json.dumps(records), encoding="utf-8")
    return path


def test_import_reads_a_conversations_file(tmp_path):
    source = _write_json(tmp_path / "conversations.json",
                         [_record(), _record(start="2026-08-24 13:00:00")])
    read, inserted = archive.import_files(tmp_path / "a.db", [source])
    assert (read, inserted) == (2, 2)


def test_importing_twice_changes_nothing(tmp_path):
    """The operator will run this again when another backup surfaces. It must be safe."""
    source = _write_json(tmp_path / "conversations.json", [_record()])
    archive.import_files(tmp_path / "a.db", [source])
    read, inserted = archive.import_files(tmp_path / "a.db", [source])
    assert (read, inserted) == (1, 0)


def test_overlapping_files_are_deduplicated(tmp_path):
    """The live file and the backup overlap; the union is what matters, not the sum."""
    shared = _record(start="2026-08-14 23:52:58")
    live = _write_json(tmp_path / "live.json", [shared, _record()])
    backup = _write_json(tmp_path / "backup.json", [shared, _record(start="2026-08-07 10:40:14")])
    read, inserted = archive.import_files(tmp_path / "a.db", [live, backup])
    assert read == 4
    assert inserted == 3


def test_import_reports_a_file_it_cannot_read(tmp_path):
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    with __import__("pytest").raises(ValueError, match="broken.json"):
        archive.import_files(tmp_path / "a.db", [tmp_path / "broken.json"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_conversation_archive.py -q`
Expected: FAIL — `AttributeError: module 'conversation_archive' has no attribute 'import_files'`

- [ ] **Step 3: Write minimal implementation**

Append to `server/conversation_archive.py` (add `import argparse` and `import sys` to the imports):

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && py -m pytest tests/test_conversation_archive.py -q`
Expected: PASS, 22 tests

- [ ] **Step 5: Commit**

```bash
git add server/conversation_archive.py server/tests/test_conversation_archive.py
git commit -m "Backfill the archive from existing conversations.json files"
```

---

### Task 5: The proxy archives every resolved conversation

**Files:**
- Modify: `server/stt_proxy/conversations.py` (imports, the path constant near line 921, and `_store_resolved`'s tail at line 1004)
- Create: `server/tests/test_conversations_archive_hook.py`

**Interfaces:**
- Consumes: `open_db`, `insert_many` from Task 1.
- Produces: `_archive_rows(rows: list[dict]) -> None` in `stt_proxy/conversations.py`, and the module constant `CONVERSATIONS_DB`.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_conversations_archive_hook.py`:

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_conversations_archive_hook.py -q`
Expected: FAIL — `AttributeError: module 'stt_proxy.conversations' has no attribute '_archive_rows'`

- [ ] **Step 3: Write minimal implementation**

In `server/stt_proxy/conversations.py`, add to the imports at the top of the file:

```python
import conversation_archive
```

Next to `CONVERSATIONS_KEEP` (after line 921), add the path constant:

```python
# The durable archive, which is NOT truncated. CONVERSATIONS_KEEP above governs only the
# rolling in-memory window and the JSON file the panel polls; everything ever resolved goes
# here and stays.
CONVERSATIONS_DB = os.path.normpath(
    os.environ.get("CONVERSATIONS_DB", "").strip()
    or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    conversation_archive.DEFAULT_DB_NAME))
```

Add the hook function just above `_store_resolved`:

```python
def _archive_rows(rows: list[dict]) -> None:
    """Copy resolved conversations into the durable archive.

    Wrapped exactly like _save_conversations: a failure here is logged and swallowed. The
    archive is a record-keeping convenience; live transcription is the job, and no amount of
    broken disk may stop it.
    """
    try:
        with conversation_archive.open_db(CONVERSATIONS_DB) as conn:
            conversation_archive.insert_many(conn, rows)
    except Exception as exc:
        print(f"[conv] could not archive to {CONVERSATIONS_DB}: {exc}", flush=True)
```

Then in `_store_resolved`, change the tail (line 1000-1004) from:

```python
    with _resolved_lock:
        _resolved.extend(rows)
        del _resolved[:-CONVERSATIONS_KEEP]
    _save_conversations()
```

to:

```python
    with _resolved_lock:
        _resolved.extend(rows)
        del _resolved[:-CONVERSATIONS_KEEP]
    _save_conversations()
    # After the rolling store, not before: the archive is the backstop, and a conversation
    # must never be archived that the live path failed to record.
    _archive_rows(rows)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && py -m pytest tests/test_conversations_archive_hook.py -q`
Expected: PASS, 4 tests

- [ ] **Step 5: Run the whole suite**

Run: `cd server && py -m pytest -q`
Expected: PASS, previous count + 4

- [ ] **Step 6: Commit**

```bash
git add server/stt_proxy/conversations.py server/tests/test_conversations_archive_hook.py
git commit -m "Archive every resolved conversation where the rolling window cannot reach it"
```

---

### Task 6: The conftest guard

**Files:**
- Modify: `server/tests/conftest.py`
- Create: `server/tests/test_archive_guard.py`

**Interfaces:**
- Consumes: `conversation_archive.connect` from Task 1.
- Produces: an autouse fixture `_never_open_the_real_archive`.

This task must land before Task 7. Task 7's tests build a control-panel app, and an app built without a written config gets every setting's default — which for `CONVERSATIONS_DB` is the operator's real archive. THE INCIDENT of 2026-08-18 is exactly this shape: a test that named a config it never wrote reached the real supervisor and killed a live capture. The lesson recorded there was that a test suite able to reach production data will eventually use it, so the reach itself is what gets removed.

- [ ] **Step 1: Write the failing test**

Create `server/tests/test_archive_guard.py`:

```python
"""The guard that stops a test writing into the operator's real ground truth."""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import conversation_archive as archive  # noqa: E402


def test_opening_the_real_archive_is_refused():
    real = archive.default_db_path(_SERVER_DIR)
    with pytest.raises(AssertionError, match="the real one"):
        archive.connect(real)


def test_a_temporary_archive_is_allowed(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    conn.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_archive_guard.py -q`
Expected: FAIL on the first test — no `AssertionError` is raised, and a `conversations.db` may appear in `server/stt_proxy/`. Delete it if it does: `rm -f server/stt_proxy/conversations.db*`

- [ ] **Step 3: Write minimal implementation**

Append to `server/tests/conftest.py`:

```python
@pytest.fixture(autouse=True)
def _never_open_the_real_archive(monkeypatch):
    """Refuse to open the operator's real conversation archive.

    Same reasoning as the two guards above. CONVERSATIONS_DB's default is a real path, and a
    test that builds an app over a config file which does not exist gets every setting's
    default -- so a route test posting a comment would write a verdict into the ground truth
    the identification benchmark is scored against. Unlike a killed process, that corruption
    would be silent and might not be noticed for weeks.
    """
    import conversation_archive

    real = conversation_archive.default_db_path(_SERVER_DIR).resolve()
    original = conversation_archive.connect

    def guarded_connect(path):
        if Path(path).resolve() == real:
            raise AssertionError(
                f"this test opened {real} -- the real one, holding the operator's conversation "
                f"archive and every ground-truth verdict recorded in the panel. Give the app a "
                f"config whose CONVERSATIONS_DB is under tmp_path.")
        return original(path)

    monkeypatch.setattr(conversation_archive, "connect", guarded_connect)
```

Note `Path(path).resolve()` on a path that does not exist is fine on Windows and POSIX for Python 3.6+; it does not require the file to be present.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && py -m pytest tests/test_archive_guard.py -q`
Expected: PASS, 2 tests

- [ ] **Step 5: Run the whole suite and confirm the guard catches nothing yet**

Run: `cd server && py -m pytest -q`
Expected: PASS. If any existing test now fails with "the real one", that test was already reaching production data and must be given a `tmp_path` database — do not weaken the guard.

- [ ] **Step 6: Commit**

```bash
git add server/tests/conftest.py server/tests/test_archive_guard.py
git commit -m "Refuse to let a test open the real conversation archive"
```

---

### Task 7: The setting, the gitignore, and comments on the read routes

**Files:**
- Modify: `server/webapp/settings_schema.py:232` (add `CONVERSATIONS_DB` beside `CONVERSATIONS_FILE`)
- Modify: `.gitignore:40`
- Modify: `server/webapp/app.py` (a `_archive_db()` helper; enrich `read_conversations` and `read_conversation`)
- Modify: `server/tests/test_app_routes.py`
- Modify: `server/tests/test_catalogue_defaults.py` if it asserts a setting count

**Interfaces:**
- Consumes: `resolve_db_path`, `open_db`, `comments_for`, `get_comment` from Tasks 1-2.
- Produces: `_archive_db()` inside `create_app`, returning a `Path`; list rows gain `has_comment: bool` and `truth: str | None`; detail gains `comment: dict | None`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_app_routes.py`:

```python
def test_a_list_row_reports_whether_it_has_a_comment(client, tmp_path):
    import conversation_archive as archive
    with archive.open_db(tmp_path / "conversations.db") as conn:
        archive.upsert_comment(conn, "2026-08-19T10:15:00+00:00|16", "246346000", "clear")

    rows = client.get("/api/conversations").json()["rows"]
    commented = [r for r in rows if r["has_comment"]]
    assert len(commented) == 1
    assert commented[0]["truth"] == "246346000"
    assert all(r["truth"] is None for r in rows if not r["has_comment"])


def test_the_detail_carries_the_whole_comment(client, tmp_path):
    import conversation_archive as archive
    with archive.open_db(tmp_path / "conversations.db") as conn:
        archive.upsert_comment(conn, "2026-08-19T10:15:00+00:00|16", "-", "too much static")

    body = client.get("/api/conversations/2026-08-19T10:15:00%2B00:00%7C16").json()
    assert body["comment"]["truth"] == "-"
    assert body["comment"]["note"] == "too much static"


def test_a_conversation_with_no_comment_says_so_rather_than_omitting_the_key(client):
    body = client.get("/api/conversations/2026-08-19T10:15:00%2B00:00%7C16").json()
    assert body["comment"] is None
```

The client fixture must now write a config naming a `tmp_path` database. In `_build_app` (line ~109), before `create_app`, add:

```python
    import json
    (tmp_path / "config.json").write_text(
        json.dumps({"CONVERSATIONS_DB": str(tmp_path / "conversations.db"),
                    "LOG_DIR": str(tmp_path / "logs")}), encoding="utf-8")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_app_routes.py -q`
Expected: FAIL — `KeyError: 'has_comment'`

- [ ] **Step 3: Add the setting and the gitignore line**

In `server/webapp/settings_schema.py`, immediately after the `CONVERSATIONS_FILE` spec:

```python
    SettingSpec(key="CONVERSATIONS_DB", type=SettingType.PATH, default="", group="Paths",
                description="The durable conversation archive, which is never truncated. Empty "
                            "means server/stt_proxy/conversations.db, beside conversations.json. "
                            "Unlike that file -- which keeps only the newest 300 -- this holds "
                            "every conversation ever resolved, and the comments recorded "
                            "against them."),
```

In `.gitignore`, after line 40 (`conversations*.json`):

```
# The archive and its WAL sidecars. Same reason as conversations*.json above: received radio
# traffic is user data (NL Telecommunicatiewet 18.13) and never belongs in the repo.
conversations.db*
```

- [ ] **Step 4: Wire the archive into the read routes**

In `server/webapp/app.py`, add `import conversation_archive` to the imports, then add this helper inside `create_app` next to `_captures_root` (around line 183):

```python
    def _archive_db():
        """Where the conversation archive lives, resolved per request.

        Per request rather than held, for the same reason _captures_root is: the path is a
        setting, and the Settings screen can change it while the panel is running.
        """
        return conversation_archive.resolve_db_path(
            values().get("CONVERSATIONS_DB"), server_dir)
```

Replace `read_conversations` (line 175) with:

```python
    @guarded.get("/api/conversations")
    def read_conversations(identified: bool | None = None, channel: str | None = None,
                           text: str | None = None, limit: int = 50, offset: int = 0) -> dict:
        records, snap = data.conversations()
        page = conversations_view.query(
            records, identified=identified, channel=channel, text=text,
            limit=limit, offset=offset)
        # The rows still come from the proxy's 15s snapshot; the comments are joined on here in
        # ONE query over the page's ids, never one per row -- this list is polled.
        with conversation_archive.open_db(_archive_db()) as conn:
            found = conversation_archive.comments_for(conn, [row["id"] for row in page.rows])
        for row in page.rows:
            comment = found.get(row["id"])
            row["has_comment"] = comment is not None
            row["truth"] = comment["truth"] if comment else None
        return _envelope(page, snap)
```

And in `read_conversation` (line 195), immediately before `return found`:

```python
                with conversation_archive.open_db(_archive_db()) as conn:
                    found["comment"] = conversation_archive.get_comment(
                        conn, conversation_id)
                return found
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && py -m pytest tests/test_app_routes.py tests/test_catalogue_defaults.py -q`
Expected: PASS. If `test_catalogue_defaults.py` asserts a fixed number of settings, raise it by one.

- [ ] **Step 6: Run the whole suite**

Run: `cd server && py -m pytest -q`
Expected: PASS, previous count + 3

- [ ] **Step 7: Commit**

```bash
git add server/webapp/settings_schema.py server/webapp/app.py server/tests/test_app_routes.py server/tests/test_catalogue_defaults.py .gitignore
git commit -m "Carry a conversation's comment onto the list and the detail"
```

---

### Task 8: POST /api/comments

**Files:**
- Modify: `server/webapp/app.py` (the `mutating` router, after the settings POST at line 254)
- Modify: `server/tests/test_app_routes.py`

**Interfaces:**
- Consumes: `_archive_db` from Task 7, `upsert_comment` from Task 2.
- Produces: `POST /api/comments` taking `{conversation_id, truth, note}` and returning `{"comment": dict | None}`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_app_routes.py`:

```python
def test_saving_a_comment_stores_it(client):
    body = client.post("/api/comments", json={
        "conversation_id": "2026-08-19T10:15:00+00:00|16",
        "truth": "246346000", "note": "heard clearly"}).json()
    assert body["comment"]["truth"] == "246346000"

    detail = client.get("/api/conversations/2026-08-19T10:15:00%2B00:00%7C16").json()
    assert detail["comment"]["note"] == "heard clearly"


def test_clearing_both_fields_removes_the_comment(client):
    client.post("/api/comments", json={"conversation_id": "2026-08-19T10:15:00+00:00|16",
                                       "truth": "246346000", "note": "x"})
    body = client.post("/api/comments", json={"conversation_id": "2026-08-19T10:15:00+00:00|16",
                                              "truth": "", "note": ""}).json()
    assert body["comment"] is None

    rows = client.get("/api/conversations").json()["rows"]
    assert all(not r["has_comment"] for r in rows)


def test_a_note_with_no_verdict_is_allowed(client):
    """Purpose 1 is documentation. A note must not require deciding who the vessel was."""
    body = client.post("/api/comments", json={
        "conversation_id": "2026-08-19T10:15:00+00:00|16",
        "truth": "", "note": "could not tell, too much static"}).json()
    assert body["comment"]["truth"] is None
    assert body["comment"]["note"] == "could not tell, too much static"


def test_posting_a_comment_unauthenticated_is_rejected(unauthenticated_client):
    assert unauthenticated_client.post(
        "/api/comments", json={"conversation_id": "x", "truth": "", "note": "y"}).status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_app_routes.py -q`
Expected: FAIL — 404 on `/api/comments`

- [ ] **Step 3: Write minimal implementation**

In `server/webapp/app.py`, add the request model beside the other pydantic models near the top of the file:

```python
class CommentIn(BaseModel):
    conversation_id: str
    truth: str = ""
    note: str = ""
```

Then add the route to the `mutating` router, after the settings POST:

```python
    # On `mutating`, not `guarded`, so the enumeration test covers its session and CSRF guards.
    #
    # POST /api/comments rather than POST /api/conversations/{id}/comment: the detail route is
    # registered with a `{conversation_id:path}` converter, which is greedy and would match
    # ".../comment" too. Putting the id in the body removes that dependence on route ordering,
    # and the id -- which contains a space and a pipe -- belongs in a body regardless.
    @mutating.post("/api/comments")
    def write_comment(body: CommentIn) -> dict:
        with conversation_archive.open_db(_archive_db()) as conn:
            stored = conversation_archive.upsert_comment(
                conn, body.conversation_id, body.truth, body.note)
        return {"comment": stored}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && py -m pytest tests/test_app_routes.py -q`
Expected: PASS, 4 new tests

- [ ] **Step 5: Commit**

```bash
git add server/webapp/app.py server/tests/test_app_routes.py
git commit -m "Save a conversation comment from the panel"
```

---

### Task 9: GET /api/labels

**Files:**
- Modify: `server/webapp/app.py` (the `guarded` router)
- Modify: `server/tests/test_app_routes.py`

**Interfaces:**
- Consumes: `_archive_db` from Task 7, `labels_text` from Task 3.
- Produces: `GET /api/labels?day=YYYY-MM-DD` returning `text/plain`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/test_app_routes.py`:

```python
def test_the_labels_export_is_plain_text_and_parses(client, tmp_path):
    import conversation_archive as archive
    with archive.open_db(tmp_path / "conversations.db") as conn:
        archive.insert_conversation(conn, {
            "start": "2026-08-19 10:15:00", "end": "2026-08-19 10:16:30",
            "channel": "16", "vessel": "PASHA BULKER", "mmsi": "244123456"})
        archive.upsert_comment(conn, "2026-08-19 10:15:00|16", "244123456", "clear")

    response = client.get("/api/labels")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")

    import bench_identify
    out = tmp_path / "labels.txt"
    out.write_text(response.text, encoding="utf-8")
    assert len(bench_identify.parse_labels(out)) == 1


def test_the_labels_export_can_be_narrowed_to_one_day(client, tmp_path):
    import conversation_archive as archive
    with archive.open_db(tmp_path / "conversations.db") as conn:
        for day in ("2026-08-19", "2026-08-20"):
            archive.insert_conversation(conn, {
                "start": f"{day} 10:15:00", "end": f"{day} 10:16:30", "channel": "16"})
            archive.upsert_comment(conn, f"{day} 10:15:00|16", "244123456", "")

    text = client.get("/api/labels?day=2026-08-20").text
    lines = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(lines) == 1
    assert lines[0].startswith("2026-08-20")


def test_the_labels_export_rejects_an_unauthenticated_request(unauthenticated_client):
    assert unauthenticated_client.get("/api/labels").status_code == 401
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && py -m pytest tests/test_app_routes.py -q`
Expected: FAIL — 404 on `/api/labels`

- [ ] **Step 3: Write minimal implementation**

Add `from fastapi.responses import PlainTextResponse` to the imports in `server/webapp/app.py`, then add to the `guarded` router:

```python
    @guarded.get("/api/labels", response_class=PlainTextResponse)
    def read_labels(day: str | None = None) -> str:
        """The identification ground truth, in the format bench_identify.parse_labels reads.

        Text rather than JSON because its consumer is a file on disk that a human also hand-
        edits; a JSON round trip would only be in the way.
        """
        with conversation_archive.open_db(_archive_db()) as conn:
            return conversation_archive.labels_text(conn, day=day)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && py -m pytest tests/test_app_routes.py -q`
Expected: PASS, 3 new tests

- [ ] **Step 5: Run the whole suite**

Run: `cd server && py -m pytest -q`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add server/webapp/app.py server/tests/test_app_routes.py
git commit -m "Serve the identification ground truth as a downloadable label file"
```

---

### Task 10: The comment editor in the conversation detail

**Files:**
- Modify: `server/webapp/static/app.js` (`renderConvDetail` at line 939; `buildConvRow` at line 624; `updateConvRow` at line 641)
- Modify: `server/webapp/static/index.html` (the conversations `<thead>`, line ~126)
- Modify: `server/webapp/static/app.css`

The editor itself needs no `index.html` change — `renderConvDetail` builds everything inside `#conv-detail-body` in JS, and this follows it. The list marker does, because it is a table column.

**Interfaces:**
- Consumes: `POST /api/comments` (Task 8), `GET /api/vessels?text=` (existing), the `api()` helper, and the existing `element()` / `setText()` helpers.
- Produces: `renderCommentEditor(detail)` returning an element; a `conv-row-comment` marker span on list rows.

- [ ] **Step 1: Add the comment editor to the detail view**

Insert into `server/webapp/static/app.js` immediately before `renderConvDetail`:

```javascript
/* The comment editor.
 *
 * Two fields, because the operator asked for two things: what the vessel really was when the
 * resolver got it wrong, and a free note. The verdict is optional -- a note that says "could
 * not tell" is a real contribution and must not require naming anyone.
 *
 * The vessel field searches the AIS cache and stores the picked vessel's MMSI rather than its
 * name. Names are not safe as ground truth: a name shared by two MMSIs resolves arbitrarily,
 * which cost this project about seven precision points before anyone noticed. Free text stays
 * allowed for dark vessels, which are never in the cache, and for "-".
 */
function renderCommentEditor(detail) {
  const wrap = element("div", "comment-editor");
  wrap.append(element("h3", null, "Comment"));

  const comment = detail.comment || {};

  const truthRow = element("label", "comment-field");
  truthRow.append(element("span", "comment-label", "Real vessel"));
  const truth = document.createElement("input");
  truth.type = "text";
  truth.className = "comment-input";
  truth.placeholder = "name, MMSI, or - for nobody";
  truth.value = comment.truth || "";
  truthRow.append(truth);
  wrap.append(truthRow);

  const matches = element("ul", "comment-matches");
  wrap.append(matches);

  const noteRow = element("label", "comment-field");
  noteRow.append(element("span", "comment-label", "Note"));
  const note = document.createElement("textarea");
  note.className = "comment-input comment-note";
  note.rows = 3;
  note.value = comment.note || "";
  noteRow.append(note);
  wrap.append(noteRow);

  const actions = element("div", "comment-actions");
  const save = element("button", "button", "Save");
  save.type = "button";
  const status = element("span", "comment-status",
    comment.updated_at ? `saved ${comment.updated_at}` : "");
  actions.append(save, status);
  wrap.append(actions);

  // Debounced, because this fires per keystroke over a 6,469-entry cache. 250ms is the same
  // feel as the Vessels screen's search.
  let searchTimer = null;
  truth.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    const needle = truth.value.trim();
    if (needle.length < 2 || needle === "-") { matches.replaceChildren(); return; }
    searchTimer = window.setTimeout(async () => {
      try {
        const body = await api(`/api/vessels?text=${encodeURIComponent(needle)}&limit=6`);
        matches.replaceChildren();
        for (const row of body.rows || []) {
          const item = element("li", "comment-match");
          const pick = element("button", "comment-pick", `${row.name || "—"} · ${row.mmsi}`);
          pick.type = "button";
          // Store the MMSI, show the name. The MMSI is the unambiguous half.
          pick.addEventListener("click", () => {
            truth.value = row.mmsi;
            matches.replaceChildren();
          });
          item.append(pick);
          matches.append(item);
        }
      } catch (error) {
        matches.replaceChildren(element("li", "comment-match", `search failed: ${error.message}`));
      }
    }, 250);
  });

  save.addEventListener("click", async () => {
    save.disabled = true;
    setText(status, "saving…");
    try {
      const body = await api("/api/comments", {
        method: "POST",
        body: JSON.stringify({ conversation_id: detail.id, truth: truth.value, note: note.value }),
      });
      detail.comment = body.comment;
      setText(status, body.comment ? `saved ${body.comment.updated_at}` : "cleared");
      // Keep the list marker honest without waiting for the next poll.
      const row = convState.rows.get(detail.id);
      if (row) {
        row.has_comment = body.comment !== null;
        row.truth = body.comment ? body.comment.truth : null;
      }
    } catch (error) {
      setText(status, `could not save: ${error.message}`);
    } finally {
      save.disabled = false;
    }
  });

  return wrap;
}
```

Then, at the end of `renderConvDetail` (after the suggestions block, line ~987), append:

```javascript
  body.append(renderCommentEditor(detail));
```

- [ ] **Step 2: Add the list marker as a proper column**

Rows are built once by `buildConvRow` and mutated by `updateConvRow` — rebuilding the DOM on a poll destroys scroll position, which is why the code is shaped this way. So the marker is a **cell**, set on update; appending an element in the builder or the render path would fight that design.

Three edits, all small.

`server/webapp/static/index.html`, the conversations `<thead>` (line ~126) — add a column:

```html
            <th>Start</th><th>Channel</th><th>Vessel</th><th>Type</th>
            <th>Destination</th><th>Confidence</th><th>Turns</th><th>Candidates</th><th>Review</th>
```

`server/webapp/static/app.js`, in `buildConvRow` (line ~627), add `"review"` to the key list:

```javascript
  for (const key of ["start", "channel", "vessel", "type", "destination",
                      "confidence", "turns", "candidates", "review"]) {
```

`server/webapp/static/app.js`, at the end of `updateConvRow` (line ~658), set it:

```javascript
  // Plain text, never a control. This row is a button that opens the detail, and putting
  // anything clickable inside it is the mistake the VesselFinder links made -- the natural
  // click landed on the inner element every time, and the operator reported it.
  // "reviewed" means a verdict was recorded; "note" means a note with no verdict, which is a
  // real and different state -- it says someone looked and could not tell.
  setText(view.cells.review,
    row.has_comment ? (row.truth ? "reviewed" : "note") : "—");
```

- [ ] **Step 3: Add the styles**

Append to `server/webapp/static/app.css`:

```css
.comment-editor { margin-top: 1.5rem; }
.comment-field { display: block; margin-bottom: .75rem; }
.comment-label { display: block; margin-bottom: .25rem; font-size: .85rem; opacity: .8; }
.comment-input { width: 100%; box-sizing: border-box; }
.comment-note { resize: vertical; }
.comment-matches { list-style: none; margin: 0 0 .75rem; padding: 0; }
.comment-match { padding: .1rem 0; font-size: .9rem; }
/* There is no existing link-shaped button class in this stylesheet -- checked. */
.comment-pick { background: none; border: 0; padding: 0; font: inherit; cursor: pointer;
                color: inherit; text-decoration: underline; }
.comment-actions { display: flex; align-items: center; gap: .75rem; }
.comment-status { font-size: .85rem; opacity: .75; }
```

- [ ] **Step 4: Verify by hand in the running panel**

Static files are served with `Cache-Control: no-cache` by `RevalidatingStatic`, so **a browser reload is enough — no panel restart, and no signing out.**

1. Reload the panel at `http://localhost:8787`
2. Open the Conversations tab and click a conversation
3. Type `cape` in Real vessel — the cache matches should appear; click one and confirm the field becomes an MMSI
4. Add a note, press Save, confirm the status line shows a timestamp
5. Reload the page and confirm the comment is still there and the list row shows a marker
6. Clear both fields, Save, confirm the marker disappears

- [ ] **Step 5: Run the whole suite**

Run: `cd server && py -m pytest -q`
Expected: PASS — no test count change; this task is UI.

- [ ] **Step 6: Commit**

```bash
git add server/webapp/static/app.js server/webapp/static/app.css
git commit -m "Record a verdict and a note against a conversation from the panel"
```

---

### Task 11: Backfill the real archive and document it

**Files:**
- Modify: `docs/` — whichever operator document describes running the server (check `docs/` for the user manual; add a short section there)
- No code changes.

**Interfaces:**
- Consumes: the CLI from Task 4.

- [ ] **Step 1: Back up the live data before touching anything**

```bash
cd server
cp stt_proxy/conversations.json "stt_proxy/conversations.json.bak-$(date +%Y%m%d-%H%M%S)"
```

- [ ] **Step 2: Import both existing files, newest first**

`INSERT OR IGNORE` means the first file to supply an id wins, so the newest goes first. The two overlap; the union is what matters.

```bash
cd server
py conversation_archive.py --import \
  stt_proxy/conversations.json \
  stt_proxy/conversations.json.bak-20260818-121753
```

Expected output shape: `…conversations.db: read 600, inserted 500, already present 100`. The exact numbers will have drifted since the plan was written — the proxy keeps resolving — so treat them as a worked example. The assertion that matters is the next step.

- [ ] **Step 3: Prove the import is idempotent**

Run the exact same command again.
Expected: `inserted 0`.

- [ ] **Step 4: Confirm the archive already exceeds the rolling window**

```bash
cd server
py -c "import conversation_archive as a; \
       conn=a.connect('stt_proxy/conversations.db'); \
       print(conn.execute('SELECT count(*), min(start), max(start) FROM conversations').fetchone()[:])"
```
Expected: a count above 300 and a `min(start)` earlier than the oldest record in `conversations.json` — that difference is history the old code would have destroyed.

- [ ] **Step 5: Restart the proxy so it begins archiving**

The proxy reads `CONVERSATIONS_DB` at import. Restart it from the control panel's Dashboard. This drops live radio audio for a few seconds; do it during quiet traffic.

- [ ] **Step 6: Document it**

Add a short section to the operator documentation covering: what the archive is, that it is never truncated, where it lives, how to back it up (copy `conversations.db*` — all three files), and the `--import` command for recovering from an old JSON backup.

- [ ] **Step 7: Commit**

```bash
git add docs
git commit -m "Document the conversation archive and how to back it up"
```

---

## Done when

1. Every resolved conversation lands in the archive and is never deleted — verified by a record count that only rises across a proxy restart.
2. The records currently on disk are imported, and re-importing reports `inserted 0`.
3. A comment can be created, edited and deleted from the conversation detail view.
4. `GET /api/labels` output is accepted by `bench_identify.parse_labels()` without error.
5. Breaking the archive write leaves transcription and the live Conversations screen working.
6. The suite cannot touch the real database, proved by `test_archive_guard.py`.
7. `cd server && py -m pytest -q` is green, `requirements.txt` unchanged.
