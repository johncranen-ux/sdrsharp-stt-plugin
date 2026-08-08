"""The plugin's own DSP chain, ported to Python and pinned against production output.

bench.py scores `_sent.wav`, which is post-DSP: the plugin DC-blocks, high-passes at 150 Hz,
resamples to 16 kHz and peak-normalises before sending. The replay harness must do the same
or its clips are a different shape from the corpus its numbers get compared against.

A port can diverge silently, so it is pinned against real `_raw.wav` -> `_sent.wav` pairs
rather than against the formulas it was written from. That caught a real bug immediately:
the first attempt scored 0.946 correlation because LinearResample steps by
(len-1)/(outLen-1), not by fromRate/toRate.
"""

import os
import sys
import wave
from pathlib import Path

import numpy as np
import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from iq import plugin_dsp  # noqa: E402

# Real Ch 01 traffic, so it is never copied into the repository -- see the task note.
# Read from wherever the plugin wrote it; skip when it is not on this machine (CI).
_GOLDEN = Path(os.environ.get(
    "STT_CAPTURES_DIR", r"D:/SDR/SdrSharp/Plugins/SttPlugin/captures/2026-08-07"))


def _read(path):
    with wave.open(str(path), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
        return a / 32768.0, float(w.getframerate())


@pytest.mark.parametrize("clip", ["0000", "0003", "0121"])
def test_the_port_reproduces_production_output(clip):
    """The whole point of this task. Anything below 0.9999 means a stage has diverged.

    Skipped where the captures are not present. That is a real loss of coverage in CI and
    is accepted deliberately: the alternative is publishing recorded vessel traffic.
    """
    raw_path, sent_path = _GOLDEN / f"{clip}_raw.wav", _GOLDEN / f"{clip}_sent.wav"
    if not (raw_path.exists() and sent_path.exists()):
        pytest.skip(f"golden capture {clip} not present under {_GOLDEN}")

    raw, raw_rate = _read(raw_path)
    sent, sent_rate = _read(sent_path)

    got = plugin_dsp.process_chunk(raw, raw_rate, sent_rate)

    assert len(got) == len(sent), "sample count must match exactly, not approximately"
    corr = float(np.corrcoef(got, sent)[0, 1])
    rms = float(np.sqrt(np.mean((got - sent) ** 2)))
    assert corr >= 0.9999, f"correlation {corr:.6f}"
    assert rms < 1e-4, f"rms error {rms:.6g} (int16 quantisation is 3.05e-5)"


def test_the_resampler_steps_endpoint_to_endpoint():
    """The exact bug that cost 0.05 correlation. from_rate/to_rate is WRONG here."""
    x = np.arange(100, dtype=np.float64)
    y = plugin_dsp.linear_resample(x, 100.0, 10.0)
    assert len(y) == 10
    assert y[0] == pytest.approx(0.0)
    assert y[-1] == pytest.approx(99.0), "last output must land on the last input"


def test_dc_block_removes_a_constant_offset():
    x = np.ones(5000)
    y = plugin_dsp.dc_block(x)
    assert abs(y[-1]) < 0.02, "a steady DC level must decay away"


def test_high_pass_rejects_a_ctcss_tone_and_keeps_speech():
    """150 Hz cutoff exists to remove NFM rumble and CTCSS (67-250 Hz) below the speech band."""
    rate = 37500.0
    t = np.arange(20000) / rate
    low = np.sin(2 * np.pi * 80.0 * t)
    mid = np.sin(2 * np.pi * 1000.0 * t)
    settled = slice(5000, None)
    assert np.std(plugin_dsp.high_pass(low, rate)[settled]) < 0.25
    assert np.std(plugin_dsp.high_pass(mid, rate)[settled]) > 0.6


def test_the_low_pass_has_unity_dc_gain_and_is_symmetric():
    h = plugin_dsp.design_low_pass(7200.0, 37500.0, 63)
    assert len(h) == 63
    assert h.sum() == pytest.approx(1.0)
    assert np.allclose(h, h[::-1]), "linear phase requires a symmetric kernel"


def test_normalize_hits_minus_one_dbfs():
    y = plugin_dsp.normalize(np.array([0.1, -0.05, 0.2]))
    assert np.max(np.abs(y)) == pytest.approx(10 ** (-1.0 / 20.0), rel=1e-4)


def test_normalize_leaves_a_silent_chunk_alone():
    """Amplifying noise-only audio to full scale would hand the decoder amplified hiss."""
    quiet = np.full(100, 1e-9)
    assert np.max(np.abs(plugin_dsp.normalize(quiet))) < 1e-6
