# AISHub as the vessel source — design

**Date:** 2026-08-12
**Status:** design agreed, not implemented
**Supersedes:** the callsign-lookup investigation of 2026-08-11 (AISHub returns `CALLSIGN` directly)

## The problem

The aisstream.io feed has delivered nothing since **2026-08-05 13:31:30 UTC** — 162.6 hours as
of this design, monitor incident 51 never closed, confirmed external by elimination (a fresh
key, a whole-world bbox and no filters were all equally silent). The vessel cache has been
frozen for a week, so every identification runs against stale data.

On 2026-08-12 ~06:30 UTC AISHub accepted the local station (id 4039) and granted API access
immediately — there was no 7-day qualification wait. That makes a REST feed available which
covers the water the local receiver never could.

Three problems get solved together, and the second and third were found while designing the
first.

### 1. The feed is dead

Measured today: one call to `data.aishub.net/ws.php` over the Maas approach returns **994–1010
vessels, 993+ named**, carrying `MMSI, NAME, IMO, CALLSIGN, LATITUDE, LONGITUDE, TYPE, DEST`,
with destinations reading `NLRTM` / `ROTTERDAM,NL`. This is exactly the traffic calling Maas
Approach — the traffic the local receiver has measured **0 vessels within 25 km of Maas
Center** for, in every run ever done.

### 2. The cache cannot hold two ships with the same name

`_vessel_cache` is keyed by `name.upper()` (`ais.py:89`, `439-444`, `451-469`). Two ships named
FORTUNA do not sit in it as two candidates — **they collide on one key and merge into a single
entry**, taking MMSI from whichever sent static data last and position from whichever
transmitted last.

This is not rare. Measured on live AISHub snapshots today:

| box | vessels | duplicate-name groups | vessels sharing a name |
|---|---|---|---|
| Maas approach (51.85–52.25 N, 3.55–4.45 E) | 1,010 | **17** | 37 |
| extended (51.0–53.2 N, 2.0–6.0 E) | 9,293 | **777** | **2,176 — 23% of named** |

In the approach box alone, simultaneously present: `ALBATROS ×3`, `ESTRELLA ×3`, `CORNELIA ×3`,
`FORTUNA ×2`, `DELTA ×2`, and eleven more. In the extended box: `ALBATROS ×14`, `ANNA ×14`,
`ORION ×13`.

**Expiring vessels that leave the box does not fix this**, which was the intuitive fix. There
are 17 names with 2–3 *simultaneously present* ships in the approach box. Expiry alone still
leaves three live ALBATROS entries colliding on one key.

### 3. Near-miss names are resolved by coin flip

`_best_name_match` (`ais.py:537-543`) keeps the single highest score (`score > best[1]`) and
returns it. It has no notion of a close call. Measured with `fuzz.ratio` at the production
cutoff of 76:

| what is said | winner | runner-up | gap |
|---|---|---|---|
| VOLGA MAERSK (clean) | VOLGA MAERSK 100 | VAGA MAERSK 87.0 | 13.0 |
| VOGA MAERSK (one letter dropped) | VOLGA MAERSK 95.7 | VAGA MAERSK 90.9 | 4.7 |
| VULGA MAERSK (vowel misheard) | VOLGA MAERSK 91.7 | VAGA MAERSK 87.0 | 4.7 |
| DELTA 3 (clean) | DELTA 3 100 | DELTA D 85.7 | 14.3 |
| **DELTA (token dropped)** | **DELTA 3 83.3** | **DELTA D 83.3** | **0.0** |

The last row returns a confident identification decided by list order. The same file already
applies the opposite rule to callsigns — `match_by_callsign_pattern` (`ais.py:593-597`) returns
None when several match, because *"picking any of them would be a guess wearing evidence's
clothes."* That principle is simply not applied to names.

**Incidental finding, out of scope here:** `DELTA THREE` matches nothing at all (below the 76
cutoff against `DELTA 3`). Spoken digits versus numerals is an unhandled gap and CH01 speakers
say digits aloud constantly. Logged, not fixed by this work.

### Why `AIS_MAX_AGE_MIN` was never the answer

`ais.py:235-239` explains that vessels are excluded at match time rather than deleted, because
deletion is destructive and silent — you lose the ability to ask whether the entry you removed
was the one that went on to call. Sound reasoning, but the mechanism has never been able to
run. Measured on the live cache today:

| | |
|---|---|
| cache entries | 8,672 |
| **no `last_seen` at all** | **8,609 — 99.3%** |
| with `last_seen` | 63, all from 2026-08-10 |
| no position at all | 1,432 |

Enabling the filter today would exclude 99.3% of the cache. AISHub fixes this at the root:
every poll writes a true observation time (`TIME`, the timestamp of the position report, not of
retrieval) for every vessel in the box.

## Decisions

| decision | choice | why |
|---|---|---|
| Source role | AISHub primary; aisstream preserved | aisstream was a reliable free source for a long time and may return |
| How aisstream is preserved | Live code behind `AIS_SOURCE`, **not commented out** | Commented code is not covered by the 653 tests, so it rots silently and will not run when reverted to |
| Cache identity | MMSI-keyed, name resolves to candidates | The only thing that can hold 14 ALBATROS apart |
| Foundation | Cherry-pick `record()` from `feat/local-ais-receiver` | Carries already-earned fixes: `_pending` flush, name-collision guard, newest-wins positions |
| Out-of-box vessels | Excluded from matching, not deleted | User's rule (they are out of scope) while keeping `ais.py:235-239`'s measurability |
| Ambiguity | Surfaced to the operator as clickable candidates | Preserves information; no unmeasured LLM decision; judgement sits where the context is |
| Box size | Wide (51.0–53.2 N, 2.0–6.0 E) | Margin buys lead time, which buys a slow poll |
| Poll interval | 900 s, hard floor 60 s in code | 64+ km of western margin is ~138 min at 15 kn; a 1-min poll is over-specified |
| Bench scope | Matcher only, against the frozen cache | The feed change is not measurable by replaying August labels |

### Alternatives rejected

**Full merge of `feat/local-ais-receiver`.** Gets `record()` plus the local receiver, but the
branch is 23 ahead / 39 behind with real conflicts in `ais.py`, `whisper-proxy.py` and
`test_whisper_proxy.py` — exactly the files Feature 1 rewrote — and drags in `ais_local.py`,
`pyais` and a UDP listener that hears only Scheveningen harbour and has no identification
value. Cherry-picking the merge core is the same benefit at a fraction of the conflict.

**Building fresh on master.** No merge work, but reimplements ~350 lines of merge logic whose
subtle bugs were already found and fixed once.

**Changing `_vessel_cache` to `{NAME: [entry, ...]}`.** Honest, but breaks 20 production call
sites and a large number of fixtures in the 132 KB `test_whisper_proxy.py` for no functional
gain. It stays a view onto the top-ranked candidate instead.

**Snapshot-authoritative cache** (only what is in the box now). Smaller and fresher, but
discards weeks of history in one uncontrolled step.

**Auto-expire after N hours.** Front-loads a guess at exactly the number there is no data for.

**Letting the LLM resolver pick between candidates.** Richer signal, but puts an unmeasured
decision into an LLM path and needs its own bench. The operator has better context and asked
for the choice.

## Architecture

### Source selection

New module `stt_proxy/aishub.py` owns the HTTP client, the poll loop and the field mapping.
`ais.py` keeps the cache and the matchers whose thresholds depend on its shape — it is already
36 KB and a second inline feed would make that worse. The aisstream websocket is **not** moved;
it works, it is tested where it is, and relocating it is churn this feature does not need.

Replacing the `AISSTREAM_API_KEY`-presence check at `whisper-proxy.py:507`:

```
AIS_SOURCE       = aishub | aisstream | off      (default: aishub)
AISHUB_USERNAME  = <from .env, never committed>
AISHUB_BBOX      = 51.0,53.2,2.0,6.0
AISHUB_POLL_SEC  = 900
```

```
AISHub /ws.php  --15 min-->  aishub.py  --record(fields, source="aishub")--> _mmsi_index
aisstream ws    --frames-->  ais.py::_process_ais --record(source="aisstream")-->  (same)
```

One writer, one merge implementation, two adapters.

### Field mapping

| cache field | AISHub |
|---|---|
| name / callsign / mmsi / imo / type | `NAME` / `CALLSIGN` / `MMSI` / `IMO` / `TYPE` |
| length / beam | `A+B` / `C+D` |
| draught / destination | `DRAUGHT` / `DEST` (through `_clean_destination`) |
| latitude / longitude / sog / cog / heading | `LATITUDE` / `LONGITUDE` / `SOG` / `COG` / `HEADING` |
| last_seen | `TIME` — a real observation time |

### Cache and identity model

```
_mmsi_index    : {mmsi -> entry}        authoritative; one entry per real ship
_name_index    : {NAME -> [mmsi, ...]}  all ships sharing that name          (NEW)
_vessel_cache  : {NAME -> entry}        best candidate for that name; a VIEW
_callsign_cache: {CALLSIGN -> entry}    unchanged
```

`_vessel_cache` holds references to the same entry dicts, so `/api/vessels`
(`whisper-proxy.py:339`), the bench scripts and the existing matchers keep working untouched.

**Relevance ranking**, used both to pick the view entry and to order candidates:

1. **In scope** — appeared in the most recent successful poll. Left the area ⇒ not a candidate.
2. **Proximity to the approach corridor** — great-circle distance to Maas Center
   (`52.02, 3.88`, the constant the codebase already uses), nearest first. This neutralises the
   777 collisions the wide box imports: a barge at Nijmegen never outranks a tanker at Maas
   Center. A vessel with no position ranks last rather than being excluded.
3. **Type plausibility** — a sailing yacht ranks below a tanker for a Maas Approach call.
4. **Recency** — final tie-break, meaningful now that `TIME` is real.

`match_by_name_candidates(name) -> [entry, ...]` is new. `match_by_name` keeps its existing
single-entry contract for the live path, returning the top rank, so nothing downstream changes
shape.

The existing 8,672 entries still load. With 99.3% carrying no timestamp they fail the in-scope
test and rank last — retired without being destroyed.

### The poll loop, and the failure it must not have

When the rate limit was exceeded during design, AISHub returned **HTTP 200** with a valid-JSON
104-byte body: `ERROR: true`, no ships. Parsed naively that reads as *"the box contains zero
vessels"*, which combined with the in-scope test would mark **every vessel out of scope** and
silently destroy identification. This is the aisstream silent-failure pattern in different
clothes — a feed that fails by returning success.

| outcome | meaning | action |
|---|---|---|
| ships returned | a real observation | update cache, advance `_last_good_poll` |
| `ERROR: true` / no ships key / HTTP or network failure | **no observation** | log, back off, leave cache and scope untouched |
| ships key present but empty | genuinely empty box | treat as an observation |

**In-scope is defined against the last successful poll, not wall-clock time.** If the feed is
down for an hour nothing ages out; the cache simply stops updating, which is honest. Defining
scope as "last_seen within N minutes of now" would make a feed outage indistinguishable from
every ship leaving the estuary — and this project has already lost six days to a feed that
failed quietly.

**The 60-second floor is enforced in code**, not by configuration, because the documented
penalty is silent data denial and it has been confirmed live.

A silence watchdog reuses the existing `_silence_report` shape, distinguishing "never
succeeded" (bad username, wrong bbox) from "went quiet mid-run".

Persistence is unchanged: `_save_cache` every 300 s to the same JSON list; indexes rebuild from
`mmsi` on load.

### Bandwidth

| box | vessels | payload | at 15-min poll |
|---|---|---|---|
| current `ROTTERDAM_BBOX` | 7,787 | 2.23 MB | 214 MB/day |
| extended (chosen) | 9,293 | 2.66 MB | 255 MB/day uncompressed, ~30 MB/day gzipped |

gzip is required, not optional — at a 1-minute poll the extended box would be 3.8 GB/day
uncompressed (3.2 GB/day for the current box).
`interval` (max position age, minutes) is available as a further reduction knob but is left
unset: a guessed value silently drops moored vessels.

### `/conversations` candidate list

Candidates render only when the identification is contested — more than one in-scope vessel
matched, or the top two scores within a small margin. `DELTA` (83.3 / 83.3) and `VOGA MAERSK`
(95.7 / 90.9) qualify; a clean 100-vs-87 does not.

```
Heard: "Delta"  — 2 candidates, scores 83.3 / 83.3

  DELTA 3   Tanker         4.2 km NW of Maas Center   dest NLRTM   seen 10:14  [VesselFinder]
  DELTA D   General cargo  in Waalhaven, moored       dest —       seen 10:11  [VesselFinder]
```

Type, position relative to the approach, destination and freshness are what let the operator
say "the one already in the harbour is not dropping anchor at Echo 3".

The resolved-conversation record gains a `candidates` list. Purely additive — the 286 existing
rows lack the key and render as they do today, so no migration.

`markup.py:24` moves from the search URL `vessels?name={mmsi}` to the details URL
`vessels/details/{mmsi}`, which lands on the ship rather than a result set. Still MMSI and not
name, because "vessel names are not unique" is the problem being solved. Escaping and
`rel="noopener"` are unchanged; AISHub data is untrusted in exactly the way `markup.py:8-10`
describes.

**Deliberately not built:** clicking a candidate records nothing. A click that recorded "this
was the right ship" would be free labelled ground truth for the identification bench, but it
needs a store, a schema and a correction path, and none of that is needed to answer the
question asked. Its own piece, later.

## Testing

Unit tests use synthetic fixtures for **logic**: field mapping, the `ERROR: true` path, the
rate-limit floor, malformed JSON, network failure, ranking, ambiguity detection, rendering.

A separate **contract test, run by hand and not in CI**, makes one real call and asserts the
shape: envelope-then-ships array, the field names above, `TIME` parseable, `ERROR` present.

This split exists because of a failure this project has already had. The local-AIS branch
shipped with a wrong assumption about transport shape and its design note records why nothing
caught it: *"all fixtures were synthetic JSON in the assumed shape."* Synthetic fixtures
validate code against an assumption, not against the server. The contract test cannot live in
CI — it needs the credential and burns a rate-limited request.

Real captured responses cannot be committed as fixtures: `ais_cache.json` is gitignored
alongside the transcription data under NL Telecommunicatiewet 18.13 / ITU RR 17.3, and those
patterns were only just repaired after the history rewrite.

**Measurement, and its honest limit.** `bench_identify.py` replays labelled conversations
against the cache and currently reports 85.7% precision / 76.5% recall. It must be run before
and after, because the matcher changes are the risky part.

It **cannot** measure the feed change. The labelled conversations are from early August;
AISHub returns vessels present now, and those ships are not in today's box. The bench keeps
pointing at the frozen historical `ais_cache.json` and measures the matcher in isolation. The
feed swap is verified by observation — vessels named on live CH01 traffic resolving — not by a
benchmark.

## Rollout

Each step independently verifiable; 1–4 are revertible without changing production behaviour.

1. Cherry-pick the `record()` core (`record`, `_mmsi_index`, `_pending`, `_apply`) and its
   tests onto master, plus the `motor vision` → Motorvessel regex in `corrections.py`. Suite
   green, no behaviour change.
2. Add `aishub.py` behind `AIS_SOURCE`, default unchanged. Poll works, cache fills.
3. MMSI/name indexes + ranking. Bench before/after.
4. Candidates on `/conversations`.
5. Flip `AIS_SOURCE` default to `aishub`.

## Success criteria

- [ ] `AIS_SOURCE=aishub` fills the cache from a live poll; `AIS_SOURCE=aisstream` still runs
      the websocket path unchanged.
- [ ] A rate-limited or failed poll leaves the cache and scope untouched, and says so.
- [ ] The cache holds 14 distinct ALBATROS entries, distinguishable by MMSI.
- [ ] `match_by_name("DELTA")` yields both DELTA 3 and DELTA D as candidates rather than one
      arbitrarily.
- [ ] Contested identifications render a candidate list on `/conversations` with working
      VesselFinder links.
- [ ] `bench_identify.py` shows no regression against the frozen cache.
- [ ] Full suite green (653 server tests at time of writing).

## Deferred

- Spoken digits versus numerals (`DELTA THREE` → `DELTA 3`).
- Click-to-confirm feedback loop producing labelled ground truth.
- Restoring `AIS_SILENCE_WARN_SEC=60` if aisstream ever recovers.
- Calibrating `AIS_MAX_AGE_MIN` now that `last_seen` becomes real.
