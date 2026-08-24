# Conversation archive and comments — design

**Status:** agreed 2026-08-24 through the brainstorming skill, every question answered by the
user. Ready for an implementation plan.

## What this is for

> "Comments should hold 2 purposes. 1 for myself, to document what the real vesselname was when
> wrongly identified/unidentified and corrections to misheard communication. 2 for measurement
> purposes to check if the project can be improved. For the first purpose, comments for the last
> 300 conversations (as displayed in the conversations in the webapp) should be editable. For the
> second purpose, both all conversation and comments should be stored."

Two purposes, one feature — see *The insight* below for why they turn out to be the same thing.

## The problem this also fixes

**Conversation history is being destroyed, today, silently.** This was discovered while designing
the feature and is the reason the design got heavier than "add a note field".

`stt_proxy/conversations.py` truncates in both directions:

```python
# _load_conversations, line 929
_resolved[:] = json.load(fh)[-CONVERSATIONS_KEEP:]

# _save_conversations, line 939
data = list(_resolved[-CONVERSATIONS_KEEP:])
with open(CONVERSATIONS_FILE, "w", encoding="utf-8") as fh:   # "w" — full rewrite
    json.dump(data, fh, indent=1)
```

`_save_conversations()` is called from `_store_resolved` (line 1004), so **every resolve rewrites
the whole file with the newest 300 records and drops the 301st from disk.** `CONVERSATIONS_KEEP`
defaults to 300 and is not exposed in the control panel; it is read once at import from the
environment.

Proof it has already happened:

```
stt_proxy/conversations.json                      300 records   2026-08-13 19:04 → 2026-08-24 12:25
stt_proxy/conversations.json.bak-20260818-121753  300 records   2026-08-07 10:40 → 2026-08-14 23:52
```

Everything between 08-07 10:40 and 08-13 19:04 exists **only** in that backup. Coverage back to
08-07 survives purely because someone took a backup on 08-18 and the two ranges happen to overlap.

A trap this creates: **the archive cannot be repaired by concatenating files back together.** The
load path truncates too, so the proxy would read a merged file, keep the last 300, and write the
truncated version straight back on the next resolve.

## The insight

`bench_identify.parse_labels` already reads exactly what the user wants to write:

```
<start>  TAB  <end>  TAB  <vessel name | MMSI | ->  TAB  <note>
```

Field 3 is "who it really was, `-` if nobody" — purpose 1. Field 3 is also precisely what the
identification benchmark scores — purpose 2. **A comment with a verdict *is* a label line.** The
measurement export is a query, not a translation, and the panel becomes the tool that replaces
hand-editing `identification-labels-*.txt` by ear.

## Sizing, measured

Across the live file plus the backup — 500 unique conversations over 13 active days:

| | |
|---|---|
| median | **33 conversations/day** |
| busiest day | **77** (2026-08-20) |
| record size | ~3.3 KB |
| projected | **12k–28k/year, 40–90 MB/year** |

A small dataset. This rules out anything elaborate, and it is why SQLite is comfortable rather
than ambitious.

## Decisions

| question | decision |
|---|---|
| archive store | SQLite, `sqlite3` from the stdlib — no new dependency on 3.10/3.12 CI or 3.14 local |
| archive owner | the proxy writes conversations; the panel writes comments |
| panel ↔ archive | the panel opens the DB **file** directly; no new proxy HTTP endpoints |
| `conversations.json` | unchanged, including the 300 cap and the existing 15 s snapshot fetch |
| comment shape | free-text note always, optional `truth` verdict |
| transcript corrections | free text inside the note. **No per-turn table** — see *Rejected* |
| history browsing in the panel | out of scope; the panel still shows the live 300 |
| shared code | `server/conversation_archive.py`, top level, following `ship_types.py` |

---

## Section 1 — what a comment holds

One row per conversation:

| column | meaning |
|---|---|
| `conversation_id` | PK. `start\|channel` — the key `conversations_view.conversation_id()` already computes |
| `truth` | `NULL` = not reviewed · `'-'` = nobody identifiable · otherwise the real vessel name or MMSI |
| `note` | free text, always available, empty by default |
| `created_at` / `updated_at` | ISO timestamps |

`truth` uses the benchmark's own vocabulary, so a reviewed row exports to a label line with no
translation. `start` and `end` come from the conversation itself.

**`NULL` truth and `'-'` truth must stay distinct.** `NULL` means "not reviewed"; `-` is a real
answer asserting that naming anyone at all is wrong. Collapsing them would silently turn every
conversation nobody looked at into an assertion, and inflate the benchmark corpus with junk.

### `truth` should store MMSI where it can

The labels format accepts a name *or* an MMSI, but names are not safe:
`[[label-scoring-artifacts]]` records that a name shared by two or more MMSIs resolves
arbitrarily, worth roughly seven precision points, and `bench_identify` warns in its own comments
that field 3 has to be spelled the way AIS spells it — which is exactly what a name heard over VHF
is not.

So the input searches the AIS cache as the user types (`vessels_view.search`, already supporting
`?` and `*` wildcards at 5–10 ms over 6,469 entries), and picking a vessel stores its **MMSI**.
Free text stays allowed: dark vessels never appear in the cache, and `-` must always be typeable.

---

## Section 2 — storage and ownership

### What does not change

`conversations.json`, its 300 cap, and the panel's existing HTTP snapshot path stay exactly as
they are. The live Conversations screen keeps working as it does today. If the archive write
fails, live operation is untouched.

### Where the file lives

A new `CONVERSATIONS_DB` setting following `CONVERSATIONS_FILE` exactly — `SettingType.PATH`,
default `""` meaning `server/stt_proxy/conversations.db` beside the JSON, group `Paths`, with the
same "set it to move the data off the install directory before a host migration" rationale. Both
processes resolve it the same way.

`.gitignore` needs `conversations.db*`. Line 40 covers `conversations*.json` but nothing covers
the database, and WAL mode puts three files on disk (`.db`, `.db-wal`, `.db-shm`).

### Who writes what

| | writes | reads |
|---|---|---|
| **proxy** | `conversations` — one `INSERT OR IGNORE` beside the existing `_save_conversations()` call at line 1004. Never deletes. | nothing |
| **panel** | `comments` only | both tables |

SQLite's write lock is database-level, not per-table, so separate tables buy nothing on their
own — `PRAGMA journal_mode=WAL` is what actually lets one process write while the other reads,
and a 5 s `busy_timeout` covers the remaining physical overlap where two writes land at once. At
33 conversations/day the proxy writes roughly once every few minutes and the panel writes only
on save, so that overlap is rare enough for the timeout to absorb without a caller ever seeing
"database is locked".

The proxy's insert takes the same posture as `_save_conversations`: wrapped, logs on failure,
never raises into the resolve path. **Archiving must not be able to break transcription.**

### Why the panel reads the file rather than the proxy

Adding `/api/archive` endpoints would push every history query through the loopback path that
`[[loopback-tcp-tail-loss]]` documents as intermittently dropping the tail of bulk transfers — and
the archive is the one collection that grows without bound. The existing full-list fetch is
already 997 KB every 15 s and must not grow. Reading the file directly also means no change to the
proxy's hand-rolled `do_GET`.

### Schema

```sql
CREATE TABLE conversations (
  id         TEXT PRIMARY KEY,          -- start|channel
  start      TEXT NOT NULL,
  "end"      TEXT,
  channel    TEXT,
  vessel     TEXT,
  mmsi       TEXT,
  confidence TEXT,
  record     TEXT NOT NULL              -- the full record, verbatim JSON
);
CREATE INDEX conversations_start ON conversations(start);

CREATE TABLE comments (
  conversation_id TEXT PRIMARY KEY,
  truth           TEXT,                 -- NULL unreviewed · '-' nobody · else name or MMSI
  note            TEXT NOT NULL DEFAULT '',
  created_at      TEXT NOT NULL,
  updated_at      TEXT NOT NULL
);
```

**`record` holds the whole JSON, with only queried fields extracted into columns.** The record
shape has changed before — `type_code` arrived on 2026-08-20 and is `None` on every older record.
A column per field means a schema migration every time the proxy learns something new, and
silently dropping any field nobody remembered to add. Verbatim storage means the archive cannot
lose data the proxy chose to record.

**No foreign key from `comments` to `conversations`.** It would impose a write-ordering constraint
between two independent processes for no benefit — the archiver always sees a conversation before
anyone can comment on it, because the comment is written from what the panel displayed.

---

## Section 3 — the panel side

### Endpoints

The `api()` helper in `app.js` already attaches `X-CSRF-Token` to every non-GET, so a mutation is
one line of JS on the existing `mutating` router with session and CSRF enforced.

```
GET  /api/conversations            unchanged shape, each row gains  has_comment, truth
GET  /api/conversations/{id}       unchanged shape, gains  comment: {truth, note, updated_at}
POST /api/comments                 {conversation_id, truth, note}  →  upsert
GET  /api/labels[?day=YYYY-MM-DD]  text/plain, the bench_identify ground-truth file
```

**The list rows still come from the 15 s proxy snapshot.** `has_comment` and `truth` are joined on
afterwards with one `WHERE conversation_id IN (…)` over the page's ids — not a query per row, and
not a change to how conversations reach the panel.

**`/api/labels` exports only rows whose `truth` is not `NULL`**, sorted by `start`, taking `start`
and `end` from the archived conversation. Unreviewed rows are absent rather than blank: a label
file is a set of assertions, and a line with an empty field 3 is a parse error in
`bench_identify`, not an abstention. `?day=` narrows to one capture day for scoring a single day
in isolation, which is how the existing corpora are organised.

**`POST /api/comments`, not `POST /api/conversations/{id}/comment`.** The detail route is
`@guarded.get("/api/conversations/{conversation_id:path}")`, and `:path` is greedy — it would
match `.../comment` and hand back `conversation_id="…/comment"`. Rather than depend on
route-registration order staying correct forever, the id goes in the body. It is an opaque string
containing a space and a pipe (`2026-08-24 12:10:55|160,650`), which belongs in a body regardless.

**Saving an empty note with no truth deletes the row.** An empty comment is not a comment, and
blank rows would put meaningless `has_comment` markers on the list.

### UI

In the **conversation detail** view only. A block below the turns:

- **Real vessel** — search-as-you-type against the AIS cache, showing name + MMSI; accepts free
  text and `-`
- **Note** — a textarea
- **Save**, with the last-updated time beside it

In the **list**, a plain marker on commented rows — text, not a link, not a control. The row is a
button that opens the detail, and `[[control-panel-phase3]]`'s hard-won rule is that nothing
clickable goes inside it.

---

## Section 4 — migration and testing

### Backfill

600 records across two files, **500 unique**, giving unbroken coverage from 2026-08-07 10:40 to
now. A CLI — `py conversation_archive.py --import <file>...` — inserts with `INSERT OR IGNORE`,
newest file first.

Stored records are immutable once written (`_store_resolved` only appends; `_update_buffer_entry`
edits the buffer before storage), so ignore-on-conflict cannot lose a newer version of anything.
The import is idempotent: re-running imports nothing and changes nothing, which matters because
another backup may surface later.

### First run

`ensure_schema()` is `CREATE TABLE IF NOT EXISTS` plus setting WAL once, and **both processes call
it on startup**. The panel can be started before the proxy has ever run; a comment UI that fails
because the file does not exist yet would be a silly failure. Whoever arrives first creates it.

### Testing

**A third `conftest.py` guard, and it is not optional.** The hazard from THE INCIDENT
(`[[control-panel-phase1]]`) is exactly reproduced here: a test that builds an app without writing
a config gets every default, and `CONVERSATIONS_DB`'s default is the operator's real archive. A
route test posting a comment would write into actual ground truth. So `tests/conftest.py` gains an
autouse fixture refusing to open the real database, alongside the supervisor and captures guards —
the reach made impossible rather than remembered.

| test | what it pins |
|---|---|
| **export round-trip** | a saved comment → `/api/labels` → **`bench_identify.parse_labels()` accepts it**. It raises on malformed lines, so this asserts the export is consumable, not that it looks right |
| `truth` semantics | `NULL` unreviewed, `'-'` nobody, name/MMSI otherwise; `NULL` never exports a label line |
| backfill | 600 records in, 500 rows out; second run is a no-op |
| comment lifecycle | upsert, update, and empty-note-deletes-the-row |
| route guards | the comment POST is on `mutating`, so the existing mutating-route enumeration test covers session + CSRF automatically |
| proxy never breaks | make the insert fail; assert `_store_resolved` completes and logs |
| concurrency | two WAL connections, one inserting conversations, one upserting comments, no `database is locked` |

No change to `requirements.txt`.

---

## Rejected, with reasons

**Per-turn transcript corrections.** The WER corpus is keyed by **clip**, not turn:
`references-YYYY-MM-DD.txt` is `<4-digit id> TAB <transcript>`, one file per capture day. And
`webapp/clips.py` states in its own docstring that a turn's `HH:MM:SS`, the plugin's capture
`index`, and the proxy's `chunk_ids` are three independent numbering schemes reconciled only by a
±2 s wall-clock window (`TOLERANCE_S = 2.0`). Per-turn corrections would have to reverse-join
through that window to become usable, and a join that picks the neighbouring clip does not error —
it writes a reference for the wrong audio. WER scored against a wrong reference is worse than no
number, and `[[label-scoring-artifacts]]` records this project already losing ~7 precision points
to exactly this class of bug.

If the panel should serve WER later, the right build is a **clip review screen** keyed by capture
day and clip id, exporting `references-*.txt` with no join at all. The panel already serves the
audio (`/api/clips/{day}/{clip}`). That is a separate feature with a separate design. Capture is
running again as of 2026-08-24, so a fresh corpus is accumulating.

**Reserving an empty `turn_corrections` table.** Reserving the wrong table.

**Raising `CONVERSATIONS_KEEP` instead.** `proxy_data.py` fetches the *entire* conversation list
every 15 s — already 997 KB. At 10,000 records that is ~33 MB every 15 s over the loopback path
`[[loopback-tcp-tail-loss]]` documents as intermittently dropping bulk-transfer tails. It makes a
known fault worse and does not fix the problem permanently.

**Daily JSONL archive files.** Viable and rejected only on ergonomics: every cross-day question
becomes a glob-and-load, and joining comments to conversations would be hand-rolled.

**Panel-side archiver (proxy untouched).** Attractive for its zero risk to the live audio path,
but history would only be as complete as the panel's uptime — the proxy runs without the panel,
and anything resolved while the panel is down and aged out before it returned would be lost
silently. An unreliable measurement corpus is the same failure as `distinct_7d`.

## Out of scope

- Browsing or searching the archive in the panel. The live 300 stays the view.
- Any change to `conversations.json`, `CONVERSATIONS_KEEP`, or the 15 s snapshot fetch.
- WER / per-turn / per-clip corrections.
- Exposing `CONVERSATIONS_KEEP` as a panel setting. Worth doing, unrelated to this.

## Success criteria

1. Every resolved conversation lands in the archive and is never deleted, verified by a record
   count that only rises across a proxy restart.
2. The 500 records currently on disk are imported, and re-importing is a no-op.
3. A comment can be created, edited and deleted from the conversation detail view.
4. `GET /api/labels` output is accepted by `bench_identify.parse_labels()` without error.
5. Killing the archive write leaves transcription and the live Conversations screen working.
6. The suite cannot touch the real database, proved by the guard firing on a deliberate attempt.
