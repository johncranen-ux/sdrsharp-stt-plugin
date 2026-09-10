# Airband flight identification — measurement arm — design

**Date:** 2026-09-10
**Status:** design agreed, not implemented (one throwaway side-car already running, see Rollout)
**Related:** measures the feature specified in
`2026-09-08-airband-flight-identification-design.md`. Mirrors the maritime measurement
tooling (`server/bench_identify.py`, `server/replay_sessions.py`) but scores per
transmission against a live aircraft snapshot rather than per conversation against a
persistent AIS cache.

## The problem

Flight identification has shipped and works, but nobody can say how well. There are nine
`bench_*.py` scripts for the maritime side and zero for flights, so the only way to answer
"what is our recall?" is to hand-diff a plugin transcript against `grep flight-id` in the
proxy log. Every proposed improvement is therefore unfalsifiable.

Three improvements are on the table and all three are blocked on this:

- **A — decoder biasing.** Put the in-range callsigns in the Whisper prompt so the callsign is
  transcribed correctly rather than repaired afterwards.
- **B — session identity register.** Repair a one-digit-off candidate against a callsign
  exact-matched on the same channel moments earlier; resolve abbreviated callsigns
  ("Charlie") only against that register.
- **C — physical corroboration.** Use `alt_baro`/`lat`/`lon` (already cached) to disambiguate
  and to veto implausible matches.

The maritime history is the argument for measuring first. Three separate matching-layer
changes there — hint cutoff, whole-word match, phonetic key — each looked obviously correct
beforehand and each **measured as a null or worse** ([[matching-layer-headroom-exhausted]],
[[hint-cutoff-negative-result]], [[partial-name-matching]]). A fourth, the draught
plausibility check, was validated 8/8 on its sample and then **fired zero times** on real
sea-box data ([[draught-check-negative-result]]). Building A, B, or C blind repeats that.

## What the misses actually are, from real logs

Every airband miss observed so far. Sample sizes are small and stated honestly — this is the
motivation for measuring, not a substitute for it. Buckets are the scorer's (see
Architecture), assigned mechanically by what the current code actually does, not by whether
the aircraft was conceptually reachable.

| Mode | Real example | Bucket |
|---|---|---|
| Digit substitution | `DAL192`/`DAL122` spoken within 75s of `DAL162` exact-matched twice | selection |
| Tail-shape mismatch | `KLM6A` vs cached `KLM66A` (tail match is exact by design) | selection |
| Abbreviated callsign | "ATR, this is Charlie"; "four three five November" (91s after the full form) | extraction (no candidate produced) |
| Unknown telephony word | "Alpine five nine", 5+ sessions, no confident ICAO code | extraction |
| Nothing in range | `DAL5011`, `TRA6084` | retrieval |

Two selection, two extraction, one retrieval — so on this (small) sample the fixable weight is
split evenly between the matcher and extraction, and only one mode is structurally unfixable.
Which of those two dominates in a real hour is exactly what this arm exists to establish.

### A trap the abbreviated-callsign idea must survive

Observed live while writing this spec, 2026-09-10 11:20: *"is one eight Charlie available,
we're very late."* That "Charlie" is **runway 18C**, not an aircraft. Runway designators
(L/C/R spoken as Left/Center/Right, often phonetically) share a vocabulary with abbreviated
callsigns, and Approach traffic is full of them. Any future arm that resolves bare phonetic
letters must exclude letters preceded by a runway number, and must be scored against a corpus
containing cases like this one rather than only against the cases it was designed for.

## The blocker this design exists to remove

`adsb.py`'s cache is a **full REPLACE every 15s and is never persisted**. What was in range
at 10:35:47 is gone forever. `bench_identify.py`'s most valuable mode — re-run the matcher
over the same inputs and score that — works for vessels only because the AIS cache
accumulates and persists on disk. For flights the equivalent is currently impossible **in
principle**, not merely unimplemented. Any counterfactual ("would B have caught `DAL122`?")
is unanswerable until snapshots exist.

## Decisions

1. **Measure before building A, B or C.** None of them is written until the baseline and the
   four-way split below exist.
2. **Snapshot the aircraft cache**, so replay is possible. Side-car process now; per-transmission
   snapshotting inside the proxy later (see Architecture).
3. **Label by ear, not from logs.** Audio capture is on; the worksheet carries the clip
   filename so every row is checkable against what was actually said.
4. **Store full transcription text in the worksheet, never an excerpt.**
   `identification-labels-2026-08-07-verified.txt` stored ~55-character excerpts; when a later
   question needed the full text, zero of its 32 conversations could answer it and the source
   conversations had aged out of `conversations.json`. Truncation is irreversible; full text
   is free.
5. **Report a four-way outcome split, not a blended recall number** (see Architecture). The
   four causes need completely different fixes and a single number hides which one is binding.
6. **Report recall against the achievable ceiling**, with retrieval misses stated separately as
   that ceiling — so no effort is spent chasing recall that is structurally impossible.
7. **No `--repeats` noise-floor machinery.** `bench_identify.py` needs it because the resolver
   is an LLM whose near-ties flip run to run. The flight matcher is pure regex plus
   `rapidfuzz` — deterministic, so a single run per arm is exact.

### Alternatives rejected

- **Score from proxy logs alone, no audio.** Cheap and available today, but the "truth" would
  be whatever the machine transcribed, which is precisely what is under test. The vessel work
  records ear verdicts as strictly stronger than documentary ones; capture is already on, so
  there is no reason to accept the weaker kind.
- **Score without snapshots, accepting no counterfactual.** This yields a baseline and the
  four-way split, but cannot compare arms — which is the entire purpose. Rejected as soon as
  the side-car was shown to remove the constraint cheaply.
- **Snapshot per poll rather than per transmission.** ~5,760 snapshots/day at ~6 KB each is
  ~34 MB/day for data that is only ever read at transmission timestamps. Per-transmission is
  roughly a few MB/day and is exactly aligned with what the scorer joins on.
- **Build B first because it is cheap and obviously safe.** It probably is both. It is also
  what was said about the three maritime changes that measured as nulls.

## Architecture

### 1. Aircraft snapshots

**Now (throwaway, already running):** a standalone side-car polls adsb.fi every 15s and
appends `{"t": iso, "n": count, "aircraft": [...]}` to
`server/logs/adsb-snapshots-<date>.jsonl` (gitignored). It reuses `adsb.build_url`,
`adsb.parse_response` and `adsb.map_aircraft`, so the field shape cannot drift from the live
cache's. It runs as its own process deliberately: the proxy does not hot-reload, and
restarting it would interrupt the capture run these snapshots exist to support.

A failed fetch writes **nothing**. Writing an empty aircraft list would be indistinguishable
from "no traffic was in range", which is a different and false claim.

**Later (real):** move snapshotting into the proxy, writing the in-range aircraft once per
airband transmission rather than per poll. Same file format, so the scorer is unchanged.

### 2. Worksheet generator

Reads the plugin's `captures/<date>/index.jsonl` (index, timestamp, channel, duration, text)
and emits one block per airband transmission:

```
--- 0042 -------------------------------------------------------------
audio    : 0042_sent.wav   (raw: 0042_raw.wav)
time     : 2026-09-10T11:23:14.881+02:00      channel: 121,205   3.8s
machine  : Two thousand feet, ten nineteen, Delta one nine two.
system   : extracted DAL192 -> NO MATCH  (nearby: DAL162, DAL47, DAL161)

heard    : Two thousand feet, ten nineteen, Delta one nine two.
aircraft :
```

**`index.jsonl` stores the text the plugin displayed, tag included** — verified 2026-09-10:
rows read `"text":"[DAL73/A333] New York Delta seven three..."`. So a successful
identification is recoverable from the capture index alone, and the proxy log is needed only
for the near-miss diagnostic on failures (`[flight-id] no match ... nearby: [...]`), which is
console-only. The `machine` line strips the tag; the `system` line reports it.

`heard` is pre-filled with the machine text (tag stripped) so the task is editing, not
transcribing. The
HOW TO MARK header states plainly that this biases the labeller toward accepting the machine's
version — it is still the right trade against transcribing an hour of radio by hand, but the
bias is disclosed rather than hidden.

`aircraft` accepts a callsign (`DAL162`), `NONE` (no aircraft named — ATC chatter, bare
readbacks), or `UNSURE` (a callsign is spoken but cannot be made out). Nothing else is asked
of the labeller; every other fact the scorer derives from the snapshot.

Joining a capture row to its transcript is **by time, not by index** — the plugin's clip index
and the proxy's transcript sequence are independent counters.

### 3. `server/bench_flight_identify.py`

- `--labels X` — score what happened live. The historical record.
- `--labels X --replay` — re-run `extract_callsign_candidate` and `match_flight` against the
  snapshot nearest each transmission's timestamp. The mode that makes a change measurable.

Snapshot join: nearest snapshot at or before the transmission time, within 20s (polls are
15s). A row with no snapshot in window is **excluded and reported as excluded**, never scored
as "nothing in range".

Every labelled row with a real aircraft lands in exactly one bucket:

| Outcome | Test | Fixable by |
|---|---|---|
| **Correct** | tagged, matches the label | — |
| **Retrieval miss** | true callsign absent from the snapshot | nothing here; this is the ceiling |
| **Extraction miss** | in range, but no candidate produced | telephony table, abbreviated-callsign handling; arm A may also help |
| **Selection miss** | candidate produced, no match | arms B and C |
| **Wrong match** | tagged, but not the labelled aircraft | precision guard (BERGE TOWNSEND class) |

Headline figures: precision (correct ÷ all tags applied), recall against the achievable
ceiling (correct ÷ rows naming an in-range aircraft), and the ceiling itself (rows naming an
aircraft that was not in range).

## Testing

The worksheet generator and the scorer are ordinary modules under `server/tests/`, TDD as
usual:

- Snapshot join: exact hit, nearest-preceding, outside the 20s window (excluded, not
  zero-scored), empty snapshot file.
- Bucket assignment: one test per row of the outcome table, including the wrong-match case,
  built from the real `DAL162`/`DAL192` and `KLM6A`/`KLM66A` transmissions.
- Worksheet parsing: unedited row (still pre-filled), edited `heard`, `NONE`, `UNSURE`,
  malformed block.
- Generator: full text preserved verbatim including the characters that would tempt an
  excerpt; a tagged capture row (`[DAL73/A333] ...`) split correctly into `machine` (tag
  stripped) and `system` (tag reported); an untagged row; a capture row with no matching
  proxy-log diagnostic.
- The runway-designator row (`"is one eight Charlie available"`) is carried in the corpus as a
  standing negative case: it must never score as an identification, whatever is built later.

The side-car itself is throwaway and untested by design. If it graduates into the proxy it
gets tests then, and its correctness is currently established empirically — its first poll
recorded 83 aircraft with real callsigns, types, altitudes and registrations.

## Rollout

1. Side-car started 2026-09-10 ~11:11 local, covering the user's capture hour. **Done.**
2. User labels the resulting worksheet by ear.
3. Score it. Publish the baseline and the four-way split.
4. Only then decide which of A, B or C to build — the split says which one is binding.

## Success criteria

- [ ] A baseline precision and achievable-ceiling recall figure exists for airband flight ID.
- [ ] Every labelled miss is assigned to exactly one of retrieval / extraction / selection /
      wrong-match.
- [ ] `--replay` reproduces the live result on unchanged code — if replay and history disagree
      on rows with a valid snapshot, the harness is wrong and nothing built on it can be
      trusted.
- [ ] The split names which of A, B, C addresses the largest bucket, with a number attached.

## Deferred

- Arms A, B and C themselves — deliberately, until the split says which is binding.
- Per-transmission in-proxy snapshotting (the side-car covers the first corpus).
- Scoring the 2026-09-07→09 sessions retrospectively: no snapshots exist for them and no
  audio was captured for most, so they can support miss *classification* but not replay.
- **Poll geometry — measure on the NEXT run, not this one.** The circle is centred at
  52.15/4.3 with a 40nm (74 km) radius, which is 36.2 km southwest of Schiphol. It therefore
  reaches only ~20nm past the field to the northeast while extending ~60nm southwest over
  sea. One instantaneous sample (2026-09-10, airline callsigns only, airborne below 15,000ft,
  within 40nm of Schiphol) put **1 of 20 candidates outside the circle** — `KLM26X`, 8900ft,
  54 km out on bearing 056 — and the visible traffic showed a matching bearing skew: N 8,
  **E 1**, S 3, W 7. Consistent with northeast arrivals being truncated, but it is a single
  poll and is treated as indicative only.
  The next measurement run should log a wide circle alongside the narrow one and report the
  distribution over a full session. Note that widening is **not** a free win: it enlarges the
  candidate set, which cuts against precision, so it is a trade to score rather than a fix to
  apply. Deliberately not run during the first corpus, to avoid changing the poll geometry
  midway through the session being labelled.
- **Ground-traffic filtering.** Aircraft parked at Schiphol (~36 km out, `alt_baro: "ground"`)
  sit in the candidate set `match_flight` searches — four of six Deltas at one sampled instant,
  including `DAL161`, a name that recurs in the near-miss diagnostics. Filtering
  `alt_baro != "ground"` is a one-line change, but it could cost recall for aircraft that have
  just landed and are still on frequency. Score it with `--replay`; do not assume it.
- Conversation-level resolution for airband. The 2026-09-10 interleaving analysis found
  Approach 4's gaps are inverted relative to CH01's (same-aircraft gaps of 90–112s against
  13–30s to a different aircraft), so the CH01 window-and-resolve pattern needs a different
  design, not retuned constants. Out of scope here; this arm measures whether it is worth it.
