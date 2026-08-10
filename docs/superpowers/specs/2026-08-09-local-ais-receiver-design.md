# Local AIS Reception — Design

**Date:** 2026-08-09
**Status:** approved, ready for implementation planning

## Why

Vessel identification depends on the AIS cache, and the cache has been frozen since
**2026-08-05 13:31 UTC**. aisstream connects, accepts the subscription, and delivers nothing.
The upstream issue describing exactly this symptom (`aisstream/aisstream#15`) has been **open
since 2026-03-13 with no maintainer response**, so this is a five-month-old unaddressed fault
on a free service, not a blip. Waiting is not a plan.

Every day the cache ages, identification degrades: positions go stale, new vessels are absent,
and `last_seen` becomes meaningless.

**Local reception is now proven.** On 2026-08-09, AIS-catcher v0.66 on the existing antenna
decoded ~11 messages from four distinct MMSIs in 15 seconds, on both channels A and B, at
signal powers of −10 to −15 dB. The earlier doubt came from a waterfall showing nothing, which
was never evidence: an AIS burst is 26.7 ms and renders as a single faint pixel row.

Local AIS is also a better fit than the feed it replaces. Vessels calling Maas Approach are
inside VHF range by definition — exactly what a local receiver covers — with second-level
freshness rather than cloud latency.

## Scope

**In:**
- A second RTL-SDR dongle running AIS-catcher as an external process.
- A UDP listener in the proxy that consumes AIS-catcher's decoded JSON.
- A provider-neutral recorder in `ais.py` that both aisstream and local feed.
- An MMSI index and a pending-position area, so nameless position reports are usable.
- A configurable geographic filter on what enters the cache.
- Per-source health counters exposed for later consumption.

**Out (deliberately):**
- **AISHub.** Free only for contributors meeting ≥10 vessels and ≥90% uptime over a rolling
  7 days — which effectively means running ~24/7, and is unenforceable-to-verify from their
  published terms (they do not say whether it is re-checked after approval). Cannot even be
  applied for until the receiver has 7 days of history. A separate project, plausibly wanting
  a Raspberry Pi so uptime does not depend on the desktop.
- **Proxy supervision of AIS-catcher.** It is started from `start-all.bat`, exactly as
  whisper.cpp is. Webapp-driven start/stop belongs to the webapp project.
- **The webapp.** Separate project. This design only ensures the health data it will need is
  exposed.
- Any change to the name matcher, the resolver, or the identification pipeline.
- The `-X` community feed at aiscatcher.org (noted as a future alternative to AISHub).

## Architecture

```
  dongle A ── SDR# ──────────────── plugin ── audio ──▶ proxy (STT)
  dongle B ── AIS-catcher ── UDP JSON ──────────────▶ proxy (ais_local)
                                                          │
                                            record(fields, source=...)
                                                          │
  aisstream ── websocket ── _process_ais ─────────────────┘
                                                          ▼
                                                   _vessel_cache
```

Two dongles on one PC is routine; the requirement is a **unique serial per dongle** (set with
`rtl_eeprom`, one plugged in at a time) and **explicit selection by serial** in each program.
Index ordering is not stable across reboots and would let AIS-catcher seize SDR#'s dongle.

AIS-catcher has a native Windows binary, so no WSL USB passthrough is involved.

### Invocation

```
AIS-catcher -d <serial-B> -o 5 -u 127.0.0.1 10110
```

- `-o 5` — JSON Full. **AIS-catcher does all AIVDM decoding**: 6-bit unpacking, multi-part
  reassembly, checksums. We parse JSON. This is the single largest risk reduction in the
  design — we are writing an adapter, not a decoder.
- `-u` — one UDP datagram per decoded message.
- `-d <serial>` — not `-d:<index>`.

## Components

### `stt_proxy/ais_local.py` (new)

A UDP listener thread. Parses AIS-catcher JSON, maps fields, calls the shared recorder. Holds
no cache state.

### `stt_proxy/ais.py` (refactor)

Extract a provider-neutral `record(fields, *, source)` holding the merge logic. `_process_ais`
becomes a thin aisstream adapter over it; `ais_local` gets its own adapter.

**One merge implementation is the point.** The merge is where the subtle bugs lived: static
messages wholesale-replacing position data left 25% of vessels in the labelled conversations
with no position at all, until the MERGE-never-replace fix. Two providers writing the cache
through two code paths would be two chances to get that wrong, with only one covered by tests.

## The naming problem

**The cache is keyed by vessel name. Raw AIS position reports do not carry one.**

aisstream enriched every position report with `MetaData.ShipName`. Raw AIS does not: message
types 1/2/3 carry MMSI and position only; the name arrives separately in type 5, roughly every
6 minutes. The 2026-08-09 sample shows exactly this — MSG 1 from 244583000 with no name, MSG 5
from 246096000 with one.

The current handler drops nameless position reports. Correct for aisstream; it would silently
discard most local position data.

Two additions:

- **`_mmsi_index: dict[str, dict]`** — MMSI → the *same entry object* already in
  `_vessel_cache`. Lets a nameless position report find its vessel once a static message has
  named it. Also retires `match_by_mmsi`'s linear scan over ~8,600 entries.
- **`_pending_positions: dict[str, dict]`** — positions for MMSIs not yet named, flushed into
  the entry when the name arrives.

Pending vessels are deliberately **not** stored under a synthetic `MMSI:2445...` key in
`_vessel_cache`: the fuzzy name matcher iterates those keys, and junk keys would become
candidates for name matching.

## Merge rules

- **Position: newest observation wins.** Store `position_at` with the fix; a write applies only
  if it is newer. In practice local is real-time and wins essentially always — this delivers
  "local wins" without its pathological case, where a vessel heard locally two hours ago and
  now out of range keeps a stale position over a fresh remote one.
- **Static (name, callsign, IMO, dimensions, draught, destination): last write wins**, keeping
  today's fill-and-update behaviour. It is the vessel's own most recent statement about itself.
- Each entry records `source` — which provider last wrote it.

## Geographic filter

`AIS_LOCAL_MAX_KM`, a radius from Maas Center (`_MAAS_CENTER = (52.02, 3.88)`), **default
40 km**, applied in the recorder so one rule governs every source.

A vessel enters the cache only once a position inside the radius arrives. Static messages carry
no position, so they wait in the pending area until a position admits or rejects the vessel.

**The purpose is pool reduction, not excluding any particular port.** An earlier draft of this
spec claimed a 40 km radius would exclude Scheveningen. That was wrong, from a mis-typed
reference point. Measured against the real `_MAAS_CENTER`:

| vessel | distance |
|---|---|
| SCH123 ZEELAND (in Scheveningen harbour) | **27.8 km** |
| VARNEBANK (in Scheveningen harbour) | **27.6 km** |
| ZIRFAEA | 69.5 km (cached position — live it is 27.4 km, see below) |

**Scheveningen is 27.7 km from Maas Center, so no radius separates it cleanly** — vessels
calling Maas Approach occupy the same distance band. Any boundary tight enough to exclude
Scheveningen also risks excluding inbound traffic. This is a genuine limitation, not a tuning
problem.

What the radius *does* buy is a much smaller candidate pool, which is where wrong-match risk
lives (the documented NORDIC SIRA / NORDIC SAGA failure). Over the 7,205 cached vessels that
carry a position:

| radius | vessels admitted |
|---|---|
| 20 km | 349 (4.8%) |
| 30 km | 654 (9.1%) |
| **40 km (default)** | **1,116 (15.5%)** |
| 60 km | 2,219 (30.8%) |
| 100 km | 5,878 (81.6%) |

40 km cuts the pool by 85% while keeping a generous margin for vessels that call from further
out. **It is a starting point, not a finding.** Too tight loses recall; too wide loses
precision. `bench_identify.py --labels ... --resolve --repeats 3` reports both with a spread,
so tune against it rather than trusting the default. 20–30 km is the obvious next thing to try.

### Reception range: measured at ~4 km, and it blocks the feature

An earlier draft of this spec said "reception range is not the constraint", citing a decode of
ZIRFAEA at 69.5 km. **That figure was wrong**: 69.5 km was ZIRFAEA's position in the *frozen
2026-08-05 cache*, not where it was when we heard it. Measured live on 2026-08-09 it sits at
27.4 km, with everything else.

The question was then measured properly on 2026-08-10, with the radius filter off
(`AIS_LOCAL_MAX_KM = 0.0`) plus a distance histogram — the filter can only report what it
admitted, so it cannot answer "did we hear the right water". A 10-minute capture: 391 messages,
44 ignored, 0 malformed, 17 named vessels and 11 unnamed.

Distances from `_MAAS_CENTER`:

| band | vessels |
|---|---|
| inside 15 km (the approach area proper) | **0** |
| 15–25 km | **0** |
| 25–30 km | 15 |
| 30–40 km | 1 (GPO AMETHYST, 31.6 km) |
| beyond 40 km | 0 |

Closest contact 27.4 km (ZIRFAEA).

The receiver sits at **52.111188 N, 4.292962 E** (Den Haag) — the coordinate every range
calculation needs. From there: Maas Center 30.0 km, Euro/Maas approach 26.5 km, Hoek van
Holland 18.9 km, **Scheveningen harbour 2.2 km**. Overlay that on the histogram and the answer
falls out: everything heard lay 27.4–31.6 km from Maas Center while the antenna is 30.0 km from
Maas Center, so **every contact was within ~2–4 km of the antenna**, and the tight band is
Scheveningen harbour. Nothing was heard beyond ~4 km even though open sea with heavy traffic
starts 19 km west — a hard range limit, not "that is where the transmitters happen to be".

**The same dipole hears voice at 30 km and AIS at 4 km** — ~8×, ~18 dB. That is consistent with
FM voice staying intelligible well below the SNR at which 9600-baud GMSK packet decoding
collapses: no partial credit, 26.7 ms bursts, CRC-checked. Nothing is faulty.

**Consequence: as installed, local AIS cannot help identification at all.** The vessels calling
Maas Approach are simply not in the received set, and no filter tuning changes that — the
geometry is that the filter admits a ring around Maas Center while the antenna hears a disc
around the operator, and today those barely overlap. This bounds the feature's usefulness, not
its correctness: the pipeline is proven end to end (391 messages, 0 malformed, names, callsigns
and positions all decoded).

Levers, cheapest first:

1. **AIS gain, never tuned.** SDR#'s 36.4 dB was set for *voice* and does **not** apply here —
   AIS-catcher has its own gain and `start-all.bat` never set it, so it runs on auto. Sweep with
   `-gr TUNER 0.0-50.0 RTLAGC off`, measure with `-M D` (per-message signal power and ppm), and
   consider `-a` (tuner bandwidth) against broadcast-FM overload.
2. **Antenna height.** Reaching 30 km against a 10 m masthead needs **~17 m** of receive height
   (horizon ≈ 4.12·(√h_rx + √h_tx) km). An external antenna plus splitter is the planned fix.
3. **Re-centring or widening the radius filter** — which on its own changes nothing, because the
   traffic is not being received in the first place.

**Behaviour change to note:** applying this in the recorder governs aisstream too, which today
accepts anything in the 205 × 210 km box. That is an improvement, and aisstream is dead, but it
is a change.

## Failure handling

- **Per-provider silence watchdog.** AIS-catcher not running is indistinguishable from a quiet
  channel. Reuse the `_watch_silence` pattern already built for aisstream, and keep its
  distinction between "never received anything" and "went quiet mid-stream" — the distinction
  that made the aisstream fault diagnosable at all.
- **Bind the UDP port without `SO_REUSEADDR`, and fail loudly if taken.** `ThreadingHTTPServer`
  sets `allow_reuse_address`, and a second proxy once bound alongside the first, silently took
  the port, and left the original running as a zombie. A listener that quietly binds a port
  someone else owns is that bug in a new place.
- **Malformed JSON logged rate-limited, never fatal**, mirroring `_report_unrecognised_frame`.

## Observability

Per-source counters — messages seen, last message at, vessels contributed, vessels rejected by
the radius filter — exposed through the existing `/api/ais-cache` route. This is the seam the
webapp project reads for provider health, so it needs no proxy change later.

## Testing

- **Recorder merge rules**, as pure unit tests: static-then-position from different sources; a
  stale position correctly rejected; a pending position flushed when the name arrives; the MMSI
  index staying consistent with the name-keyed cache; a vessel outside the radius never
  entering.
- **The adapter**, against a **real captured AIS-catcher JSON sample**. Capturing that sample is
  the first implementation step, so field names are mapped from reality rather than assumption.
- **The listener**, over loopback UDP: receives, handles malformed input, reports silence,
  refuses a taken port.
- No network, no dongle, and no AIS-catcher process in the test suite.

## Success criteria

1. With AIS-catcher running, the cache gains vessels with fresh `last_seen` and positions inside
   the radius, and `/api/ais-cache` shows a non-zero local message count.
2. Nameless position reports are retained and attributed once the vessel's static message
   arrives — verifiable as a vessel that has both a name and a position from local only.
3. Stopping AIS-catcher produces a loud, distinguishable silence report rather than silent
   staleness.
4. The full suite passes, with no network or hardware dependency.
5. `bench_identify.py --resolve --repeats 3` runs against a locally-populated cache, giving a
   baseline for tuning `AIS_LOCAL_MAX_KM`.

## Configuration

| Setting | Default | Meaning |
|---|---|---|
| `AIS_LOCAL_ENABLED` | `on` | Enable the UDP listener |
| `AIS_LOCAL_UDP_PORT` | `10110` | Port AIS-catcher pushes to |
| `AIS_LOCAL_MAX_KM` | `40` | Radius from Maas Center for cache admission |
| `AIS_SILENCE_WARN_SEC` | `60` | Existing; now applied per provider |

## Things to watch, not solve

- **Cache growth.** Local AIS adds vessels continuously and there is no eviction. 8,600 entries
  is nothing; if it reaches tens of thousands it may want pruning. Watch rather than build a
  reaper on speculation.
- **Antenna.** Two dongles need two feeds — a second antenna or a VHF splitter (~£15, a couple
  of dB). Reception was proven on the existing antenna with SDR# closed; sharing it is a
  hardware question, not a software one.
- **Coverage asymmetry.** The Maas Approach shore station is high-sited and high-power, so it is
  received far more easily than the 12.5 W vessels talking to it. Some vessels heard on Ch 01
  may still be out of AIS range, which the radius filter cannot fix. The 69.5 km decode is
  encouraging but is one observation, not a coverage survey — worth re-checking against a
  longer run before relying on it.
- **Local traffic is not necessarily relevant traffic.** The antenna hears what is near *it*,
  and a busy nearby harbour (Scheveningen, 27.7 km) contributes vessels that will never appear
  on Ch 01. The radius filter cannot separate them. If the pool still proves too noisy after
  tuning, the next lever is a hand-drawn polygon over the approach channels — more precise, but
  brittle and hand-maintained, so only if measurement justifies it.
