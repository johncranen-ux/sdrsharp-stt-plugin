# Airband flight identification (v1, API-only) — design

**Date:** 2026-09-08
**Status:** design agreed, not implemented
**Related:** mirrors the AIS vessel-identification pipeline ([[aishub-vessel-source]],
`server/stt_proxy/identify.py`, `server/stt_proxy/ais.py`), but is a separate feature for a
separate band, not an extension of the maritime one.

## The problem

Airband transcripts read as noise even when the decoder got the words right — headings, QNH,
flight levels and runway numbers dominate, and the one piece of information that would anchor
a transmission to something real (which aircraft this is) is either missing or itself garbled.
The maritime pipeline solves the equivalent problem for ships by resolving a heard name/callsign
against a live AIS feed. Nothing plays that role for aircraft yet.

## Feasibility, checked before designing further

Two things were verified against the live proxy log and a live API call, not assumed.

**1. Callsigns are actually present in the real (non-filtered) transcript output.** Scanning
the whole 2026-09-08 session log: `KL190`, `American two two one` (×2), `Delta two four three`,
`United nine zero eight` (×2), `Transavia, six november`, `Envoy six zero`, `Qatar Air Three
Echo Heavy`, `China seven three zero eight`, repeated `Papa...`-prefixed fragments consistent
with Dutch PH- registrations. Identification is feasible.

**2. Airline codes get garbled almost as often as ship names did.** In the same session, KLM
alone appears as `KLM`, `KALM`, `RKLM`, and `KLMX`. **Exact-string matching on the airline code
will miss most of these.** This is the same problem `ais.py`'s name matching solved with
`rapidfuzz`, and the fix is the same: fuzzy-match the airline-code portion, don't require an
exact hit. This finding changed the design — the original sketch assumed clean regex extraction
would be enough; it will not be.

**3. Data source, checked live, not from documentation.** Three free/no-key ADS-B APIs were
candidates; only one actually works from here today:

| source | result |
|---|---|
| `api.airplanes.live/v2/point/...` | **HTTP 403** — "contact us... your email MUST include any links, a description of the project" — no longer open, gated behind manual approval |
| `api.adsb.lol/v2/point/...` | connection failed outright (curl exit, no response) |
| `api.adsb.one/v2/point/...` | blocked by a Cloudflare JS challenge — unreachable by a server-side poller |
| `opendata.adsb.fi/api/v2/lat/{lat}/lon/{lon}/dist/{dist}` | **200, real live traffic**, 59 aircraft within 40 nm of (52.15, 4.3) at test time, including `KLM281`, `KLM1504`, `RYR37DV`, `AFR16JN`, `EZY85FV`, `PHVSY` (a Dutch-registered light aircraft) |

adsb.fi is the only one that is both open and live-verified. Its URL shape differs from
airplanes.live's (`lat/{lat}/lon/{lon}/dist/{dist}`, not `point/{lat}/{lon}/{radius}`) —
worth remembering if either service changes again, since the two are easy to conflate.

## Decisions

| decision | choice | why |
|---|---|---|
| Data source (v1) | **adsb.fi**, no key | Only one of four candidates that is both open and actually reachable, confirmed live |
| Local receiver (2nd dongle) | **Deferred to Phase 2** | AIS evolved the same way — aisstream proven first, AISHub added later. Get the API-only version working and measured before adding a second source and failover logic |
| Extraction method | **Regex/dictionary, not Claude** | User's explicit choice for v1: "simple, match a heard callsign, show inline" — no conversation resolver, no LLM extraction pass |
| Callsign decoding | **Reuse `corrections.py`'s `_PHONETIC_LETTERS` / `_SPOKEN_DIGITS`** | Already built for spelled-out ship callsigns; directly applicable to spoken digit strings and phonetic tail numbers — no need to rebuild |
| Airline-code matching | **Fuzzy (`rapidfuzz`), not exact** | Feasibility check found KLM alone garbled 3 different ways in one session; exact matching would miss most real hits |
| Scope | **Approach/Tower channels only** | Ground sometimes carries callsigns; ATIS/Departure Information never does — no point running extraction against a channel that structurally cannot contain one |
| Display | **Prefix the transcript text**, same convention as `format_for_plugin` | `"[VESSEL/type] (MMSI) text"` becomes `"[TRA6N/B738] text"`. Zero plugin changes — it already displays whatever `text` the server returns, and this stacks with the channel prefix shipped earlier today: `[121,200 MHz] [TRA6N/B738] Approach, good day...` |

### Alternatives rejected

**airplanes.live as primary source.** Was the original plan going into this session, based on
its documentation stating no key required. Live-tested and found to return a hard 403 asking
for manual approval — the documentation is stale relative to current access policy. Kept as a
documented rejection so a future session doesn't re-propose it without re-checking.

**OpenSky Network.** Free and well-established, but anonymous (non-contributor) access is
credit-limited in a way that doesn't suit a continuous poll loop; contributor-tier access needs
running a feeder, which is exactly the Phase 2 local-receiver work, not simpler than adsb.fi
for v1.

**Claude-based extraction (mirroring `identify.py::extract_vessel`).** More robust to varied
phrasing, but explicitly out of scope for v1 by the user's own choice, and adds per-transmission
API cost/latency the maritime system only took on after the simpler approach was already
running.

**Conversation-level resolution (mirroring `conversations.py`).** Same reasoning — real
sophistication, real cost, deferred until the single-transmission version is proven.

## Architecture

### New module: `server/stt_proxy/adsb.py` (parallel to `ais.py`)

```
adsb.fi /api/v2/lat/.../lon/.../dist/...  --poll every 15-20s-->  adsb.py  --> _aircraft_cache
```

- A threaded poll loop, structurally the same shape as `ais.py::_ais_thread` — a poll function,
  an in-memory cache, a `_last_good_poll` marker, and the same "no data key means feed trouble,
  not an empty sky" failure handling `aishub.py` already established (an ADS-B API returning
  0 aircraft over Schiphol/Rotterdam approach paths during business hours is never a real
  observation; it's the same silent-failure shape AISHub's `ERROR: true` was).
- Poll interval much shorter than AISHub's 900s: aircraft move at 150-250 m/s on approach,
  ships at a few m/s. 15-20s keeps the cache meaningfully current without exceeding adsb.fi's
  implicit fair-use expectations (no published hard rate limit found; poll conservatively and
  watch for 429s rather than assuming a number).
- Cache keyed by `hex` (ICAO24, always unique and stable for the aircraft's session) — same
  reasoning as AIS's move to MMSI-keying: `flight` (callsign) is the thing users say, not the
  thing that's safe to key a cache on, since two aircraft can show the same padded/truncated
  callsign field in edge cases and a real identifier should own the cache.
- Fields cached: `flight` (callsign, trimmed), `hex`, `r` (registration), `t` (aircraft type),
  `alt_baro`, `gs`, `track`, `lat`, `lon`, `squawk`, last-seen.

### New module: `server/stt_proxy/flight_identify.py` (parallel to `identify.py`, regex not LLM)

- `extract_callsign_candidate(text: str) -> str | None` — scans corrected transcript text for
  an airline-telephony word (a new, small table: Transavia→TRA, KLM→KLM, American→AAL,
  United→UAL, Delta→DAL, Envoy→ENY, Qatari→QTR, etc., seeded from carriers actually observed
  in this session and grown from there) followed by a spoken-digit run, decoded via
  `corrections._decode_spoken_word`/`_SPOKEN_DIGITS`. Also tries `_phonetic_callsign_probes`
  (already built) for GA-style spelled-out registrations (`Papa Delta` → `PD`-suffixed match
  attempt).
- `match_flight(candidate: str) -> dict | None` — fuzzy-matches the airline-code portion of the
  candidate against `adsb.py`'s live cache (`rapidfuzz.fuzz.ratio`, same library already a
  dependency), exact-matches the digit portion (digits are not usually misheard the way letters
  are — no evidence yet that they need fuzzing too; revisit if measurement says otherwise).
- `format_flight_for_plugin(result: dict, text: str) -> str` — `f"[{flight}/{type}] {text}"`,
  same shape as `identify.py::format_for_plugin`.

### Wiring

In `whisper-proxy.py`, the existing `elif mode == "airband":` branch
(`whisper-proxy.py:459-464`) gains one step after `_apply_sttt_corrections`:

```python
elif mode == "airband":
    corrected = _apply_sttt_corrections(raw_text, mode="airband")
    if channel in APPROACH_TOWER_CHANNELS:          # not Ground/ATIS/Departure Info
        flight = flight_identify.match_flight(
            flight_identify.extract_callsign_candidate(corrected))
        if flight:
            corrected = flight_identify.format_flight_for_plugin(flight, corrected)
    data["text"] = corrected
```

`APPROACH_TOWER_CHANNELS` is a config constant, not hardcoded inline — the channel list is
already in flux this session (frequency corrections, exclude candidates), so it should be one
named place to update, not scattered through the branch.

## Testing

Unit tests, synthetic — mirrors the AISHub design's split between logic tests and a live
contract check:

- `extract_callsign_candidate`: clean phrasings (`"Transavia six november"` → `TRA6N`), garbled
  airline names that should still extract a candidate for the fuzzy matcher to resolve, and
  bare digit strings with no airline anchor (`"one nine zero"` alone) that must return `None` —
  ambiguous without an anchor, same reasoning AIS uses for unsupported callsign matches.
- `match_flight`: exact and near-miss (garbled) airline-code lookups against a fake cache;
  digit-portion mismatch correctly returns no match even when the airline code is right.
- `adsb.py` poll loop: the "aircraft list present but empty" vs "no aircraft key / network
  failure" distinction, mirroring `aishub.py`'s tested failure handling — an empty response
  during a busy period must not silently look like "no traffic."
- A **live contract check, run by hand, not in CI** — one real call to adsb.fi asserting the
  response shape and field names, same reasoning as AISHub's contract test: synthetic fixtures
  validate code against an assumption, not against the server, and this project has already
  been burned by that once (`local-ais-receiver`'s wrong transport-shape assumption).

## Rollout

1. `adsb.py`: poll loop + cache, no wiring yet. Verify against the live endpoint by hand.
2. `flight_identify.py`: extraction + matching, unit-tested against fixtures and the fake
   cache. No wiring yet.
3. Wire into `whisper-proxy.py` behind the existing `mode == "airband"` branch, restricted to
   `APPROACH_TOWER_CHANNELS`.
4. Listen against real traffic (same validation method the AIS feed swap used — verified by
   observation on live traffic, not a benchmark, since there's no labelled airband corpus yet).

## Success criteria

- [ ] `adsb.py` fills a live cache from adsb.fi; a failed/empty poll leaves the cache and
      "in scope" state untouched rather than looking like an empty sky.
- [ ] `extract_callsign_candidate` pulls a candidate from real garbled examples already seen in
      today's log (`"Hello KLM"`, `"Transavia, six november"`, `"American two two one"`).
- [ ] `match_flight("KALM 676")`-style garbled input still resolves to the same aircraft
      `"KLM676"` would.
- [ ] A transmission with no extractable callsign displays exactly as it does today — no
      degradation, no crash, no empty-bracket artifact.
- [ ] Full test suite green.

## Deferred

- Local ADS-B receiver (second RTL-SDR dongle) as a backup/primary source, with failover logic
  between it and adsb.fi.
- Claude-based extraction for phrasings the regex/fuzzy approach misses.
- Conversation-level resolution (tracking a multi-turn exchange before committing to an
  identification), mirroring `conversations.py`.
- A dedicated "identified flights" log page, mirroring `vessel_log.py`.
- Enriching beyond callsign/type — route (origin/destination), operator name, photo links, the
  things `enrich_with_ais` adds for vessels — once the basic match is proven.
- Digit-portion fuzzing, if measurement ever shows flight numbers get misheard as often as
  letters do (no evidence of this yet, unlike airline codes).
