"""Reading SDR# baseband recordings, and synthesising IQ to test against.

Every later test in the replay harness builds its input with synth_nfm, so this is the
foundation: if the synthesiser is wrong, every downstream test passes against a fiction.
"""

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from iq import baseband  # noqa: E402


def _write_iq_wav(path, iq, rate):
    """Write interleaved I/Q as 16-bit stereo, which is what SDR# sampleFormat=1 produces."""
    inter = np.empty(iq.size * 2, dtype=np.float64)
    inter[0::2] = iq.real
    inter[1::2] = iq.imag
    pcm = np.clip(inter * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())


def _to_rf64(path):
    """Rewrite a plain RIFF wav as RF64 in place: 'RF64' magic, 0xFFFFFFFF placeholder
    sizes, and a ds64 chunk carrying the real 64-bit values. This is what SDR#'s
    'WAV RF64' file format produces, and it is what an hour-long capture must be, because
    'WAV SDR# Compatible' tops out at 2-4 GB and 60 min @ 250 kSPS is ~3.6 GB."""
    raw = bytearray(path.read_bytes())
    riff_size = int.from_bytes(raw[4:8], "little")
    data_at = raw.find(b"data")
    data_size = int.from_bytes(raw[data_at + 4:data_at + 8], "little")

    ds64 = (b"ds64" + (28).to_bytes(4, "little")
            + riff_size.to_bytes(8, "little")
            + data_size.to_bytes(8, "little")
            + (data_size // 4).to_bytes(8, "little")
            + (0).to_bytes(4, "little"))

    raw[0:4] = b"RF64"
    raw[4:8] = (0xFFFFFFFF).to_bytes(4, "little")
    raw[data_at + 4:data_at + 8] = (0xFFFFFFFF).to_bytes(4, "little")
    out = raw[:12] + ds64 + raw[12:]
    out[4:8] = (0xFFFFFFFF).to_bytes(4, "little")
    path.write_bytes(bytes(out))


def test_a_baseband_wav_round_trips(tmp_path):
    rate = 250_000.0
    iq = np.exp(2j * np.pi * 10_000.0 * np.arange(2000) / rate) * 0.5
    p = tmp_path / "SDRSharp_20260808_120000Z_160650000Hz_IQ.wav"
    _write_iq_wav(p, iq, rate)

    got, got_rate, centre = baseband.read_baseband(p)
    assert got_rate == rate
    assert got.dtype == np.complex128
    assert len(got) == 2000
    assert np.max(np.abs(got - iq)) < 1e-3, "16-bit quantisation only"


def test_an_rf64_capture_reads_identically(tmp_path):
    """The real capture format. Python's wave module cannot read RF64 at all, so this is
    the difference between a working harness and one that fails on the only input that
    matters."""
    rate = 250_000.0
    iq = np.exp(2j * np.pi * 10_000.0 * np.arange(2000) / rate) * 0.5

    plain = tmp_path / "plain_160650000Hz.wav"
    _write_iq_wav(plain, iq, rate)
    expected, _, _ = baseband.read_baseband(plain)

    big = tmp_path / "rf64_160650000Hz.wav"
    _write_iq_wav(big, iq, rate)
    _to_rf64(big)

    info = baseband.open_baseband(big)
    assert info.rate == rate and info.channels == 2 and info.frames == 2000
    got, _, _ = baseband.read_baseband(big)
    assert np.array_equal(got, expected)


def test_streaming_blocks_reassemble_into_the_whole_file(tmp_path):
    """An hour of IQ is 14.4 GB as complex128, so streaming is the only viable path and
    it must be exact, not merely close."""
    rate = 250_000.0
    iq = np.exp(2j * np.pi * 3_000.0 * np.arange(5000) / rate) * 0.4
    p = tmp_path / "cap_160650000Hz.wav"
    _write_iq_wav(p, iq, rate)

    blocks = list(baseband.iter_baseband(p, block_frames=512))
    assert len(blocks) > 1, "the test is pointless with a single block"
    whole, _, _ = baseband.read_baseband(p)
    assert np.array_equal(np.concatenate(blocks), whole)


def test_reading_a_huge_file_eagerly_is_refused(tmp_path):
    """read_baseband must never be the thing that OOMs on a real capture."""
    p = tmp_path / "cap_160650000Hz.wav"
    _write_iq_wav(p, np.zeros(16, dtype=np.complex128), 250_000.0)
    baseband.MAX_EAGER_BYTES, saved = 8, baseband.MAX_EAGER_BYTES
    try:
        with pytest.raises(ValueError, match="iter_baseband"):
            baseband.read_baseband(p)
    finally:
        baseband.MAX_EAGER_BYTES = saved


def test_the_centre_frequency_comes_from_the_filename():
    """SDR# encodes it there and nowhere else in the wav, and the mixer needs it."""
    name = "SDRSharp_20260808_120000Z_160650000Hz_IQ.wav"
    assert baseband.parse_centre_freq(name) == pytest.approx(160_650_000.0)


def test_a_filename_without_a_frequency_is_not_guessed():
    """Returning a plausible default would silently mistune every arm identically."""
    assert baseband.parse_centre_freq("recording.wav") is None


def test_synth_nfm_puts_the_carrier_where_asked():
    """The offset is what the mixer stage has to undo, so it must be exact."""
    rate = 250_000.0
    n = 8192
    audio = np.zeros(n)
    iq = baseband.synth_nfm(audio, rate, rate, deviation_hz=0.0, offset_hz=25_000.0)
    spectrum = np.abs(np.fft.fft(iq))
    peak_hz = np.fft.fftfreq(len(iq), 1 / rate)[int(np.argmax(spectrum))]
    assert peak_hz == pytest.approx(25_000.0, abs=rate / len(iq))


def test_synth_nfm_has_constant_envelope():
    """FM carries information in phase only. A varying envelope would mean the synthesiser
    is producing something the discriminator could cheat on."""
    rate = 250_000.0
    audio = np.sin(2 * np.pi * 1000 * np.arange(4000) / 8000.0)
    iq = baseband.synth_nfm(audio, 8000.0, rate, deviation_hz=3000.0)
    env = np.abs(iq)
    assert np.std(env) < 1e-9


# The gap that let the segmentation bug ship. synth_nfm ALWAYS emits a carrier, so
# "no transmission" -- the state the radio is actually in for ~95% of a captured hour --
# did not exist in any fixture, and no synthetic test could have caught a segmenter that
# calls dead air speech. These two build that missing case.


def test_synth_noise_sits_at_the_requested_level():
    """The level is what a threshold gets compared against, so it has to mean something."""
    noise = baseband.synth_noise(0.2, 250_000.0, level_db=-30.0)
    measured_db = 10 * np.log10(np.mean(np.abs(noise) ** 2))
    assert measured_db == pytest.approx(-30.0, abs=0.5)


def test_synth_noise_has_no_carrier():
    """The defining property: power is spread across the band instead of concentrated in
    one bin. Asserted against synth_nfm on the same length, so the contrast is the claim."""
    rate = 250_000.0
    noise = baseband.synth_noise(8192 / rate, rate, level_db=-30.0)
    carrier = baseband.synth_nfm(np.zeros(8192), rate, rate, deviation_hz=0.0)

    def peak_over_mean(iq):
        # Mean, not median: a pure carrier puts every other bin at exactly zero, so a
        # median denominator is 0.0 and the ratio is a divide-by-zero warning.
        power = np.abs(np.fft.fft(iq)) ** 2
        return float(np.max(power) / np.mean(power))

    # A pure carrier concentrates all N bins' worth of power into one, so peak/mean = N
    # (8192 here). Noise spreads it, and the largest of N exponential bins sits near ln(N),
    # about 9. Two orders of magnitude apart, so these bounds are not finely tuned.
    assert peak_over_mean(noise) < 100.0
    assert peak_over_mean(carrier) > 1000.0
