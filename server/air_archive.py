"""The airband transmission archive: Approach 4 transmissions, their clues, and hand moves.

Top level for the same reason conversation_archive.py is -- the proxy writes it and the panel
reads and writes it, and neither package may import the other. Same database file as the
conversation archive, opened through conversation_archive.connect so the test guard that
makes the operator's real archive unreachable covers these tables too.

See docs/superpowers/specs/2026-09-24-airband-conversations-design.md.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import re
import sqlite3

import conversation_archive

_SCHEMA = """
CREATE TABLE IF NOT EXISTS air_transmissions (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  t             TEXT NOT NULL,
  epoch         REAL NOT NULL,
  channel       TEXT,
  text          TEXT NOT NULL,
  numbers       TEXT NOT NULL,
  callsign_clue TEXT NOT NULL,
  echo_clue     TEXT,
  state         TEXT,
  outcome_kind  TEXT NOT NULL,
  outcome_key   TEXT NOT NULL,
  badge         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS air_transmissions_epoch ON air_transmissions(epoch);

CREATE TABLE IF NOT EXISTS air_moves (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  transmission_id INTEGER NOT NULL,
  from_key        TEXT NOT NULL,
  to_key          TEXT NOT NULL,
  t_moved         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS air_moves_tid ON air_moves(transmission_id);
"""

_KEY = re.compile(r"^(unassigned|review|heard:[A-Z0-9]{1,12}|[0-9A-Fa-f]{6})$")
_JSON_COLS = ("numbers", "callsign_clue", "echo_clue", "state")

# The latest move per transmission, or NULL. MAX(id) rather than MAX(t_moved): two moves in
# the same second must still have a defined winner.
_SELECT = """
SELECT a.*, m.to_key AS moved_to
FROM air_transmissions a
LEFT JOIN air_moves m ON m.id = (
  SELECT MAX(id) FROM air_moves WHERE transmission_id = a.id)
"""


def valid_key(key: str) -> bool:
    return bool(_KEY.match(key or ""))


@contextlib.contextmanager
def open_db(path):
    conn = conversation_archive.connect(path)
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
        yield conn
    finally:
        conn.close()


def _decode(row: sqlite3.Row) -> dict:
    out = dict(row)
    for col in _JSON_COLS:
        out[col] = json.loads(out[col]) if out.get(col) else None
    moved_to = out.pop("moved_to", None)
    out["effective_key"] = moved_to or out["outcome_key"]
    out["moved"] = moved_to is not None
    return out


def insert_transmission(conn: sqlite3.Connection, row: dict) -> int:
    cursor = conn.execute(
        "INSERT INTO air_transmissions (t, epoch, channel, text, numbers, callsign_clue, "
        "state, outcome_kind, outcome_key, badge) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["t"], row["epoch"], row.get("channel"), row["text"],
         json.dumps(row.get("numbers") or []), json.dumps(row["callsign_clue"]),
         json.dumps(row["state"]) if row.get("state") is not None else None,
         row["outcome_kind"], row["outcome_key"], row["badge"]))
    conn.commit()
    return int(cursor.lastrowid)


def set_echo(conn: sqlite3.Connection, tid: int, echo: dict, outcome: dict,
             state: dict | None) -> None:
    conn.execute(
        "UPDATE air_transmissions SET echo_clue = ?, outcome_kind = ?, outcome_key = ?, "
        "badge = ?, state = COALESCE(?, state) WHERE id = ?",
        (json.dumps(echo), outcome["kind"], outcome["key"], outcome["badge"],
         json.dumps(state) if state is not None else None, tid))
    conn.commit()


def pending_echo(conn: sqlite3.Connection, cutoff_epoch: float) -> list[dict]:
    rows = conn.execute(_SELECT + " WHERE a.echo_clue IS NULL AND a.epoch <= ? "
                        "ORDER BY a.epoch", (cutoff_epoch,)).fetchall()
    return [_decode(r) for r in rows]


def transmissions(conn: sqlite3.Connection, start_epoch: float,
                  end_epoch: float) -> list[dict]:
    rows = conn.execute(_SELECT + " WHERE a.epoch >= ? AND a.epoch <= ? ORDER BY a.epoch",
                        (start_epoch, end_epoch)).fetchall()
    return [_decode(r) for r in rows]


def get_transmission(conn: sqlite3.Connection, tid: int) -> dict | None:
    row = conn.execute(_SELECT + " WHERE a.id = ?", (tid,)).fetchone()
    return _decode(row) if row else None


def add_move(conn: sqlite3.Connection, tid: int, to_key: str,
             now: str | None = None) -> dict | None:
    current = get_transmission(conn, tid)
    if current is None:
        return None
    stamp = now or datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    conn.execute("INSERT INTO air_moves (transmission_id, from_key, to_key, t_moved) "
                 "VALUES (?, ?, ?, ?)", (tid, current["effective_key"], to_key, stamp))
    conn.commit()
    return {"transmission_id": tid, "from_key": current["effective_key"],
            "to_key": to_key, "t_moved": stamp}
