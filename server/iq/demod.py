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

# RF channel power measurement.
#
# 1 ms frames. Chosen from below by the squelch, not from above by the segmenter: the
# squelch's attack is 5 ms and the whole point of that arm is to measure how much of a
# transmission's opening the gate eats, so a frame coarser than a few ms would quantise the
# damage being measured. The segmenter wants ~20 ms and just averages these down. An hour of
# track is 3.6M float64 (29 MB), which is affordable where the IQ itself (14.4 GB) is not.
_POWER_FRAME_MS = 1.0

# Floor for the log conversion, so a frame of exact digital silence is a very negative
# number rather than -inf. Well below any real receiver noise floor; not a tuned value.
_POWER_FLOOR_DB = -300.0


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

        # RF channel power, accumulated over the whole capture on the absolute input
        # timeline -- deliberately NOT returned from process(). The emitted audio lags its
        # input by the resampler's lookahead reserve, so a per-call return value would make
        # the caller responsible for realigning power against audio; frame i here always
        # means t = i / power_frame_rate from the start of the recording, whatever the
        # blocking. Measured after the channel filter and before the discriminator, which is
        # where a real squelch measures it and the last point at which amplitude still
        # exists.
        self._pow_frame = max(1, int(round(iq_rate * _POWER_FRAME_MS * 1e-3)))
        self.power_frame_rate = iq_rate / self._pow_frame
        self._pow_carry = np.zeros(0)                 # tail of a not-yet-complete frame
        self._pow_parts: list[np.ndarray] = []

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

        # 2a. Channel power, while the amplitude still exists. The next line destroys it:
        #     np.angle() keeps only phase, which is why an FM discriminator is exactly as
        #     loud with no carrier as with one.
        self._accumulate_power(block)

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

    def _accumulate_power(self, filtered: np.ndarray) -> None:
        """Mean-square the channel-filtered IQ into fixed frames, carrying the remainder.

        The carry is what makes the track independent of block size: a block boundary
        falling mid-frame must not start a short frame, or every frame after it would shift
        and the times read off the track would drift against the audio.
        """
        squared = np.abs(filtered) ** 2
        if len(self._pow_carry):
            squared = np.concatenate([self._pow_carry, squared])
        n_frames = len(squared) // self._pow_frame
        if n_frames:
            whole = squared[:n_frames * self._pow_frame].reshape(n_frames, self._pow_frame)
            self._pow_parts.append(whole.mean(axis=1))
        self._pow_carry = squared[n_frames * self._pow_frame:]

    @property
    def power_db(self) -> np.ndarray:
        """The whole capture's channel power in dB, one value per 1/power_frame_rate second.

        Recomputed on each access (a concatenate plus a log over ~3.6M values per hour), so
        read it once and keep it rather than indexing into it in a loop.
        """
        if not self._pow_parts:
            return np.zeros(0)
        linear = np.concatenate(self._pow_parts)
        return 10.0 * np.log10(np.maximum(linear, 10 ** (_POWER_FLOOR_DB / 10.0)))

    def flush(self) -> np.ndarray:
        """Whatever is left in the reserve buffer, at the end of the capture. No lookahead
        margin needed here: the capture really does end, so there is no future data whose
        edge accuracy the margin would have protected."""
        # The final partial frame, so the power track spans the whole recording rather than
        # stopping up to one frame short of it. Done before the early return below, which is
        # about the audio reserve and says nothing about the power carry.
        if len(self._pow_carry):
            self._pow_parts.append(np.array([self._pow_carry.mean()]))
            self._pow_carry = np.zeros(0)

        if len(self._reserve) == 0:
            return np.zeros(0)
        chunk = np.concatenate([self._history, self._reserve])
        out = signal.resample_poly(chunk, self._up, self._down)
        front = len(self._history) * self._up // self._down
        self._history = np.zeros(0)
        self._reserve = np.zeros(0)
        return np.asarray(out[front:], dtype=np.float64)


def _expand_gate(open_frames: np.ndarray, audio: np.ndarray,
                 power_frame_rate: float, audio_rate: float) -> np.ndarray:
    """Apply a frame-rate boolean gate to audio-rate samples, by zeroing closed runs.

    Deliberately not `audio * gate[indices]`: an hour of audio is 135M samples, so both the
    index array and the expanded mask would be ~1 GB each on top of the audio itself. The
    gate only changes state a few thousand times an hour, so walking its runs costs nothing
    and allocates one copy.
    """
    out = audio.copy()
    closed = ~open_frames
    if not closed.any():
        return out

    edges = np.diff(np.concatenate([[0], closed.view(np.int8), [0]]))
    scale = audio_rate / power_frame_rate
    for a, b in zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)):
        i0 = min(len(out), int(a * scale))
        # A closed run reaching the last frame extends to the end of the audio: the audio
        # can outrun the power track by a fraction of a frame after resampling, and that
        # tail must not come back un-gated.
        i1 = len(out) if b >= len(closed) else min(len(out), int(b * scale))
        out[i0:i1] = 0.0
    return out


def apply_squelch(audio: np.ndarray, audio_rate: float, threshold_db: float | None,
                  power_db: np.ndarray, power_frame_rate: float,
                  attack_ms: float = 5.0, release_ms: float = 100.0) -> np.ndarray:
    """Gate audio by RF CHANNEL POWER, or return it untouched when squelch is off.

    `threshold_db` is a level on `power_db` (Demodulator.power_db), not on the audio. That
    is the whole correction: the first version of this function ran its envelope follower
    over |audio|, and an FM discriminator emits full-scale hiss when there is no carrier, so
    the gate stayed open through every second of dead air in the capture. Squelch exists
    precisely because carrier presence is invisible in demodulated audio -- it lives in RF
    amplitude, which the discriminator throws away. Same blind spot, same fix, as
    segments.detect_segments.

    Express the threshold relative to the capture's measured noise floor
    (segments.noise_floor_db), the way a squelch knob behaves. An absolute value would have
    to be retuned whenever RF gain changed.

    threshold_db=None means squelch disabled, and returns the input unchanged so the two
    arms differ by the gate alone.

    Fast attack, slow release, as any receiver squelch has. The attack is what clips the
    opening syllables of a transmission, which is the damage this arm exists to quantify --
    the vessel name is almost always in the first words.

    The envelope is tracked in the dB domain. A linear follower (the first version) makes
    attack_ms nearly meaningless: time-to-threshold then depends on the linear gap between
    threshold and signal rather than on attack_ms, so a threshold well below the
    transmission's level -- the realistic case -- is crossed in a handful of frames whatever
    attack_ms says, and the test for the very damage this arm measures came out passing with
    only 7 of 750 samples gated. In dB the one-pole step response's rise is independent of
    that gap, same physics as an analogue compressor's log-domain follower.

    Not vectorised: each frame's attack/release choice depends on the previous frame's
    envelope, i.e. on the recurrence's own not-yet-computed output, so lfilter's fixed
    coefficients cannot express it. It now runs over the 1 kHz power track rather than
    37.5 kHz audio, which is ~4 s per captured hour instead of ~40 s.
    """
    if threshold_db is None:
        return audio
    audio = np.asarray(audio, dtype=np.float64)
    if len(audio) == 0:
        return audio

    power_db = np.asarray(power_db, dtype=np.float64)
    if len(power_db) == 0:
        raise ValueError("squelch needs a channel-power track; pass Demodulator.power_db")

    attack = np.exp(-1.0 / max(1.0, power_frame_rate * attack_ms * 1e-3))
    release = np.exp(-1.0 / max(1.0, power_frame_rate * release_ms * 1e-3))
    env_db = np.empty_like(power_db)
    # Start at the capture's opening level rather than at -inf: the receiver has been on and
    # its follower settled long before the recording began, so a cold start would gate the
    # first moments of the file for a reason that has nothing to do with the squelch.
    level = float(power_db[0])
    for i, p in enumerate(power_db):
        coeff = attack if p > level else release
        level = coeff * level + (1.0 - coeff) * p
        env_db[i] = level

    return _expand_gate(env_db >= threshold_db, audio, power_frame_rate, audio_rate)


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
