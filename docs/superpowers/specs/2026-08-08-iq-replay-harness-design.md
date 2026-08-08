# IQ replay harness — design

## Problem

Every receiver setting is baked into the audio before anything this project controls sees it.
The plugin is an `IRealProcessor` (`SDRSharp.SttPlugin/AudioProcessor.cs:12`), so it taps
**demodulated audio at 48 kHz**, downstream of SDR#'s entire receive chain — downstream of the
channel filter, the squelch, the demodulator and any audio-stage plugin. `docs/design-notes.md`
documents decoder settings exhaustively and says **nothing** about the receiver.

The specific suspicion, raised 2026-08-07: bandwidth is set to **12.5 kHz**, but marine Ch 01
is a 25 kHz channel at ±5 kHz deviation, so Carson gives `2×(5+3) ≈ 16 kHz` occupied. At 12.5
the outer sidebands are clipped, distorting exactly the loud modulation peaks where consonants
live — and consonants are what separate "Berge Townsend" from "Berkey Fountain". Nobody has
measured it.

The reason it has never been measured is cost. A receiver change normally needs a fresh
capture **and** fresh hand-verified references, and reference verification cost hours on
2026-08-07 alone. Two settings compared this way is two full days of work, and the comparison
is still unpaired: different traffic on different days, so any difference is confounded with
whatever ships happened to call.

## Approach

Record raw IQ **once**, then replay that one recording through different demodulator settings.
Identical RF in, one variable changed, and the references only need verifying once because
every arm decodes the same transmissions. This is the same paired design as
`bench_prompt_ab.py`, one stage earlier in the chain.

Demodulation happens **offline in Python**, not in SDR#. Driving the real receiver would be
authoritative, but each arm would run in real time, GUI automation is fragile, and no IQ-file
*source* could be found in `D:\SDR\SDRSharp` — only `BasebandRecorder`, which writes. Offline
demod is fully automated, faster than real time, deterministic, and sweeps as many settings as
we like. The trade is that it answers "which setting is better" rather than "what does SDR#
do"; that is the question actually being asked.

## Architecture

One new tool. Everything downstream already exists and is already tested.

```
baseband .wav (250 kSPS I/Q, ~60 min)
        │
        ▼
   iq_replay.py  ── per arm ──►  captures-style dir of NNNN_sent.wav
        │                        (identical clip IDs in every arm)
        ▼
   bench.py --captures <arm-dir> --references references-iq.txt  ──► arm.json
        │
        ▼
   bench_prompt_ab.py bw12k=a.json bw16k=b.json …  ──► paired WER + bootstrap CI
```

`bench.py` and `bench_prompt_ab.py` are used **unchanged**. `bench.py::discover_clips` already
globs `*_sent.wav` and derives the clip ID from the filename, so writing that layout gets the
whole existing scoring stack for free — including the bootstrap confidence interval, which
exists precisely because `identify.py` records ~1 point of pooled-WER movement between
byte-identical runs, so a bare delta of a point or two carries no information on its own.

## `iq_replay.py` stages

1. **Read** the SDR# baseband wav — interleaved I/Q, rate and centre frequency from the header
   and filename.
2. **Mix** the channel to DC (`exp(-j2πf_offset·t)`); the VFO sits offset from centre.
3. **Channel filter** — low-pass at `bandwidth/2`. *Variable under test.*
4. **FM discriminate** — `angle(x[n]·conj(x[n-1]))`.
5. **De-emphasis** (750 µs, configurable) and resample to **48 kHz**.
6. **Squelch** — optional power gate. *Variable under test.*
7. **Plugin DSP chain** — DC block, 150 Hz high-pass, decimate to 16 kHz, normalise.
8. **Cut and write** clips at fixed boundaries.

Stages 5 and 7 look wasteful together — resample up to 48 kHz only to decimate to 16 kHz —
and the detour is deliberate. 48 kHz is exactly where the plugin taps the audio, so passing
through it means stage 7 is the *same* chain production runs, operating on the *same* rate.
Going straight to 16 kHz would be cheaper and would quietly stop measuring what production
does.

## Dependencies

`scipy` is added to `server/requirements.txt` for `firwin`, `resample_poly` and `lfilter`.
The project's dependency list is deliberately lean (5 packages before this), so this is a
considered exception: hand-rolled resampling and decimation anti-aliasing are a classic source
of subtle aliasing, and aliasing here would corrupt the very measurement the harness exists to
make — in a way the tests could plausibly miss, since both arms would be corrupted and only
their *difference* is reported. Wav I/O stays on the stdlib `wave` module plus numpy; no
`soundfile`.

## Fixed segmentation

This is the part that makes the whole design work, and the part most easily got wrong.

"References verify once" only holds if every arm produces the *same clips*. It does **not**
hold if each arm runs its own VAD: bandwidth and squelch change what the VAD cuts, boundaries
drift, and clip `0042` stops meaning the same transmission in every arm. `bench_prompt_ab.py`
pairs on `clip_id`, so drifting boundaries would silently compare different audio.

Therefore: **one segmentation pass, run once against a reference arm, producing a list of
(start, end) timestamps.** Every arm is cut at those fixed timestamps. The list is a plain
file, so it can be inspected and corrected by hand.

The reference arm for segmentation and for reference-listening is the **widest** bandwidth
tested, since it preserves the most information and is the easiest to transcribe by ear. The
reference text is a property of the transmission, not of the arm, so this introduces no bias
toward any arm under test.

## Fidelity of the post-demod chain

Stage 7 reproduces what the plugin does before sending, because `_sent.wav` — the file
`bench.py` scores — is **post-plugin-DSP**, not raw demodulated audio. Porting
`BiquadFilters`, `Decimator` and `Normalizer` from C# to Python keeps the harness's clips the
same shape as the existing corpus, so its absolute WER is comparable to the 2026-08-07
numbers.

**The risk, accepted deliberately:** a port can diverge silently from the C# original. It is
mitigated by pinning the Python against known C# behaviour in tests, and bounded by the fact
that arm-vs-arm comparisons stay valid regardless — every arm goes through the *same* chain,
so a divergence shifts all arms together and cannot manufacture a difference between them.
Only the comparison to historical absolute WER depends on the port being right.

## Testing

Synthetic IQ throughout, so the harness is fully testable before any recording exists.

- Generate NFM from a known audio signal at known deviation; assert the demodulator recovers
  it (correlation / SNR against the source).
- Assert a signal wider than the channel filter comes out measurably distorted, and that a
  25 kHz filter beats a 12.5 kHz one on the same wide input. **The harness must be able to
  detect the effect it exists to measure**; a test that only proves it runs is worthless here.
- Assert segmentation boundaries are identical across arms — the property the whole design
  rests on.
- Assert squelch opens and closes at its threshold, and that squelch-off preserves
  transmission openings that squelch-on clips.
- Pin the ported plugin DSP against known C# behaviour.

## Capture parameters

**~60 minutes at 250 kSPS**, roughly 3.6 GB. That is ~15× wider than the channel needs, so
every bandwidth arm up to ~100 kHz is reachable from the one recording. An hour of Ch 01
traffic yielded roughly 15–20 conversations on 2026-08-07, enough to move a pooled WER figure.

**Verify before committing an hour to it.** Some RTL-SDR devices drop samples below 900 kSPS.
Record two minutes first and check for dropped samples; a capture with gaps makes everything
downstream meaningless, and the failure is silent.

## What this cannot answer

Stated here and in the tool's docstring, so that silence is never mistaken for a null result.

- **RF gain.** Applied before the ADC, so it is baked into the recording. Sweeping it needs a
  fresh capture per setting, and those can never be paired, because the traffic differs. The
  right instrument for that question is an SNR/noise-floor meter, not a WER A/B.
- **The SDR# audio-NR plugins** (`AudioProcessor`, `AudioEqualizer`). Those are other
  plugins' algorithms, downstream of the tap point.

## Success criteria

1. Synthetic-IQ tests pass, including the one proving the harness can distinguish a 12.5 kHz
   filter from a 25 kHz filter on the same input.
2. Segmentation is provably identical across arms.
3. A real capture replays through at least three bandwidth arms and one squelch arm, producing
   paired `bench.py` results.
4. `bench_prompt_ab.py` reports a WER difference with a bootstrap confidence interval, so the
   bandwidth question gets a number and an honest uncertainty rather than an opinion.
