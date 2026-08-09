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


# Fixed seed: a flaky DSP test is worthless.
_WIDEBAND_VOICE_SEED = 0x5EA1
_WIDEBAND_VOICE_FREQS = (300.0, 600.0, 1000.0, 1500.0, 2200.0, 3000.0)


def _wideband_voice_like(seconds, rate=8000.0):
    """Several tones spanning the marine-voice band (300 Hz-3 kHz), random relative phase
    and amplitude, with a slow syllabic-rate envelope -- richer than a single tone, so a
    channel filter narrower than the signal's occupied bandwidth has real energy across the
    band to clip, not just one frequency's Bessel tail. Peak-normalised to +/-1, matching
    every other helper here, so `deviation_hz` passed to synth_nfm means peak deviation."""
    rng = np.random.default_rng(_WIDEBAND_VOICE_SEED)
    t = np.arange(int(seconds * rate)) / rate
    freqs = np.array(_WIDEBAND_VOICE_FREQS)
    phases = rng.uniform(0, 2 * np.pi, size=len(freqs))
    amps = rng.uniform(0.5, 1.0, size=len(freqs))
    signal_ = sum(a * np.sin(2 * np.pi * f * t + p) for f, a, p in zip(freqs, amps, phases))
    envelope = 0.6 + 0.4 * np.sin(2 * np.pi * 2.0 * t)   # ~2 Hz syllabic-rate wobble
    signal_ = signal_ * envelope
    return signal_ / np.max(np.abs(signal_))


def _recovered_tone_power(audio, freq, rate):
    """Power at `freq` relative to total, ignoring filter settling at the edges."""
    trimmed = audio[len(audio) // 10: -len(audio) // 10]
    spec = np.abs(np.fft.rfft(trimmed * np.hanning(len(trimmed))))
    freqs = np.fft.rfftfreq(len(trimmed), 1 / rate)
    bin_i = int(np.argmin(np.abs(freqs - freq)))
    band = spec[max(0, bin_i - 2): bin_i + 3]
    return float(np.sum(band ** 2) / np.sum(spec ** 2))


def _recovered_signal_power(audio, freqs, rate):
    """Like _recovered_tone_power, generalised to several known frequencies at once: total
    power in narrow bands around each of `freqs`, relative to the whole spectrum."""
    trimmed = audio[len(audio) // 10: -len(audio) // 10]
    spec = np.abs(np.fft.rfft(trimmed * np.hanning(len(trimmed))))
    fft_freqs = np.fft.rfftfreq(len(trimmed), 1 / rate)
    total = np.sum(spec ** 2)
    acc = 0.0
    for f in freqs:
        bin_i = int(np.argmin(np.abs(fft_freqs - f)))
        band = spec[max(0, bin_i - 2): bin_i + 3]
        acc += np.sum(band ** 2)
    return float(acc / total)


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


@pytest.mark.xfail(
    reason=(
        "Honest finding, fix round 1 (2026-08-09): measured wide/narrow ratio is ~0.995-0.997 "
        "-- under 1.0, not over it -- for a peak-normalised, +/-5kHz-deviation, 300Hz-3kHz "
        "6-tone speech-like signal through the linear-phase FIR channel filter, at every tap "
        "count from 255 to 2047. Carson's rule predicts ~16kHz occupied (2*(5+3)), but Carson "
        "is a conservative 98%-power bound, not a measured spectrum: under 0.05% of this "
        "signal's actual power sits beyond 6.25kHz (checked directly via FFT), so a "
        "magnitude-only channel filter has almost nothing to remove differently between 12.5 "
        "and 25kHz. This was checked with a single 2500Hz tone (task 3 initial), an ideal "
        "brick-wall filter (rules out filter sharpness as the cause), a hand-built FM tone "
        "bypassing synth_nfm's upsampler (rules out a synth_nfm artefact), and now this "
        "6-tone signal across three phase/amplitude weightings -- all agree. This is a "
        "genuine small-effect finding, not a test bug: see task-3-report.md fix-round-1. "
        "Left as xfail rather than deleted so a future filter or synthesis change that moves "
        "the honest ratio above 1.1 shows up here as an unexpected pass."
    ),
    strict=False,
)
def test_a_narrow_filter_degrades_a_wide_signal():
    """THE test. A 12.5 kHz filter should measurably damage a signal that occupies ~16 kHz
    (Carson: 2*(5+3)), while a 25 kHz filter passes it -- if the harness cannot see this
    effect, it cannot answer its own question. Currently xfail: see the marker's reason."""
    audio_in = _wideband_voice_like(0.5, 8000.0)
    iq = baseband.synth_nfm(audio_in, 8000.0, IQ_RATE, deviation_hz=5000.0)

    wide = demod.demodulate(iq, IQ_RATE, 25_000.0, audio_rate=AUDIO_RATE, deemphasis_us=None)
    narrow = demod.demodulate(iq, IQ_RATE, 12_500.0, audio_rate=AUDIO_RATE, deemphasis_us=None)

    wide_p = _recovered_signal_power(wide, _WIDEBAND_VOICE_FREQS, AUDIO_RATE)
    narrow_p = _recovered_signal_power(narrow, _WIDEBAND_VOICE_FREQS, AUDIO_RATE)
    # Threshold set from the measurement, not the other way around: the honestly measured
    # ratio at 511 taps is ~0.996, so even 1.02 is not a margin below what was measured --
    # it is above it. There is currently no honest threshold here to assert past 1.0 itself.
    assert wide_p > narrow_p * 1.1, (
        f"wide {wide_p:.4f} vs narrow {narrow_p:.4f} (ratio {wide_p / narrow_p:.4f}): "
        f"the harness cannot see the effect it exists to measure")


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


# Squelch
#
# The second variable under test. The suspicion from 2026-08-07: squelch clips the opening
# of each transmission, and the vessel name is almost always in the first words -- exactly
# what identification needs. Squelch-off is a supported configuration; the plugin's VAD
# falls back to its adaptive RMS gate when ReadSquelchOpen() returns None.


# These tests are driven from REAL demodulated output rather than a hand-built
# silence-then-tone array. The hand-built version was the fiction that hid the bug: it
# assumed dead air arrives at the gate quiet, and it never does.


def _demod_with_power(iq, bandwidth_hz=16_000.0):
    d = demod.Demodulator(IQ_RATE, bandwidth_hz, audio_rate=AUDIO_RATE)
    audio = np.concatenate([d.process(iq), d.flush()])
    return audio, d.power_db, d.power_frame_rate


def _open_threshold(power_db, over_floor_db=6.0):
    """A squelch knob setting, expressed the way a real one behaves: so many dB above the
    capture's own noise floor. 20th percentile, matching segments.noise_floor_db."""
    return float(np.percentile(power_db, 20.0)) + over_floor_db


def test_squelch_off_is_a_no_op():
    """The arm this is compared against, so it must not alter a single sample."""
    audio, power, prate = _demod_with_power(_noise_carrier_noise(0.2, 0.2))
    assert np.array_equal(demod.apply_squelch(audio, AUDIO_RATE, None, power, prate), audio)


def test_squelch_closes_on_dead_air_however_loud_the_discriminator_makes_it():
    """THE squelch regression test, and the same bug as the segmenter's. The audio here is
    near full scale during dead air -- that is what an FM discriminator does with no carrier
    -- so any gate that looks at audio amplitude stays wide open. The gate has to look at RF
    power, which is exactly what a real squelch does."""
    iq = _noise_carrier_noise()
    audio, power, prate = _demod_with_power(iq)

    dead = slice(int(0.2 * AUDIO_RATE), int(0.8 * AUDIO_RATE))
    assert np.std(audio[dead]) > 0.01, "precondition: dead air is NOT quiet in the audio"

    gated = demod.apply_squelch(audio, AUDIO_RATE, _open_threshold(power), power, prate)
    assert np.max(np.abs(gated[dead])) == 0.0, (
        f"dead air must be gated to silence, peak was {np.max(np.abs(gated[dead])):.4f}")


def test_squelch_passes_the_transmission():
    iq = _noise_carrier_noise()
    audio, power, prate = _demod_with_power(iq)
    gated = demod.apply_squelch(audio, AUDIO_RATE, _open_threshold(power), power, prate)

    talk = slice(int(1.2 * AUDIO_RATE), int(1.8 * AUDIO_RATE))
    np.testing.assert_array_equal(gated[talk], audio[talk])


def test_squelch_clips_the_start_of_a_transmission():
    """The exact damage this arm exists to quantify: the gate takes time to open, and the
    vessel name is in the words it eats."""
    iq = _noise_carrier_noise()
    audio, power, prate = _demod_with_power(iq)
    gated = demod.apply_squelch(audio, AUDIO_RATE, _open_threshold(power), power, prate,
                                attack_ms=5.0)

    onset = int(1.0 * AUDIO_RATE)
    assert np.max(np.abs(gated[onset:onset + int(0.0005 * AUDIO_RATE)])) == 0.0, (
        "the gate cannot already be open at the instant the carrier appears")
    assert np.max(np.abs(gated[onset + int(0.02 * AUDIO_RATE):
                                onset + int(0.05 * AUDIO_RATE)])) > 0.0, (
        "and it must be open well before the first word is over")


def test_a_slower_attack_eats_more_of_the_opening():
    """attack_ms has to mean something, or the arm is measuring nothing. The dB-domain
    envelope follower exists for this: a linear |x| follower made the time-to-open depend on
    the threshold/signal gap instead of on attack_ms."""
    iq = _noise_carrier_noise()
    audio, power, prate = _demod_with_power(iq)
    threshold = _open_threshold(power)

    def gated_samples(attack_ms):
        g = demod.apply_squelch(audio, AUDIO_RATE, threshold, power, prate,
                                attack_ms=attack_ms)
        onset = int(1.0 * AUDIO_RATE)
        window = slice(onset, onset + int(0.2 * AUDIO_RATE))
        return int(np.sum(g[window] == 0.0))

    assert gated_samples(50.0) > gated_samples(5.0)


# RF channel power
#
# The whole reason radios have squelch: an FM discriminator emits FULL-SCALE HISS when there
# is no carrier. Noise and speech come out of it equally loud, so no threshold on audio
# amplitude can tell them apart -- the 2026-08-09 harness gated on demodulated-audio RMS and
# cut 57.6 of 60.1 minutes of a real capture into "clips", against an RF-measured truth of
# 23 transmissions at 4.8% duty. Power has to be measured in the RF domain, on the
# channel-filtered IQ, before the discriminator throws the amplitude away.


def _noise_carrier_noise(quiet_s=1.0, talk_s=1.0, noise_db=-20.0):
    """A capture the way a real one looks: dead air, one transmission, dead air."""
    return np.concatenate([
        baseband.synth_noise(quiet_s, IQ_RATE, noise_db, seed=1),
        baseband.synth_nfm(_speech_like(talk_s), 8000.0, IQ_RATE,
                           deviation_hz=5000.0, noise_db=noise_db),
        baseband.synth_noise(quiet_s, IQ_RATE, noise_db, seed=2),
    ])


def _power_track(iq, bandwidth_hz=16_000.0, blocks=1):
    d = demod.Demodulator(IQ_RATE, bandwidth_hz, audio_rate=AUDIO_RATE)
    for block in np.array_split(iq, blocks):
        d.process(block)
    d.flush()
    return d.power_db, d.power_frame_rate


def test_channel_power_is_high_with_a_carrier_and_low_without():
    """THE test this fix exists for. The demodulated audio cannot make this distinction --
    test_audio_rms_cannot_tell_noise_from_speech below proves that on the same signal."""
    power_db, rate = _power_track(_noise_carrier_noise())

    quiet = np.concatenate([power_db[int(0.1 * rate):int(0.9 * rate)],
                            power_db[int(2.1 * rate):int(2.9 * rate)]])
    carrier = power_db[int(1.1 * rate):int(1.9 * rate)]
    assert np.median(carrier) > np.median(quiet) + 15.0, (
        f"carrier {np.median(carrier):.1f} dB vs dead air {np.median(quiet):.1f} dB: "
        f"channel power must separate a transmission from no transmission")


def test_audio_rms_cannot_tell_noise_from_speech():
    """Not a test of new code -- a pin on WHY the power track exists. If this ever starts
    failing, the discriminator stopped behaving like an FM discriminator."""
    iq = _noise_carrier_noise()
    audio = demod.demodulate(iq, IQ_RATE, 16_000.0, audio_rate=AUDIO_RATE)
    r = AUDIO_RATE

    def rms(a):
        return float(np.sqrt(np.mean(a ** 2)))

    quiet = rms(audio[int(0.1 * r):int(0.9 * r)])
    carrier = rms(audio[int(1.1 * r):int(1.9 * r)])
    assert quiet > carrier * 0.5, (
        f"dead-air audio RMS {quiet:.4f} vs speech {carrier:.4f} -- if dead air were "
        f"genuinely quiet in the audio domain, the original RMS segmenter would have worked")


def test_the_power_track_covers_the_whole_capture():
    """Segment times are read off this track, so a track that silently ran short would
    place every later cut at the wrong moment."""
    power_db, rate = _power_track(_noise_carrier_noise())
    assert abs(len(power_db) / rate - 3.0) < 0.02


def test_the_power_track_does_not_depend_on_the_block_size():
    """The real capture is streamed in blocks whose size is an implementation detail. If it
    leaked into the power track, it would leak into where every clip gets cut."""
    iq = _noise_carrier_noise(quiet_s=0.3, talk_s=0.4)
    one_shot, _ = _power_track(iq, blocks=1)
    streamed, _ = _power_track(iq, blocks=7)
    assert len(one_shot) == len(streamed)
    assert np.max(np.abs(one_shot - streamed)) < 1e-9
