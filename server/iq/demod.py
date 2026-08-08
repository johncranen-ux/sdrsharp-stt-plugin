"""IQ -> audio: mix, channel-filter, discriminate, de-emphasise, resample.

The channel filter width is the variable the replay harness exists to sweep. Everything else
here is held fixed so that a difference between two arms can only have come from the setting
under test.

scipy is used for filter design and rate conversion at this stage -- unlike plugin_dsp, which
must transcribe the plugin rather than improve on it, this stage has no production
counterpart to match and correctness matters more than fidelity to anything.
"""

from __future__ import annotations

import numpy as np
from scipy import signal

DEFAULT_AUDIO_RATE = 37_500.0   # what SDR# actually feeds the plugin on this setup

# Channel filter design. Linear-phase FIR, not IIR: SDR# very likely implements its channel
# filter as a linear-phase windowed-sinc FIR (as most SDR software does), and an IIR filter's
# non-linear phase / non-constant group delay is a distortion mechanism the real receiver
# probably does not have. Using it would inflate the measured penalty of a narrow bandwidth
# setting and bias the whole experiment toward the answer the harness exists to check for --
# the worst kind of measurement error, because it would look like a result. See
# task-3-report.md fix-round-1 for the elliptic-IIR attempt and why it was rejected.
#
# Tap count matters and was measured, not guessed: at 255 taps (the brief's original count),
# firwin's transition band is wide enough that the 12.5kHz filter's stopband leakage beyond
# 6.25kHz is comparable to the 25kHz filter's own leakage beyond 12.5kHz, and the *narrow*
# arm came out measuring cleaner than the *wide* arm (backwards) on every signal tried. By
# 511 taps the transition is sharp enough that the direction is consistently correct on every
# signal tried (511-2047 all agree, so 511 is the tap count where the direction stops being
# an artefact of an under-resolved filter, not a magic number chosen to hit a target ratio).
_FIR_TAPS = 511
_FIR_WINDOW = "blackmanharris"


class Demodulator:
    """Stateful NFM demodulator, so a capture can be processed in blocks.

    An hour of 250 kSPS IQ is 900M complex samples -- 14.4 GB as complex128 -- so the real
    input is never held in memory. Every stage that has memory carries it across blocks:
    the mixer's phase, the channel filter's zi, the de-emphasis zi, the discriminator's
    previous sample, and the resampler's input overlap. Restarting any of them per block
    would stamp an artefact at every boundary, and since all arms share the block size, the
    artefact would be common-mode and invisible in the A/B while still degrading every arm.
    """

    def __init__(self, iq_rate: float, bandwidth_hz: float, offset_hz: float = 0.0,
                 audio_rate: float = DEFAULT_AUDIO_RATE,
                 deemphasis_us: float | None = 750.0):
        nyquist = iq_rate / 2.0
        if bandwidth_hz / 2.0 >= nyquist:
            raise ValueError(f"bandwidth {bandwidth_hz} Hz exceeds the recording's Nyquist")

        self.iq_rate = iq_rate
        self.audio_rate = audio_rate
        self.offset_hz = offset_hz

        # Channel filter -- THE variable under test. Half the occupied bandwidth either side
        # of DC, so `bandwidth_hz` means what SDR#'s bandwidth control means. Linear-phase
        # FIR: see the module-level comment on _FIR_TAPS for why, and for why 511 taps.
        self._taps = signal.firwin(_FIR_TAPS, (bandwidth_hz / 2.0) / nyquist,
                                   window=_FIR_WINDOW)
        self._zi_ch = np.zeros(len(self._taps) - 1, dtype=np.complex128)

        if deemphasis_us:
            alpha = 1.0 / (1.0 + iq_rate * deemphasis_us * 1e-6)
            self._de_b, self._de_a = [alpha], [1.0, -(1.0 - alpha)]
            self._zi_de = np.zeros(1)
        else:
            self._de_b = None

        from math import gcd
        g = gcd(int(audio_rate), int(iq_rate))
        self._up, self._down = int(audio_rate) // g, int(iq_rate) // g

        self._n = 0                                   # samples mixed so far, for phase
        self._prev_iq = np.zeros(0, dtype=np.complex128)
        # resample_poly has no zi/streaming state of its own -- see process() step 5 for why
        # this needs two separate buffers rather than one shared overlap.
        self._history = np.zeros(0)                   # already-emitted tail, kept for context
        self._reserve = np.zeros(0)                   # not-yet-emitted audio, needs a lookahead margin first
        # Must be a whole number of decimation phases (a multiple of self._down), or the
        # resampler's output grid shifts between blocks and the concatenation develops a
        # slow drift. Also must exceed resample_poly's Kaiser-windowed filter half-length in
        # the upsampled domain, or the context/lookahead margins don't fully suppress its
        # per-call edge transient. 64 decimation phases is generous margin for either.
        self._overlap = self._down * 64

    def process(self, block: np.ndarray) -> np.ndarray:
        block = np.asarray(block, dtype=np.complex128)
        if len(block) == 0:
            return np.zeros(0)

        # 1. Mix to DC, with the phase continuing from wherever the last block ended.
        if self.offset_hz:
            t = (self._n + np.arange(len(block))) / self.iq_rate
            block = block * np.exp(-2j * np.pi * self.offset_hz * t)
        self._n += len(block)

        # 2. Channel filter, state carried. lfilter with zi carried reproduces whole-signal
        #    output exactly (max abs diff 0.0) -- the measured fact this streaming design
        #    relies on for test_streaming_matches_one_shot.
        block, self._zi_ch = signal.lfilter(self._taps, 1.0, block, zi=self._zi_ch)

        # 3. FM discriminator: instantaneous frequency is the phase step between samples.
        #    The previous block's last sample supplies the first difference.
        joined = np.concatenate([self._prev_iq, block])
        self._prev_iq = block[-1:]
        audio = np.angle(joined[1:] * np.conj(joined[:-1]))

        # 4. De-emphasis, state carried.
        if self._de_b is not None:
            audio, self._zi_de = signal.lfilter(self._de_b, self._de_a, audio, zi=self._zi_de)

        # 5. Resample to the plugin's tap rate. resample_poly has no zi/streaming state --
        #    every call re-zero-pads its own array boundary, treating position 0 as a cold
        #    start even when it is really mid-signal -- so a single "hold back the tail"
        #    overlap (what a first attempt here used) isn't enough: the *reserve* held back
        #    from the previous call is genuine not-yet-emitted audio, and re-feeding it as
        #    the new call's leading edge means its own output is corrupted by that cold
        #    start. Measured: doing only that left a 0.02 max-abs boundary error, two orders
        #    of magnitude over the 1e-9 bound. This needs two buffers instead of one:
        #    `_history` is already-emitted audio kept ONLY so the filter has real context
        #    (its output is recomputed and discarded every call -- it was correct last
        #    time), and `_reserve` is not-yet-emitted audio that needs `_overlap` samples of
        #    lookahead in front of it before its own output is trustworthy. Verified this
        #    combination against a whole-signal reference: max abs diff exactly 0.0.
        self._reserve = np.concatenate([self._reserve, audio])
        if len(self._reserve) <= self._overlap:
            return np.zeros(0)
        emit = len(self._reserve) - self._overlap
        emit -= emit % self._down
        if emit <= 0:
            return np.zeros(0)
        chunk = np.concatenate([self._history, self._reserve[:emit + self._overlap]])
        out = signal.resample_poly(chunk, self._up, self._down)
        front = len(self._history) * self._up // self._down
        keep = front + emit * self._up // self._down

        emitted = self._reserve[:emit]
        hist_source = np.concatenate([self._history, emitted])
        self._history = hist_source[-self._overlap:]
        self._reserve = self._reserve[emit:]
        return np.asarray(out[front:keep], dtype=np.float64)

    def flush(self) -> np.ndarray:
        """Whatever is left in the reserve buffer, at the end of the capture. No lookahead
        margin needed here: the capture really does end, so there is no future data whose
        edge accuracy the margin would have protected."""
        if len(self._reserve) == 0:
            return np.zeros(0)
        chunk = np.concatenate([self._history, self._reserve])
        out = signal.resample_poly(chunk, self._up, self._down)
        front = len(self._history) * self._up // self._down
        self._history = np.zeros(0)
        self._reserve = np.zeros(0)
        return np.asarray(out[front:], dtype=np.float64)


# Floor for the log-domain envelope, in dB. Only needs to sit well below any threshold_db
# a caller would pass (and below float64 log(0) blowing up); -240dB is arbitrary margin,
# not a tuned value.
_SQUELCH_FLOOR_DB = -240.0


def apply_squelch(audio: np.ndarray, rate: float, threshold_db: float | None,
                  attack_ms: float = 5.0, release_ms: float = 100.0) -> np.ndarray:
    """Gate audio below `threshold_db`, or return it untouched when squelch is off.

    Modelled on what a receiver squelch does to the audio the plugin sees, not on SDR#'s
    internal implementation: an envelope follower with a fast attack and slow release. The
    attack is what clips the opening syllables of a transmission, which is the damage this
    arm exists to quantify -- the vessel name is almost always in the first words.

    threshold_db=None means squelch disabled, and must return the input unchanged so the
    two arms differ by the gate alone.

    The envelope is tracked in the dB domain, not linear. A linear |x| follower (the first
    version tried here) makes attack_ms nearly meaningless for a squelch: the time to cross
    threshold_db depends on the *linear gap* between the threshold and the signal, not on
    attack_ms alone, so a threshold set far below the transmission's level (the realistic
    case -- -40dB threshold under a -6dBFS signal, per test_squelch_clips_the_start_of_a_
    transmission) is crossed in a handful of samples regardless of attack_ms, and the test
    that exists to detect the very damage this arm is built to measure failed: only 7 of 750
    samples in the first 20ms came out gated. Tracking level in dB makes the one-pole step
    response's rise, in dB, independent of how far below the signal the threshold sits --
    same physics as an analogue compressor's log-domain envelope follower -- so attack_ms
    means what it says regardless of the threshold_db/signal-level gap. Verified against all
    four squelch tests before switching from the linear version.

    Not vectorised: the attack/release coefficient at each sample depends on comparing the
    input to the *previous* envelope value, i.e. on the recurrence's own not-yet-computed
    output, so scipy.signal.lfilter (fixed coefficients) cannot express it and there is no
    reordering that lets numpy compute it without the sample-by-sample scan. Measured at
    ~40s per hour of 37.5kHz audio -- the same order of magnitude as demodulate() itself
    (~100s/hour for this harness's FIR channel filter), so judged acceptable for an offline
    harness rather than trading correctness for speed. See task-4-report.md.
    """
    if threshold_db is None:
        return audio
    audio = np.asarray(audio, dtype=np.float64)
    if len(audio) == 0:
        return audio

    # Envelope: one-pole follower over 20*log10(|x|), floored so silence doesn't log(0).
    # Separate attack and release constants, as any squelch/compressor envelope needs.
    attack = np.exp(-1.0 / max(1.0, rate * attack_ms * 1e-3))
    release = np.exp(-1.0 / max(1.0, rate * release_ms * 1e-3))
    mag = np.abs(audio)
    floor_lin = 10 ** (_SQUELCH_FLOOR_DB / 20.0)
    mag_db = 20.0 * np.log10(np.maximum(mag, floor_lin))
    env_db = np.empty_like(mag_db)
    level = _SQUELCH_FLOOR_DB
    for i, m in enumerate(mag_db):
        coeff = attack if m > level else release
        level = coeff * level + (1.0 - coeff) * m
        env_db[i] = level

    return audio * (env_db >= threshold_db)


def demodulate(iq: np.ndarray, iq_rate: float, bandwidth_hz: float,
               offset_hz: float = 0.0, audio_rate: float = DEFAULT_AUDIO_RATE,
               deemphasis_us: float | None = 750.0) -> np.ndarray:
    """One-shot convenience over Demodulator. For tests and short signals only -- a real
    capture must go through Demodulator.process block by block."""
    iq = np.asarray(iq, dtype=np.complex128)
    if len(iq) == 0:
        return np.zeros(0)
    d = Demodulator(iq_rate, bandwidth_hz, offset_hz, audio_rate, deemphasis_us)
    return np.concatenate([d.process(iq), d.flush()])
