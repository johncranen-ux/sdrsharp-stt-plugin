"""NFM demodulation with a configurable channel bandwidth.

Bandwidth is the variable this whole harness exists to measure: marine Ch 01 is a 25 kHz
channel at +/-5 kHz deviation, so Carson gives 2*(5+3) ~ 16 kHz occupied, while SDR# was
running at 12.5 kHz. The decisive test here is not that the demodulator runs, but that it
can TELL 12.5 kHz from 25 kHz on identical input -- an instrument that cannot see the effect
it is built to measure would report a confident null.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from iq import baseband, demod  # noqa: E402

IQ_RATE = 250_000.0
AUDIO_RATE = 37_500.0


def _tone(freq, seconds, rate):
    t = np.arange(int(seconds * rate)) / rate
    return np.sin(2 * np.pi * freq * t)


def _speech_like(seconds, rate=8000.0):
    """Two tones and an envelope: enough spectral structure that a boundary artefact or a
    filter difference has somewhere to show up."""
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 400 * t) + 0.5 * np.sin(2 * np.pi * 1800 * t)) * 0.5


def _recovered_tone_power(audio, freq, rate):
    """Power at `freq` relative to total, ignoring filter settling at the edges."""
    trimmed = audio[len(audio) // 10: -len(audio) // 10]
    spec = np.abs(np.fft.rfft(trimmed * np.hanning(len(trimmed))))
    freqs = np.fft.rfftfreq(len(trimmed), 1 / rate)
    bin_i = int(np.argmin(np.abs(freqs - freq)))
    band = spec[max(0, bin_i - 2): bin_i + 3]
    return float(np.sum(band ** 2) / np.sum(spec ** 2))


def test_a_modulated_tone_comes_back_out():
    audio_in = _tone(1000.0, 0.5, 8000.0)
    iq = baseband.synth_nfm(audio_in, 8000.0, IQ_RATE, deviation_hz=3000.0)
    out = demod.demodulate(iq, IQ_RATE, bandwidth_hz=16_000.0, audio_rate=AUDIO_RATE,
                           deemphasis_us=None)
    assert _recovered_tone_power(out, 1000.0, AUDIO_RATE) > 0.5


def test_the_channel_is_tuned_by_the_offset():
    """The recording's VFO sits away from centre; mistuning it must not silently half-work."""
    audio_in = _tone(1000.0, 0.5, 8000.0)
    iq = baseband.synth_nfm(audio_in, 8000.0, IQ_RATE, deviation_hz=3000.0, offset_hz=40_000.0)

    tuned = demod.demodulate(iq, IQ_RATE, 16_000.0, offset_hz=40_000.0,
                             audio_rate=AUDIO_RATE, deemphasis_us=None)
    mistuned = demod.demodulate(iq, IQ_RATE, 16_000.0, offset_hz=0.0,
                                audio_rate=AUDIO_RATE, deemphasis_us=None)

    assert _recovered_tone_power(tuned, 1000.0, AUDIO_RATE) > 0.5
    assert _recovered_tone_power(mistuned, 1000.0, AUDIO_RATE) < 0.1


def test_a_narrow_filter_degrades_a_wide_signal():
    """THE test. A 12.5 kHz filter must measurably damage a signal that occupies ~16 kHz,
    while a 25 kHz filter passes it. If this fails the harness cannot answer its question."""
    audio_in = _tone(2500.0, 0.5, 8000.0)
    iq = baseband.synth_nfm(audio_in, 8000.0, IQ_RATE, deviation_hz=5000.0)

    wide = demod.demodulate(iq, IQ_RATE, 25_000.0, audio_rate=AUDIO_RATE, deemphasis_us=None)
    narrow = demod.demodulate(iq, IQ_RATE, 12_500.0, audio_rate=AUDIO_RATE, deemphasis_us=None)

    wide_p = _recovered_tone_power(wide, 2500.0, AUDIO_RATE)
    narrow_p = _recovered_tone_power(narrow, 2500.0, AUDIO_RATE)
    assert wide_p > narrow_p * 1.2, (
        f"wide {wide_p:.4f} vs narrow {narrow_p:.4f}: the harness cannot see the effect "
        f"it exists to measure")


def test_the_output_lands_at_the_requested_rate():
    iq = baseband.synth_nfm(_tone(1000.0, 1.0, 8000.0), 8000.0, IQ_RATE)
    out = demod.demodulate(iq, IQ_RATE, 16_000.0, audio_rate=AUDIO_RATE)
    assert abs(len(out) / AUDIO_RATE - 1.0) < 0.02


def test_deemphasis_tilts_the_spectrum_down():
    """750 us de-emphasis must attenuate 3 kHz more than 300 Hz, or it is not doing its job."""
    lo = baseband.synth_nfm(_tone(300.0, 0.5, 8000.0), 8000.0, IQ_RATE, deviation_hz=3000.0)
    hi = baseband.synth_nfm(_tone(3000.0, 0.5, 8000.0), 8000.0, IQ_RATE, deviation_hz=3000.0)

    def ratio(iq, freq):
        flat = demod.demodulate(iq, IQ_RATE, 16_000.0, audio_rate=AUDIO_RATE, deemphasis_us=None)
        tilt = demod.demodulate(iq, IQ_RATE, 16_000.0, audio_rate=AUDIO_RATE, deemphasis_us=750.0)
        return np.std(tilt) / np.std(flat)

    assert ratio(hi, 3000.0) < ratio(lo, 300.0)


def test_empty_input_is_not_a_crash():
    assert len(demod.demodulate(np.zeros(0, dtype=np.complex128), IQ_RATE, 16_000.0)) == 0


def test_streaming_matches_one_shot():
    """An hour of IQ is 14.4 GB, so the real capture is demodulated in blocks. If block
    processing differed from whole-signal processing, every arm would carry a boundary
    artefact every N samples and the WER difference between arms would be measuring that."""
    audio_in = _speech_like(2.0)
    iq = baseband.synth_nfm(audio_in, 8000.0, IQ_RATE, deviation_hz=3000.0, offset_hz=30_000.0)

    one_shot = demod.demodulate(iq, IQ_RATE, 16_000.0, offset_hz=30_000.0,
                                audio_rate=AUDIO_RATE)

    d = demod.Demodulator(IQ_RATE, 16_000.0, offset_hz=30_000.0, audio_rate=AUDIO_RATE)
    parts = [d.process(b) for b in np.array_split(iq, 9)]
    parts.append(d.flush())
    streamed = np.concatenate([p for p in parts if len(p)])

    n = min(len(one_shot), len(streamed))
    assert abs(len(one_shot) - len(streamed)) <= 2, "block splitting must not change length"
    assert np.max(np.abs(one_shot[:n] - streamed[:n])) < 1e-9, (
        "block boundaries must be invisible")


def test_the_mixer_phase_is_continuous_across_blocks():
    """Restarting the mixer phase each block puts a step at every boundary, and the FM
    discriminator turns a phase step into a loud click."""
    iq = baseband.synth_nfm(np.zeros(20_000), IQ_RATE, IQ_RATE, deviation_hz=0.0,
                            offset_hz=30_000.0)
    d = demod.Demodulator(IQ_RATE, 16_000.0, offset_hz=30_000.0, audio_rate=AUDIO_RATE,
                          deemphasis_us=None)
    out = np.concatenate([d.process(b) for b in np.array_split(iq, 8)] + [d.flush()])
    settled = out[len(out) // 4: -len(out) // 4]
    assert np.max(np.abs(settled)) < 0.05, "a perfectly tuned unmodulated carrier is silent"
