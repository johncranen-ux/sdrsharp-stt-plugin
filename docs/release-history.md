# Release history

How this project got from "transcribe the radio" to what it is now.

Every release below is a real point in the git history, tagged retroactively when the
repository was opened up. The versions are a narrative device rather than a record of things
that were separately published — there were no public releases before `v1.0.0`.

The through-line is measurement. Almost every version here exists because something was
measured, and several exist because a measurement *contradicted* what was believed. Those are
called out, because they are the more useful half of the story. The full numbers live in
[design-notes.md](design-notes.md); this file is the map.

---

## v0.1 — It works at all

**2026-05-30** · `a00aa47`

The first working plugin. Audio out of SDR#, voice activity detection in the plugin so silence
is never uploaded, a Python proxy in front of a local `whisper.cpp` server on an AMD RX 7900 XTX
under WSL2 with ROCm, and text back into a panel inside SDR#.

The architecture chosen here survived everything that followed: **the plugin only ever talks to
the proxy.** Backends, correction passes, vessel matching and an entire web control panel were
added later without the plugin needing to know, and tuning stayed a Python restart rather than a
plugin rebuild and an SDR# restart.

Then it sat untouched for two months.

## v0.2 — Learning to measure

**2026-07-28** · `622d3a0`

The accuracy work started and immediately became work on *how to know* whether accuracy had
improved: a benchmark harness, hand-transcribed reference clips, word-level alignment, and
substitution-frequency analysis to find which errors were worth fixing at all.

What that turned up:

- **The maritime prompt was the single largest lever** — roughly 9–10 WER points over no prompt.
  A keyword-list prompt was tried first and rejected: it primes Whisper to echo the list back
  verbatim over noisy or silent audio.
- **A domain correction pass** built from measured substitution frequencies rather than intuition
  (Mass/Mars/March Approach → Maas Approach, *call sign* → Callsign, *draft* → draught,
  *boys* → buoys) moved pooled WER from **41.6% to 35.9%**.

That second number came with a caveat that set the tone for everything after it: of the 5.7
points, about **4.2 came from rules that already existed** and only ~1.3 from the rules added in
that pass. The headline figure was real; the attribution was not, until someone checked.

## v0.3 — The GPU stops being a requirement

**2026-07-30** · `eb4ebd3`

Four days went to the GPU wedging mid-inference. It was chased properly — kernel event codes
rather than request failures, a crash-rate harness, a power-limit experiment, a change-history
audit — and root-caused to a **driver-level AMD/ROCm fault this project could not fix**. A warm
reboot did not clear it. Nothing in the codebase had introduced it. A watchdog was shipped to
restart the wedged server instead.

That is what prompted trying Groq's hosted Whisper, and the measurement that changed the
project's shape:

| Run | Pooled WER |
|---|---|
| whisper.cpp `beam5_prompt`, raw | 0.416 |
| **Groq `whisper-large-v3`** | **0.411** |

**Losing local beam search cost essentially nothing.** Groq became the default backend and
whisper.cpp stayed switchable. From this release on, *a GPU buys privacy, not accuracy* — which
is the single thing that makes this repository useful to anyone who does not own a 24 GB card.

The same week the repository was made fit to publish: MIT licence, third-party attribution, a
security policy, CI, and a hard rule that **no transcript of received traffic is ever
committed** — not even a synthetic one. The proxy was split from one file into the `stt_proxy`
package.

## v0.4 — Identifying who is talking

**2026-08-08** · `65138d9`

Transcription became identification: matching spoken vessel names and callsigns against a live
AIS feed to say *which ship* was on the air.

The design decisions all point the same way — **the pipeline must never invent a vessel**:

- AIS hints may only correct the spelling of a name that was actually said. They are never a
  source of names. A transmission naming nobody returns nobody, however many ships are nearby.
- A callsign is accepted only if it can be read back out of the transmission. An invented
  callsign looks up to a real ship, which is worse than no callsign at all.
- **Identity is settled after a conversation ends, not during it.** Conversation context supplied
  during transcription bleeds into the transcription itself — that was measured — so a garbled
  opening call gets resolved by a clearer later turn instead.
- A partial callsign half-lost to STT still identifies a ship when the surviving characters fit
  exactly one vessel *and* a spoken name agrees.

And the measurement that mattered most: the system had been **scoring itself at 98.5% precision
and 97.7% recall.** Hand-labelled against real traffic, the true figures were **68.0% and
51.7%**. The scoring was measuring agreement with itself. Everything after this point is scored
against hand-verified labels.

## v0.5 — Measuring the receiver, not just the software

**2026-08-09** · `0e36473`

An IQ replay harness: record baseband once, then replay the same radio through different
receiver settings and score each as its own arm. It reads SDR#'s RF64 recordings, demodulates
NFM with a sweepable channel bandwidth, and segments on RF channel power so every arm gets
identical clip boundaries.

Its most valuable output was an **error bar**. Re-running the identical arm three times gave a
spread of **2.9 precision points and 0.0 recall points** — so precision deltas under about three
points are noise, and recall is the metric to judge an A/B on. Several earlier "improvements" did
not survive it.

The harness also refuses to present a run where every clip failed as though it were a result,
which is how one earlier conclusion had been reached.

## v0.6 — Correcting a transmission from the conversation around it

**2026-08-10** · `1e43bf1`

A second LLM pass that re-reads each transmission with the surrounding exchange as context, so a
name mangled in one turn can be fixed from a clearer turn nearby — without that context
contaminating the transcription itself.

Shipped behind a flag, measured, and only then turned on by default. What was *rejected* is the
more interesting half:

- **Sonnet was tried against Haiku and Haiku was kept**, compared where it mattered rather than
  only where it was easy to measure.
- **Curated few-shot examples measured worse than synthetic ones** and were dropped.
- The correction is stored *beside* the verbatim text, never over it, and a reply that breaks its
  contract is rejected before it reaches storage.

## v0.7 — A vessel source that is actually reliable

**2026-08-13** · `9a29a3c`

The aisstream.io feed went silent for eight days — proven external by elimination, since a fresh
API key did not help. AISHub was added as a polled alternative, with `AIS_SOURCE` choosing
between them and a single merge point so two providers cannot get the cache wrong in two
different ways.

The cutover carried a measurement worth more than the source change itself. Narrowing the polling
box from the wide inland-inclusive one to a **sea box** — Maas Approach works ships at sea
entering or waiting to enter, never river traffic already inside — cut duplicate vessel-name
groups from **685 to 43**, a 94% reduction in exactly the collisions that cause misidentification.

This release also narrowed *who could use the feature*, which was not noticed at the time: AISHub
issues credentials only to stations that contribute their own AIS feed. That was corrected in
v1.0.

## v0.8 — A station you can run from a phone

**2026-08-20** · `3b2d0a9`

A web control panel replaced the batch file as the way to run the station: settings with a
validated catalogue, a process supervisor owning detached children and their logs, a dashboard
with per-feed liveness, and screens for conversations, vessels and settings.

Security was designed in rather than added: argon2id password hashing in its own file, sessions
and CSRF on every mutation, secrets that can be set but never read back, and a **refusal to start
at all** when bound to a network-reachable address without a password — failing at startup, with
no socket ever opened.

Two diagnostic habits from this period are worth noting. A 19-second stall in the AIS cache was
root-caused to **loopback TCP losing a tail** — a machine fault, not project code — and a probe
was built to reproduce it on demand. And the AIS ship-type table turned out to have been wrong
for its whole life (codes 60–99 shifted by ten), feeding not just the display but the resolver.

## v1.0 — Fit for other people

**2026-08-25** · `master`

A SQLite conversation archive so nothing is lost to the rolling display window, operator comments
and verdicts recorded against conversations, and an export turning reviewed comments into a
ground-truth file the identification benchmark can score against — closing the loop from
"listening" to "measuring".

Then the work of making it usable by someone who is not its author:

- **`AIS_SOURCE` now defaults to `aisstream`, not `aishub`.** AISHub is the better source, but it
  issues credentials only to contributing stations — a second receiver, a seaward antenna, a 24/7
  uptime bar. Defaulting to it meant anyone without that hardware saw `AIS feed: disabled` on
  first run, which reads like a broken install.
- **The aisstream bounding box was still the old wide one, and was not configurable.** Only
  AISHub had been moved to the sea box in v0.7, because only AISHub was in use. Shipping
  aisstream as the default without fixing this would have put every new user on the box measured
  as carrying 16× the name collisions.
- **The silence warning is armed again.** It had been muted during the eight-day outage, where it
  fired roughly 8,600 times and drowned output worth reading. aisstream was measured delivering
  again on 2026-08-25, so the instrument goes back on — and it matters more now, being the only
  thing that distinguishes a quiet channel from a feed that connected and then stopped.
- **A prebuilt release archive**, so neither the SDR# SDK nor the .NET toolchain is needed to
  install the plugin.

---

## What the arc actually shows

Read as a whole, the interesting pattern is not the features. It is how often a believed result
did not survive being measured properly:

| Believed | Measured |
|---|---|
| Identification was ~98% accurate | 68.0% precision, 51.7% recall against hand labels |
| The new correction rules bought 5.7 WER points | ~1.3; the rest predated them |
| A local GPU was needed for accuracy | Groq matched it; the GPU buys privacy |
| An A/B moving precision 3 points meant something | The noise floor is 2.9 points |
| The GPU crashes were caused by a code change | Nothing in the history introduced them |
| Lowering the identification cutoff would help | −11 precision points, and non-monotonic |
| The ship-type table was correct | Codes 60–99 were shifted by ten, and fed the resolver |

The design notes record what was tried and rejected alongside what shipped, with numbers, for
exactly this reason.
