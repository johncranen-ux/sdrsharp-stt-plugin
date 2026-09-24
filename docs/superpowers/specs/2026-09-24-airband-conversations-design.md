# Airband conversations (Schiphol Approach 4) — design

**Date:** 2026-09-24
**Status:** IMPLEMENTED 2026-09-24 on feat/airband-conversations, autopilot clue in shadow mode; deploy + live check pending; gate run due after 3-5 days of traffic.
**Related:** `2026-09-08-airband-flight-identification-design.md` (the per-transmission
`[FLIGHT/TYPE]` tag this builds on), `2026-09-10-airband-identification-measurement-design.md`
(the labelled 09-10 hour and `bench_flight_identify.py`), `2026-09-11-airband-prompt-biasing-design.md`
(why the decoder prompt is not the lever), `2026-08-24-conversation-archive-and-comments-design.md`
(the SQLite archive this writes into).

## The problem

Schiphol Approach 4 (121.200, tuned as 121.205) is hard to follow. A transmission is a string of
numbers that could be a callsign, an altitude, a heading, a runway or a frequency, and several
aircraft's conversations run at the same time, so it is not clear which transmission belongs to
which conversation. Grouped under a flight, the numbers start to make sense.

The operator wants a view in the web app that groups Approach 4 transmissions **per flight**,
live and for past periods. Explaining what each number means is wanted later, not in this
release.

### Why the Maas Approach conversations code is not reused

CH01's `stt_proxy/conversations.py` journals transmissions, closes a window after a 60 s gap, and
has Claude Haiku split the window into exchanges and pick a vessel from AIS candidates. The
2026-09-10 investigation showed this does not transfer to Approach 4:

- Traffic **interleaves**: several aircraft's exchanges braid together over minutes.
- The gap heuristic **inverts**: turns of the *same* aircraft were up to 112 s apart, turns of
  *different* aircraft 13–30 s apart. A gap window would fragment real conversations and fail to
  close in busy periods.
- Pilots abbreviate rather than repeat their callsign, so the repetition cue is weak.
- The 09-11 measurement showed a language model fills unintelligible audio with plausible
  callsigns (clips 0027, 0035, 0108). An LLM resolver would reintroduce that risk.

### A second, independent clue exists and is being thrown away

adsb.fi returns the autopilot's **selected** values, not only the current state. A live query on
2026-09-24 (81 aircraft) returned `nav_altitude_mcp` for 39, `nav_qnh` for 36, `baro_rate` for 44
and `nav_heading` for 22. Example: KLM12B at 9,725 ft with 7,008 selected.

A controller's "descend seven thousand" should therefore show up, seconds later, as a change of
the selected altitude on exactly one aircraft: a way to attribute a transmission **without
hearing the callsign correctly**. `adsb.map_aircraft` currently drops all of these fields and the
09-10 snapshot log does not contain them. **This clue is unmeasured.**

## Decisions taken in brainstorming

| question | decision |
|---|---|
| Purpose | Comprehension: group transmissions per flight so the numbers make sense |
| Approach | Combine the callsign tag with the autopilot clue in one attribution step |
| Autopilot clue | Built and computed now, **shown only after it passes a measurement** |
| Layout | Flight list ("strips") on the left, selected flight's conversation on the right |
| Time scope | Live by default, plus any past hour/day from the archive |
| Corrections | "Move to…" dropdown per transmission; every move is stored |
| Audio | ▶ playback of the captured clip, in this release |
| Scope | 121.200 / 121.205 only; other channels later by editing one list |

## Architecture

Separate from the CH01 code. No shared logic with `conversations.py`.

```
plugin ──chunk──▶ whisper-proxy.py ──▶ transcribe ──▶ flight_identify (callsign clue)
                        │                                     │
                        │                  flight_attribution.attribute_now()  ── stage 1
                        │                                     │
                        │                         air_transmissions (conversations.db)
                        │                                     ▲
adsb.py poll (15 s) ──▶ snapshot cache + logs/adsb-YYYY-MM-DD.jsonl
                                                              │
                        flight_attribution.recheck_due()  ────┘  ── stage 2, ~60 s later

webapp  ──▶ /api/air/flights?from&to   /api/air/flights/{key}   POST /api/air/moves
        ──▶ "Airband" tab (strips + conversation + move-to + ▶ via /api/clips)
```

### Units

1. **`stt_proxy/adsb.py` (changed).** `map_aircraft` keeps `nav_altitude_mcp`, `nav_heading`,
   `nav_qnh`, `baro_rate` in addition to today's fields. Every successful poll is appended to
   `logs/adsb-YYYY-MM-DD.jsonl` (`{"t": <ISO time>, "aircraft": [...]}`), the same shape as the
   existing `adsb-snapshots-2026-09-10.jsonl`. It also keeps a short in-memory ring of recent
   snapshots (≥ 2 minutes) that stage 2 reads. **This unit is built and deployed first**, because
   every day without the fields is data the measurement cannot use.

2. **`stt_proxy/flight_identify.py` (small change).** `identify_flight` returns prefixed text. A
   structured sibling (candidate heard, matched aircraft hex/callsign/type, or none) is exposed so
   attribution does not parse the prefix back out. `identify_flight`'s behaviour is unchanged.

3. **`stt_proxy/air_numbers.py` (new, pure).** Extracts altitudes and headings from transcript
   text:
   - "flight level seven zero" / "level seven zero" → FL70 → 7,000 ft; "climb one three zero" after
     a climb/descend verb → FL130.
   - "two thousand (feet)", "two thousand five hundred" → 2,000 / 2,500 ft.
   - "heading two seven zero", "left/right heading …", "turn right three six zero" → 270° / 360°.
   - **Rejected on purpose:** QNH (the decoder writes "QNH" where "KLM" was spoken, 09-11
     finding), speed (no selected speed in ADS-B), frequencies ("one one eight four zero five",
     "one two three seven zero five", "one eight four zero five" = 118.405 / 123.705), runways
     ("runway one eight right", "one eight center").

4. **`stt_proxy/flight_attribution.py` (new).**
   - `echo_candidates(numbers, t, snapshots)`: aircraft whose selected altitude **changed to** a
     spoken altitude (±100 ft) or whose selected heading changed to a spoken heading (±5°)
     between t−10 s and t+60 s. An aircraft already set to that value before t−10 s does not
     count. The echo clue is only a single aircraft; two or more candidates means no clue.
   - `combine(callsign_clue, echo_clue) -> outcome` (pure):

     | callsign | echo | outcome | badge |
     |---|---|---|---|
     | X | X | flight X, confirmed | ✔✔ |
     | X | none | flight X, callsign only | ✔ |
     | none | X (unique) | flight X, autopilot only | ✔ |
     | X | Y ≠ X | **needs review** | ⚠ |
     | heard, not in ADS-B | any | unknown callsign, grouped by heard text | ? |
     | none | none / ambiguous | unassigned | — |

   - **Two stages.** Stage 1 runs when the transmission arrives and stores the callsign clue.
     Stage 2 runs once t+60 s plus one ADS-B poll interval has passed (so the poll covering
     the window's end has landed), computes the echo clue from the snapshot ring, and
     stores it. A single background loop (same style as the existing reapers) drives stage 2.
   - **Shadow mode.** The echo clue is computed and stored for every transmission regardless of
     `AIR_ECHO_ENABLED`. The setting (default **off**) only decides whether the echo clue counts
     towards the displayed outcome. With it off, the outcome is exactly the callsign clue.
     The outcome is stored when stage 2 runs, so the setting applies to transmissions attributed
     **after** it is switched on: rows recorded in shadow mode keep their callsign-only grouping
     (their echo clue stays stored and visible in the hover evidence). Switching it off again
     does not undo echo-based groupings already stored.
   - **A manual move always wins** and is shown with ✎.

5. **Storage (new tables in `stt_proxy/conversations.db`).** Same database, same WAL and
   `busy_timeout` rules as the archive.
   - `air_transmissions`: `id` (INTEGER PRIMARY KEY, stable across restarts; **not** the CH01
     `_chunk_seq`, which resets to 0), `t` (full ISO timestamp with offset, never bare
     `HH:MM:SS`), `channel`, `text`, `callsign_clue` (JSON), `echo_clue` (JSON, NULL until
     stage 2), `numbers` (JSON), `outcome_key`, `outcome_kind`.
   - `air_moves`: `id`, `transmission_id`, `from_key`, `to_key`, `t_moved`. Append-only; the
     effective assignment is the latest move, otherwise the computed outcome.
   - A flight key is the ICAO hex where known (callsign changes mid-flight are rare but
     happen), `heard:<text>` for unknown callsigns, and the literals `review` / `unassigned`.
   - Each transmission also stores the aircraft's state at that moment (altitude, selected
     altitude, heading), so history mode can show values as they were.

6. **Web app.**
   - `webapp/air_view.py` + routes: `GET /api/air/flights?from&to` (strips for a range; `live`
     = last 15 min), `GET /api/air/flights/{key}?from&to` (that flight's transmissions),
     `POST /api/air/moves` (auth-protected like the comment endpoints).
   - A new **Airband** tab beside Conversations in `static/index.html` / `app.js` / `app.css`,
     no build step, same conventions as the existing tabs.

## The page

Agreed mockup: `.superpowers/brainstorm/1308-1790241294/content/page-detail.html`.

- **Time bar:** Live (refresh every 15 s), Last hour, pick a day + hour. Right side shows the
  ADS-B feed status and the echo clue state ("recording, not shown" / "on").
- **Strips (left), sorted by last heard:** callsign, airline name, type, altitude → selected
  altitude, heading, transmission count, time since last heard. Live: a strip disappears 15 min
  after its last transmission. History: values as at that flight's last transmission. Below the
  flights: **⚠ Needs review**, **? unknown callsigns** ("2430 alfa", heard n×, not in ADS-B),
  **Unassigned**.
- **Conversation (right):** header with callsign, airline, type, registration, current state.
  One row per transmission: time, badge (✔✔ / ✔ / ⚠ / ✎), text, ▶ play, move-to dropdown
  listing flights heard within ±10 min plus Unassigned. Hovering a badge shows the evidence,
  e.g. "callsign DAL73 heard" / "DAL73 selected 6000 ft 8 s later".
- **Audio:** reuses `webapp/clips.py` and `/api/clips/{day}/{clip}` (time join within 2 s).
  Because `t` is a full timestamp, a transmission just after midnight looks in the right day's
  capture directory. No clip → no ▶, as on the Maas page.

## Out of scope (for this release)

- Explaining numbers ("FL70 = flight level, 7,000 ft") — the stored `numbers` and aircraft state
  are the basis for it later.
- Inferring a flight from neighbouring transmissions (readback after an instruction). Close-in-time
  is exactly what the 09-10 finding showed to be unreliable here; unmeasured, so not built.
- Any LLM in the attribution path.
- Map, routes (adsb.fi has no origin/destination), other airband channels.

## Error handling

- **ADS-B feed down:** stage 1 still stores the transmission (callsign clue will be none, as
  today); stage 2 records `echo_clue = {"status": "no_snapshots"}` rather than "no match", so a
  feed outage can never be scored as a measured negative. The time bar shows the feed state.
- **Proxy restart between stage 1 and stage 2:** on startup nothing special is needed: stage 2
  reads `adsb.snapshots_between`, which falls back to the daily log, and records
  `{"status": "no_snapshots"}` when the log doesn't cover the window either. That merges the
  spec's `missed_restart` into `no_snapshots`, since both mean "no evidence", which is the only
  distinction the measurement needs.
- **Move to a flight that has since left:** allowed; the target list is built from the archive,
  not the live cache.
- **Database busy:** 5 s `busy_timeout` as in the archive; a failed insert is logged and the
  transcription path is not blocked (the plugin still gets its text).

## Testing

TDD; units are pure where possible, clocks and snapshot sources are injected, no sleeps.

- **`air_numbers`:** positive and negative cases from real 09-10 transcripts. Negatives are the
  point: frequencies ("one eight four zero five", "one two three seven zero five",
  "one one eight decimal four zero five"), runways ("ILS runway one eight right"), QNH.
- **`echo_candidates`:** synthetic snapshot sequences — a single change to 7,008 matches; already
  set does not; two aircraft changing does not; a change at t+75 s does not; heading within ±5°.
- **`combine`:** every row of the outcome table, plus manual-move precedence and
  `AIR_ECHO_ENABLED` off ⇒ outcome equals the callsign clue.
- **Storage:** ids survive reopen; moves are append-only and latest wins; range queries; the
  restart re-check path.
- **Routes:** use the existing guard fixtures (no test may build the app over the real config or
  the real captures directory — see the control-panel incident).
- **Regression on the 09-10 hour:** replay the stored transcripts with
  `adsb-snapshots-2026-09-10.jsonl` through the new attribution with the echo clue off. It must
  reproduce today's per-transmission result **exactly**: 43.8% recall, 7 wrong. The echo clue
  cannot be replayed on that hour (no autopilot fields in its log).
- **Live check:** proxy running, SDR# on 121.200: transmissions appear under flights, ▶ plays,
  a move is stored and survives a page reload.

## Measuring the autopilot clue (switch-on gate)

After 3–5 days of recorded data, `server/bench_air_echo.py` reads `air_transmissions`,
`air_moves` and the snapshot logs. `AIR_ECHO_ENABLED` is switched on **only if all three hold**:

1. Where both clues exist, the echo clue agrees with the callsign clue in **≥ 95%** of at least
   **50** such transmissions.
2. On transmissions the operator moved by hand, the echo clue picked the operator's flight **at
   least as often** as the callsign clue did.
3. It attributes **≥ 10%** of what is currently Unassigned.

If any fails, it stays off and the result is recorded as a measured negative with its numbers.

## Success criteria for this release

- [ ] `adsb.py` keeps the autopilot fields and writes a daily snapshot log (deployed first).
- [x] Approach 4 transmissions are stored with a stable id, full timestamp, both clues and outcome.
- [x] The Airband tab shows strips and conversations, live and for a chosen past hour/day.
- [x] ▶ plays the captured clip where one exists.
- [x] Move-to works, is stored in `air_moves`, and survives reload and proxy restart.
- [x] 09-10 regression replay reproduces 43.8% recall / 7 wrong exactly with the echo clue off.
- [ ] Full test suite green; CI green.
- [x] `bench_air_echo.py` exists and runs on the recorded days (the switch-on decision itself
      comes after 3–5 days and is not a release blocker).
