# IQ Replay Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replay one raw-IQ recording through different demodulator settings so the receiver's channel bandwidth and squelch can be measured against transcription accuracy, paired, from a single set of hand-verified references.

**Architecture:** A new `server/iq/` package turns a baseband wav into a captures-style directory of `NNNN_sent.wav` clips for a given arm (bandwidth, squelch). Because `bench.py::discover_clips` already globs `*_sent.wav` and derives clip IDs from filenames, the entire existing scoring stack — `bench.py` for WER, `bench_prompt_ab.py` for paired comparison with a bootstrap confidence interval — is reused **unchanged**. Segmentation is computed once and reused by every arm, so clip `0042` is the same transmission everywhere and pairing is valid.

**Tech Stack:** Python 3.10+, numpy, scipy (new), stdlib `wave`. Design spec: `docs/superpowers/specs/2026-08-08-iq-replay-harness-design.md`.

## Global Constraints

- **The production audio rate is 37,500 Hz, not 48,000.** `_raw.wav` files in `D:\SDR\SdrSharp\Plugins\SttPlugin\captures\` are 37500 Hz mono 16-bit; `_sent.wav` are 16000 Hz mono 16-bit. Never hard-code 48000; read the rate from the file or take it as a parameter. The spec says "48 kHz" in places — the spec is wrong on this point and this plan supersedes it.
- **37500 → 16000 is a non-integer ratio (2.34375)**, so `Decimator.Resample` takes the convolve-then-linear-interpolate path, *not* the polyphase path. Port both, but the non-integer path is the one production uses here.
- **Clip output format is exactly:** mono, 16000 Hz, 16-bit signed PCM, little-endian. Filenames `NNNN_sent.wav` with `NNNN` a zero-padded 4-digit index starting at `0000`.
- **All new tests live in `server/tests/` and must pass under `py -m pytest server/tests`.** CI runs Python 3.10 and 3.12.
- **No network calls in any test.** Synthetic IQ and the committed golden clips only.
- Follow the repo's comment style: explain *why*, and cite the measurement or bug that motivated the code.

---

### Task 1: Baseband reader and synthetic IQ

**Files:**
- Create: `server/iq/__init__.py`
- Create: `server/iq/baseband.py`
- Create: `server/tests/test_iq_baseband.py`
- Modify: `server/requirements.txt`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `read_baseband(path: str | Path) -> tuple[np.ndarray, float, float | None]` returning `(complex128 samples, sample_rate_hz, centre_freq_hz_or_None)`.
  - `parse_centre_freq(filename: str) -> float | None`.
  - `synth_nfm(audio: np.ndarray, audio_rate: float, iq_rate: float, deviation_hz: float = 3000.0, offset_hz: float = 0.0, noise_db: float | None = None) -> np.ndarray` returning complex128 IQ. Used by every later task's tests.

- [ ] **Step 1: Add scipy to requirements**

Edit `server/requirements.txt` to read:

```
rapidfuzz
anthropic
websockets
certifi
pip-system-certs
numpy
scipy
```

- [ ] **Step 2: Write the failing tests**

Create `server/tests/test_iq_baseband.py`:

```python
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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_iq_baseband.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'iq'`

- [ ] **Step 4: Install scipy**

Run: `py -m pip install scipy`
Expected: scipy installs; `py -c "import scipy; print(scipy.__version__)"` prints a version.

- [ ] **Step 5: Implement the module**

Create `server/iq/__init__.py` (empty file).

Create `server/iq/baseband.py`:

```python
"""Reading SDR# baseband (raw IQ) recordings, and synthesising IQ for tests.

SDR#'s BasebandRecorder writes interleaved I/Q as an ordinary wav: two channels, 16-bit
signed for sampleFormat=1. The sample rate is in the wav header; the CENTRE FREQUENCY is
not, and lives only in the filename, which is why parse_centre_freq exists and why it
returns None rather than a default -- a guessed centre would mistune every arm by the same
amount, producing a set of results that are self-consistent and uniformly wrong.
"""

from __future__ import annotations

import re
import wave
from pathlib import Path

import numpy as np

# SDR# names recordings like SDRSharp_20260808_120000Z_160650000Hz_IQ.wav
_CENTRE_RE = re.compile(r"_(\d+)Hz", re.IGNORECASE)


def parse_centre_freq(filename: str) -> float | None:
    """The centre frequency SDR# encoded in the filename, or None if it is not there."""
    match = _CENTRE_RE.search(Path(filename).name)
    return float(match.group(1)) if match else None


def read_baseband(path: str | Path) -> tuple[np.ndarray, float, float | None]:
    """(complex samples, sample rate, centre frequency or None)."""
    path = Path(path)
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 2:
            raise ValueError(f"{path.name}: expected 2 channels (I/Q), got {w.getnchannels()}")
        width = w.getsampwidth()
        rate = float(w.getframerate())
        raw = w.readframes(w.getnframes())

    if width == 2:
        flat = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 1:
        # 8-bit wav is unsigned, centred on 128.
        flat = (np.frombuffer(raw, dtype=np.uint8).astype(np.float64) - 128.0) / 128.0
    else:
        raise ValueError(f"{path.name}: unsupported sample width {width} bytes")

    return flat[0::2] + 1j * flat[1::2], rate, parse_centre_freq(path.name)


def synth_nfm(audio: np.ndarray, audio_rate: float, iq_rate: float,
              deviation_hz: float = 3000.0, offset_hz: float = 0.0,
              noise_db: float | None = None) -> np.ndarray:
    """Narrowband FM at `offset_hz` from DC, for tests.

    Constant envelope by construction: FM carries its information in phase alone, so a
    synthesiser that let amplitude vary would give the discriminator a second channel to
    cheat on and the demodulator tests would pass for the wrong reason.
    """
    audio = np.asarray(audio, dtype=np.float64)
    n_out = int(len(audio) * iq_rate / audio_rate) if len(audio) else 0
    if n_out <= 0:
        return np.zeros(0, dtype=np.complex128)

    # Resample the modulating audio up to the IQ rate by linear interpolation. Good enough:
    # the test signals are smooth tones well below the IQ Nyquist.
    src = np.linspace(0.0, len(audio) - 1, n_out) if len(audio) > 1 else np.zeros(n_out)
    up = np.interp(src, np.arange(len(audio)), audio)

    phase = 2 * np.pi * deviation_hz * np.cumsum(up) / iq_rate
    t = np.arange(n_out) / iq_rate
    iq = np.exp(1j * (phase + 2 * np.pi * offset_hz * t))

    if noise_db is not None:
        amp = 10 ** (noise_db / 20.0)
        rng = np.random.default_rng(0xC0FFEE)   # fixed: a flaky DSP test is worthless
        iq = iq + amp * (rng.normal(size=n_out) + 1j * rng.normal(size=n_out)) / np.sqrt(2)
    return iq
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_iq_baseband.py -v`
Expected: 5 passed

- [ ] **Step 7: Run the whole suite for regressions**

Run: `py -m pytest server/tests -q`
Expected: all pass (468 existing + 5 new)

- [ ] **Step 8: Commit**

```bash
git add server/iq/__init__.py server/iq/baseband.py server/tests/test_iq_baseband.py server/requirements.txt
git commit -m "Read SDR# baseband recordings, and synthesise IQ to test against"
```

---

### Task 2: Faithful port of the plugin's DSP chain

**Files:**
- Create: `server/iq/plugin_dsp.py`
- Create: `server/tests/test_iq_plugin_dsp.py`
- Create: `server/tests/golden/` (holds three copied clip pairs)

**Interfaces:**
- Consumes: nothing.
- Produces: `process_chunk(samples: np.ndarray, from_rate: float, to_rate: float = 16000.0) -> np.ndarray` — the whole production chain in one call. Also exposes the individual stages for testing: `dc_block`, `high_pass`, `design_low_pass`, `convolve`, `linear_resample`, `polyphase_decimate`, `resample`, `normalize`.

**Why this task has a golden test rather than re-derived formulas:** the plugin writes both
`NNNN_raw.wav` (pre-DSP, 37500 Hz) and `NNNN_sent.wav` (post-DSP, 16000 Hz) for every chunk,
so real production input/output pairs already exist on disk. A first attempt at this port
reached correlation 0.946 against them — it looked plausible and was wrong. The cause was
`LinearResample`, which steps by `(len-1)/(outLen-1)`, **not** by `fromRate/toRate`; the
difference is ~4e-5 per sample and drifts by about two samples across a three-second clip.
With that fixed the port reaches correlation 1.000000 and RMS error 1.2e-5, below the
3.05e-5 of int16 quantisation. **Do not substitute `scipy.signal.resample_poly` here** — it
would be a better resampler and would therefore no longer be what production does.

- [ ] **Step 1: Copy three golden clip pairs into the repo**

```bash
mkdir -p server/tests/golden
for c in 0000 0003 0121; do
  cp "D:/SDR/SdrSharp/Plugins/SttPlugin/captures/2026-08-07/${c}_raw.wav"  server/tests/golden/
  cp "D:/SDR/SdrSharp/Plugins/SttPlugin/captures/2026-08-07/${c}_sent.wav" server/tests/golden/
done
ls server/tests/golden
```

Expected: six files. These are short VHF transmissions and are committed deliberately, as
the only way to pin the port against production; `.gitignore` excludes bulk reference
transcripts and captures, not these six fixtures.

- [ ] **Step 2: Write the failing tests**

Create `server/tests/test_iq_plugin_dsp.py`:

```python
"""The plugin's own DSP chain, ported to Python and pinned against production output.

bench.py scores `_sent.wav`, which is post-DSP: the plugin DC-blocks, high-passes at 150 Hz,
resamples to 16 kHz and peak-normalises before sending. The replay harness must do the same
or its clips are a different shape from the corpus its numbers get compared against.

A port can diverge silently, so it is pinned against real `_raw.wav` -> `_sent.wav` pairs
rather than against the formulas it was written from. That caught a real bug immediately:
the first attempt scored 0.946 correlation because LinearResample steps by
(len-1)/(outLen-1), not by fromRate/toRate.
"""

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from iq import plugin_dsp  # noqa: E402

_GOLDEN = Path(__file__).resolve().parent / "golden"


def _read(path):
    with wave.open(str(path), "rb") as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
        return a / 32768.0, float(w.getframerate())


@pytest.mark.parametrize("clip", ["0000", "0003", "0121"])
def test_the_port_reproduces_production_output(clip):
    """The whole point of this task. Anything below 0.9999 means a stage has diverged."""
    raw, raw_rate = _read(_GOLDEN / f"{clip}_raw.wav")
    sent, sent_rate = _read(_GOLDEN / f"{clip}_sent.wav")

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
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_iq_plugin_dsp.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'iq.plugin_dsp'`

- [ ] **Step 4: Implement the port**

Create `server/iq/plugin_dsp.py`:

```python
"""SDRSharp.SttPlugin's audio conditioning, ported to Python.

A transcription of SDRSharp.SttPlugin/Dsp/{BiquadFilters,Decimator,Normalizer}.cs and the
order AudioProcessor.SendChunk applies them. It exists so the replay harness produces clips
the same shape as `_sent.wav`, which is what bench.py scores.

Transcribe, do not improve. scipy.signal.resample_poly is a better resampler than the
linear interpolation below and using it would make the harness stop measuring what
production does. The one place that lesson was learned the hard way is linear_resample --
see its docstring.
"""

from __future__ import annotations

import math

import numpy as np

WHISPER_RATE = 16_000.0
HIGH_PASS_CUTOFF_HZ = 150.0
DEFAULT_TAPS = 63


def dc_block(x: np.ndarray, r: float = 0.995) -> np.ndarray:
    """y[n] = x[n] - x[n-1] + r*y[n-1]. Sequential by nature; kept as a loop for clarity."""
    y = np.empty_like(x)
    prev_x = prev_y = 0.0
    for i, v in enumerate(x):
        prev_y = v - prev_x + r * prev_y
        prev_x = v
        y[i] = prev_y
    return y


def high_pass(x: np.ndarray, sample_rate: float,
              cutoff_hz: float = HIGH_PASS_CUTOFF_HZ, q: float = 0.7071) -> np.ndarray:
    """Second-order Butterworth high-pass, RBJ Audio EQ Cookbook coefficients."""
    w0 = 2 * math.pi * cutoff_hz / sample_rate
    alpha = math.sin(w0) / (2 * q)
    cos_w0 = math.cos(w0)

    b0, b1, b2 = (1 + cos_w0) / 2, -(1 + cos_w0), (1 + cos_w0) / 2
    a0, a1, a2 = 1 + alpha, -2 * cos_w0, 1 - alpha
    b0, b1, b2, a1, a2 = b0 / a0, b1 / a0, b2 / a0, a1 / a0, a2 / a0

    y = np.empty_like(x)
    x1 = x2 = y1 = y2 = 0.0
    for i, v in enumerate(x):
        out = b0 * v + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        x2, x1 = x1, v
        y2, y1 = y1, out
        y[i] = out
    return y


def design_low_pass(cutoff_hz: float, sample_rate: float, num_taps: int = DEFAULT_TAPS) -> np.ndarray:
    """Windowed-sinc low-pass, 4-term Blackman-Harris, normalised to unity DC gain."""
    if num_taps < 3:
        raise ValueError("num_taps must be >= 3")
    fc = cutoff_hz / sample_rate
    m = num_taps - 1
    n = np.arange(num_taps, dtype=np.float64)
    k = n - m / 2.0
    safe_k = np.where(np.abs(k) < 1e-9, 1.0, k)
    sinc = np.where(np.abs(k) < 1e-9, 2 * fc, np.sin(2 * math.pi * fc * safe_k) / (math.pi * safe_k))
    window = (0.35875
              - 0.48829 * np.cos(2 * math.pi * n / m)
              + 0.14128 * np.cos(4 * math.pi * n / m)
              - 0.01168 * np.cos(6 * math.pi * n / m))
    h = sinc * window
    return h / h.sum()


def convolve(x: np.ndarray, h: np.ndarray) -> np.ndarray:
    """y[n] = sum_k h[k]*x[n + center - k], zero outside. Matches Decimator.Convolve,
    including its centre offset, which compensates the kernel's group delay."""
    center = len(h) // 2
    full = np.convolve(x, h)
    return full[center:center + len(x)]


def polyphase_decimate(x: np.ndarray, h: np.ndarray, decimation: int) -> np.ndarray:
    """Integer-ratio path. Not used at 37500 -> 16000, but production would use it at
    48000 -> 16000, so it is ported rather than left as a trap for a future sample rate."""
    center = len(h) // 2
    out_len = len(x) // decimation
    y = np.empty(out_len)
    for n in range(out_len):
        base = n * decimation + center
        idx = base - np.arange(len(h))
        valid = (idx >= 0) & (idx < len(x))
        y[n] = float(np.dot(h[valid], x[idx[valid]]))
    return y


def linear_resample(x: np.ndarray, from_rate: float, to_rate: float) -> np.ndarray:
    """Literal transcription of Decimator.LinearResample.

    The step is (len-1)/(out_len-1) -- endpoint to endpoint -- NOT from_rate/to_rate. They
    differ by about 4e-5 per sample at 37500 -> 16000, which drifts roughly two samples
    across a three-second clip and drops correlation against production from 1.000 to 0.946.
    """
    out_len = int(len(x) * to_rate / from_rate)
    if out_len <= 0:
        return np.zeros(0)
    ratio = (len(x) - 1) / max(1, out_len - 1)
    pos = np.arange(out_len) * ratio
    i0 = pos.astype(int)
    frac = pos - i0
    i1 = np.minimum(i0 + 1, len(x) - 1)
    return x[i0] + frac * (x[i1] - x[i0])


def resample(x: np.ndarray, from_rate: float, to_rate: float) -> np.ndarray:
    """Decimator.Resample: anti-alias, then decimate or interpolate."""
    if len(x) == 0:
        return np.zeros(0)
    if abs(from_rate - to_rate) < 1.0:
        return x

    cutoff = min(from_rate, to_rate) / 2.0 * 0.90
    h = design_low_pass(cutoff, from_rate, DEFAULT_TAPS)

    ratio = from_rate / to_rate
    if ratio >= 1.0 and abs(ratio - round(ratio)) < 1e-6:
        return polyphase_decimate(x, h, int(round(ratio)))
    return linear_resample(convolve(x, h), from_rate, to_rate)


def normalize(x: np.ndarray, target_peak_db: float = -1.0) -> np.ndarray:
    """Per-chunk peak normalisation with a soft limiter, as Normalizer.Normalize."""
    if len(x) == 0:
        return np.zeros(0)
    peak = float(np.max(np.abs(x)))
    if peak < 1e-6:
        return x.copy()          # never amplify a noise-only chunk to full scale

    gain = (10 ** (target_peak_db / 20.0)) / peak
    y = x * gain
    t = 0.98
    y = np.where(y > t, t + (1 - t) * np.tanh((y - t) / (1 - t)), y)
    y = np.where(y < -t, -t + (1 - t) * np.tanh((y + t) / (1 - t)), y)
    return y


def process_chunk(samples: np.ndarray, from_rate: float,
                  to_rate: float = WHISPER_RATE) -> np.ndarray:
    """The full production chain, in AudioProcessor.SendChunk's order.

    Filter state starts fresh, because chunks are independent VAD segments rather than a
    continuous stream and each carries leading padding to absorb the settle time.
    """
    samples = np.asarray(samples, dtype=np.float64)
    conditioned = high_pass(dc_block(samples), from_rate)
    return normalize(resample(conditioned, from_rate, to_rate))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_iq_plugin_dsp.py -v`
Expected: 9 passed, including all three golden clips at correlation >= 0.9999

- [ ] **Step 6: Commit**

```bash
git add server/iq/plugin_dsp.py server/tests/test_iq_plugin_dsp.py server/tests/golden
git commit -m "Port the plugin's DSP chain, pinned against real production clips"
```

---

### Task 3: NFM demodulator

**Files:**
- Create: `server/iq/demod.py`
- Create: `server/tests/test_iq_demod.py`

**Interfaces:**
- Consumes: `iq.baseband.synth_nfm` (tests only).
- Produces: `demodulate(iq: np.ndarray, iq_rate: float, bandwidth_hz: float, offset_hz: float = 0.0, audio_rate: float = 37500.0, deemphasis_us: float | None = 750.0) -> np.ndarray` returning float64 audio at `audio_rate`.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_iq_demod.py`:

```python
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_iq_demod.py -v`
Expected: FAIL, `ImportError: cannot import name 'demod'`

- [ ] **Step 3: Implement the demodulator**

Create `server/iq/demod.py`:

```python
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


def demodulate(iq: np.ndarray, iq_rate: float, bandwidth_hz: float,
               offset_hz: float = 0.0, audio_rate: float = DEFAULT_AUDIO_RATE,
               deemphasis_us: float | None = 750.0) -> np.ndarray:
    """Demodulate NFM at `offset_hz` from centre, through a `bandwidth_hz` channel filter."""
    iq = np.asarray(iq, dtype=np.complex128)
    if len(iq) == 0:
        return np.zeros(0)

    # 1. Mix the wanted channel down to DC.
    if offset_hz:
        t = np.arange(len(iq)) / iq_rate
        iq = iq * np.exp(-2j * np.pi * offset_hz * t)

    # 2. Channel filter -- THE variable under test. Half the occupied bandwidth either side
    #    of DC, so `bandwidth_hz` means the same thing SDR#'s bandwidth control means.
    cutoff = bandwidth_hz / 2.0
    nyquist = iq_rate / 2.0
    if cutoff >= nyquist:
        raise ValueError(f"bandwidth {bandwidth_hz} Hz exceeds the recording's Nyquist")
    taps = signal.firwin(255, cutoff / nyquist, window="blackmanharris")
    iq = signal.lfilter(taps, 1.0, iq)

    # 3. FM discriminator: instantaneous frequency is the phase step between samples.
    #    np.angle of x[n]*conj(x[n-1]) is the standard form and needs no unwrapping.
    audio = np.angle(iq[1:] * np.conj(iq[:-1]))

    # 4. De-emphasis. Marine NFM is transmitted pre-emphasised; undoing it restores the
    #    spectral balance speech models expect. One-pole IIR with the standard time constant.
    if deemphasis_us:
        alpha = 1.0 / (1.0 + iq_rate * deemphasis_us * 1e-6)
        audio = signal.lfilter([alpha], [1.0, -(1.0 - alpha)], audio)

    # 5. Down to the plugin's tap rate. resample_poly needs an integer ratio, so reduce
    #    the rates to their lowest terms rather than assuming one.
    from math import gcd
    up, down = int(audio_rate), int(iq_rate)
    g = gcd(up, down)
    audio = signal.resample_poly(audio, up // g, down // g)

    return np.asarray(audio, dtype=np.float64)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_iq_demod.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add server/iq/demod.py server/tests/test_iq_demod.py
git commit -m "Demodulate NFM from IQ with a sweepable channel bandwidth"
```

---

### Task 4: Squelch gate

**Files:**
- Modify: `server/iq/demod.py` (append)
- Modify: `server/tests/test_iq_demod.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: `apply_squelch(audio: np.ndarray, rate: float, threshold_db: float | None, attack_ms: float = 5.0, release_ms: float = 100.0) -> np.ndarray`. `threshold_db=None` means squelch off and returns the input unchanged.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/test_iq_demod.py`:

```python
# Squelch
#
# The second variable under test. The suspicion from 2026-08-07: squelch clips the opening
# of each transmission, and the vessel name is almost always in the first words -- exactly
# what identification needs. Squelch-off is a supported configuration; the plugin's VAD
# falls back to its adaptive RMS gate when ReadSquelchOpen() returns None.


def test_squelch_off_is_a_no_op():
    """The arm this is compared against, so it must not alter a single sample."""
    audio = _tone(1000.0, 0.2, AUDIO_RATE)
    assert np.array_equal(demod.apply_squelch(audio, AUDIO_RATE, None), audio)


def test_squelch_mutes_audio_below_the_threshold():
    quiet = _tone(1000.0, 0.2, AUDIO_RATE) * 0.001
    gated = demod.apply_squelch(quiet, AUDIO_RATE, threshold_db=-40.0)
    assert np.max(np.abs(gated)) < np.max(np.abs(quiet)) * 0.1


def test_squelch_passes_audio_above_the_threshold():
    loud = _tone(1000.0, 0.2, AUDIO_RATE) * 0.5
    gated = demod.apply_squelch(loud, AUDIO_RATE, threshold_db=-40.0)
    settled = gated[int(0.05 * AUDIO_RATE):]
    assert np.std(settled) > 0.9 * np.std(loud[int(0.05 * AUDIO_RATE):])


def test_squelch_clips_the_start_of_a_transmission():
    """The exact damage being measured: a transmission that starts abruptly loses its
    opening to the gate's attack, and that is where the vessel name is."""
    silence = np.zeros(int(0.1 * AUDIO_RATE))
    speech = _tone(1000.0, 0.3, AUDIO_RATE) * 0.5
    audio = np.concatenate([silence, speech])

    gated = demod.apply_squelch(audio, AUDIO_RATE, threshold_db=-40.0)
    onset = len(silence)
    first_20ms = slice(onset, onset + int(0.02 * AUDIO_RATE))
    assert np.std(gated[first_20ms]) < np.std(audio[first_20ms]) * 0.9, (
        "the opening should be attenuated relative to ungated audio")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_iq_demod.py -k squelch -v`
Expected: FAIL, `AttributeError: module 'iq.demod' has no attribute 'apply_squelch'`

- [ ] **Step 3: Implement the gate**

Append to `server/iq/demod.py`:

```python
def apply_squelch(audio: np.ndarray, rate: float, threshold_db: float | None,
                  attack_ms: float = 5.0, release_ms: float = 100.0) -> np.ndarray:
    """Gate audio below `threshold_db`, or return it untouched when squelch is off.

    Modelled on what a receiver squelch does to the audio the plugin sees, not on SDR#'s
    internal implementation: an envelope follower with a fast attack and slow release. The
    attack is what clips the opening syllables of a transmission, which is the damage this
    arm exists to quantify -- the vessel name is almost always in the first words.

    threshold_db=None means squelch disabled, and must return the input unchanged so the
    two arms differ by the gate alone.
    """
    if threshold_db is None:
        return audio
    audio = np.asarray(audio, dtype=np.float64)
    if len(audio) == 0:
        return audio

    # Envelope: one-pole follower over |x|, with separate attack and release constants.
    attack = np.exp(-1.0 / max(1.0, rate * attack_ms * 1e-3))
    release = np.exp(-1.0 / max(1.0, rate * release_ms * 1e-3))
    mag = np.abs(audio)
    env = np.empty_like(mag)
    level = 0.0
    for i, m in enumerate(mag):
        coeff = attack if m > level else release
        level = coeff * level + (1.0 - coeff) * m
        env[i] = level

    threshold = 10 ** (threshold_db / 20.0)
    return audio * (env >= threshold)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_iq_demod.py -v`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add server/iq/demod.py server/tests/test_iq_demod.py
git commit -m "Gate demodulated audio with a squelch that can be switched off"
```

---

### Task 5: Fixed segmentation

**Files:**
- Create: `server/iq/segments.py`
- Create: `server/tests/test_iq_segments.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `detect_segments(audio: np.ndarray, rate: float, threshold: float = 0.02, min_ms: float = 400.0, hang_ms: float = 600.0, pad_ms: float = 300.0) -> list[tuple[float, float]]` — (start_s, end_s) pairs.
  - `write_segments(path, segments) -> None` and `read_segments(path) -> list[tuple[float, float]]`, one `start\tend` per line.
  - `cut(audio: np.ndarray, rate: float, segments) -> list[np.ndarray]`.

**Why this task matters more than it looks:** if each arm ran its own VAD, bandwidth and
squelch would move the boundaries, clip `0042` would be a different transmission in each arm,
and `bench_prompt_ab.py` — which pairs on `clip_id` — would compare unrelated audio while
reporting a confident number. Segments are computed **once** and reused by every arm.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_iq_segments.py`:

```python
"""Fixed segmentation: one cut list, reused by every arm.

The whole paired design rests on this. bench_prompt_ab.py pairs arms on clip_id, so if each
arm segmented its own audio the same id would name different transmissions in different arms
and the comparison would be meaningless while still producing a number.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from iq import segments  # noqa: E402

RATE = 16_000.0


def _burst(seconds, amp=0.5):
    t = np.arange(int(seconds * RATE)) / RATE
    return np.sin(2 * np.pi * 800 * t) * amp


def _silence(seconds):
    return np.zeros(int(seconds * RATE))


def test_two_bursts_become_two_segments():
    audio = np.concatenate([_silence(0.5), _burst(1.0), _silence(1.5), _burst(1.0), _silence(0.5)])
    found = segments.detect_segments(audio, RATE)
    assert len(found) == 2


def test_a_short_blip_is_not_a_segment():
    """Squelch tails and key-up clicks are not transmissions."""
    audio = np.concatenate([_silence(0.5), _burst(0.05), _silence(1.5)])
    assert segments.detect_segments(audio, RATE) == []


def test_a_segment_is_padded_before_the_first_word():
    """The vessel name is in the opening syllables; a cut that starts exactly on the
    threshold crossing loses them."""
    audio = np.concatenate([_silence(1.0), _burst(1.0), _silence(1.0)])
    start, _ = segments.detect_segments(audio, RATE, pad_ms=300.0)[0]
    assert 0.6 <= start <= 1.0, f"start {start} should sit before the burst at 1.0s"


def test_segments_survive_a_file_round_trip(tmp_path):
    original = [(1.25, 3.5), (10.0, 12.75)]
    p = tmp_path / "segments.txt"
    segments.write_segments(p, original)
    assert segments.read_segments(p) == original


def test_cutting_is_identical_for_two_different_arms():
    """The property the whole design rests on. Two arms differ in audio content but must
    produce the same number of clips, of the same lengths, at the same offsets."""
    fixed = [(0.5, 1.5), (2.0, 3.0)]
    arm_a = np.concatenate([_silence(0.5), _burst(1.0), _silence(0.5), _burst(1.0), _silence(1.0)])
    arm_b = arm_a * 0.25          # a different arm: same transmissions, different audio

    cuts_a = segments.cut(arm_a, RATE, fixed)
    cuts_b = segments.cut(arm_b, RATE, fixed)
    assert [len(c) for c in cuts_a] == [len(c) for c in cuts_b]
    assert len(cuts_a) == 2


def test_a_segment_past_the_end_is_clipped_not_crashed():
    audio = _burst(1.0)
    cuts = segments.cut(audio, RATE, [(0.5, 99.0)])
    assert len(cuts) == 1 and len(cuts[0]) > 0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_iq_segments.py -v`
Expected: FAIL, `ImportError: cannot import name 'segments'`

- [ ] **Step 3: Implement segmentation**

Create `server/iq/segments.py`:

```python
"""Cutting replayed audio into clips at FIXED boundaries.

Computed once, from one reference arm, and reused unchanged by every other arm. That is not
an optimisation, it is what makes the comparison valid: bench_prompt_ab.py pairs arms on
clip_id, so boundaries that drifted between arms would silently compare different
transmissions under the same id.

The cut list is a plain two-column text file so it can be inspected and corrected by hand.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


def detect_segments(audio: np.ndarray, rate: float, threshold: float = 0.02,
                    min_ms: float = 400.0, hang_ms: float = 600.0,
                    pad_ms: float = 300.0) -> list[tuple[float, float]]:
    """Find transmissions as (start_s, end_s), by energy with a hangover.

    `hang_ms` bridges the pauses inside a single transmission so one turn does not become
    five clips. `pad_ms` widens each segment at both ends, because the opening syllables --
    where the vessel name almost always is -- start before the energy threshold is crossed.
    """
    audio = np.asarray(audio, dtype=np.float64)
    if len(audio) == 0:
        return []

    win = max(1, int(rate * 0.02))                      # 20 ms frames
    n_frames = len(audio) // win
    if n_frames == 0:
        return []
    frames = audio[:n_frames * win].reshape(n_frames, win)
    rms = np.sqrt(np.mean(frames ** 2, axis=1))
    active = rms >= threshold

    hang_frames = max(1, int(hang_ms / 20.0))
    out: list[tuple[float, float]] = []
    start = None
    silent = 0

    for i, is_active in enumerate(active):
        if is_active:
            if start is None:
                start = i
            silent = 0
        elif start is not None:
            silent += 1
            if silent >= hang_frames:
                out.append((start, i - silent + 1))
                start = None
                silent = 0
    if start is not None:
        out.append((start, len(active)))

    pad = pad_ms / 1000.0
    limit = len(audio) / rate
    result = []
    for a, b in out:
        start_s, end_s = a * 0.02, b * 0.02
        if end_s - start_s < min_ms / 1000.0:
            continue
        result.append((max(0.0, start_s - pad), min(limit, end_s + pad)))
    return result


def write_segments(path: str | Path, segments: list[tuple[float, float]]) -> None:
    lines = "".join(f"{a:.3f}\t{b:.3f}\n" for a, b in segments)
    Path(path).write_text(lines, encoding="utf-8")


def read_segments(path: str | Path) -> list[tuple[float, float]]:
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        a, b = line.split("\t")
        out.append((float(a), float(b)))
    return out


def cut(audio: np.ndarray, rate: float,
        segments: list[tuple[float, float]]) -> list[np.ndarray]:
    """Slice `audio` at the given boundaries. Out-of-range ends are clipped, not an error:
    arms can differ in length by a sample or two after resampling."""
    audio = np.asarray(audio, dtype=np.float64)
    out = []
    for start_s, end_s in segments:
        a = max(0, int(start_s * rate))
        b = min(len(audio), int(end_s * rate))
        if b > a:
            out.append(audio[a:b])
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_iq_segments.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add server/iq/segments.py server/tests/test_iq_segments.py
git commit -m "Cut replayed audio at fixed boundaries shared by every arm"
```

---

### Task 6: The `iq_replay.py` driver

**Files:**
- Create: `server/iq_replay.py`
- Create: `server/tests/test_iq_replay.py`

**Interfaces:**
- Consumes: `iq.baseband.read_baseband`, `iq.demod.demodulate`, `iq.demod.apply_squelch`, `iq.plugin_dsp.process_chunk`, `iq.segments.{detect_segments, read_segments, write_segments, cut}`.
- Produces: `write_clip(path, samples, rate) -> None`; `replay_arm(iq, iq_rate, out_dir, bandwidth_hz, offset_hz, squelch_db, segments, audio_rate) -> int` returning the clip count; a `main()` CLI.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/test_iq_replay.py`:

```python
"""End-to-end: IQ in, a captures-style directory of clips out.

The output layout is not incidental. bench.py::discover_clips globs '*_sent.wav' and takes
the clip id from the filename, so writing exactly that layout means bench.py and
bench_prompt_ab.py score the arms unchanged.
"""

import sys
import wave
from pathlib import Path

import numpy as np
import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import bench  # noqa: E402
import iq_replay  # noqa: E402
from iq import baseband  # noqa: E402

IQ_RATE = 250_000.0


def _speechlike(seconds, rate=8000.0):
    """Two tones and an envelope -- enough structure for the energy segmenter to find."""
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 400 * t) + 0.5 * np.sin(2 * np.pi * 1800 * t)) * 0.5


def _two_transmissions():
    gap = np.zeros(int(1.5 * 8000))
    audio = np.concatenate([np.zeros(4000), _speechlike(1.2), gap, _speechlike(1.2), np.zeros(4000)])
    return baseband.synth_nfm(audio, 8000.0, IQ_RATE, deviation_hz=3000.0)


def test_a_clip_is_written_in_the_format_bench_expects(tmp_path):
    iq_replay.write_clip(tmp_path / "0000_sent.wav", np.array([0.0, 0.5, -0.5]), 16000)
    with wave.open(str(tmp_path / "0000_sent.wav"), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)


def test_bench_discovers_what_the_harness_writes(tmp_path):
    """The integration that matters: the existing scorer must find these clips unchanged."""
    iq = _two_transmissions()
    segs = iq_replay.replay_arm(iq, IQ_RATE, tmp_path, bandwidth_hz=16_000.0,
                                offset_hz=0.0, squelch_db=None, segments=None)
    assert segs >= 1
    found = bench.discover_clips(tmp_path)
    assert len(found) == segs
    assert all(cid.isdigit() and len(cid) == 4 for cid, _ in found)


def test_every_arm_produces_the_same_clip_ids(tmp_path):
    """The property the paired comparison depends on. Different bandwidths, same clips."""
    iq = _two_transmissions()
    shared = iq_replay.plan_segments(iq, IQ_RATE, bandwidth_hz=25_000.0, offset_hz=0.0)

    ids = []
    for bw, name in ((12_500.0, "narrow"), (25_000.0, "wide")):
        out = tmp_path / name
        iq_replay.replay_arm(iq, IQ_RATE, out, bandwidth_hz=bw, offset_hz=0.0,
                             squelch_db=None, segments=shared)
        ids.append([cid for cid, _ in bench.discover_clips(out)])

    assert ids[0] == ids[1], "clip ids must be identical across arms or pairing is invalid"
    assert len(ids[0]) >= 1


def test_arms_actually_differ_in_content(tmp_path):
    """Identical ids must not mean identical audio, or nothing is being measured."""
    iq = _two_transmissions()
    shared = iq_replay.plan_segments(iq, IQ_RATE, bandwidth_hz=25_000.0, offset_hz=0.0)

    audio = {}
    for bw, name in ((12_500.0, "narrow"), (25_000.0, "wide")):
        out = tmp_path / name
        iq_replay.replay_arm(iq, IQ_RATE, out, bandwidth_hz=bw, offset_hz=0.0,
                             squelch_db=None, segments=shared)
        with wave.open(str(out / "0000_sent.wav"), "rb") as w:
            audio[name] = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")

    n = min(len(audio["narrow"]), len(audio["wide"]))
    assert not np.array_equal(audio["narrow"][:n], audio["wide"][:n])
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `py -m pytest server/tests/test_iq_replay.py -v`
Expected: FAIL, `ModuleNotFoundError: No module named 'iq_replay'`

- [ ] **Step 3: Implement the driver**

Create `server/iq_replay.py`:

```python
#!/usr/bin/env python3
"""Replay one raw-IQ recording through different demodulator settings.

Why this exists: the plugin is an IRealProcessor, so it taps DEMODULATED audio and every
receiver setting is baked in before anything this project controls sees it. Comparing two
receiver settings the ordinary way needs two captures on two days, which is unpaired --
different ships called -- and needs the references hand-verified twice. Replaying one
recording gives identical RF into both arms and verifies references once.

Output is a captures-style directory of NNNN_sent.wav, so the existing scoring stack runs
over it unchanged:

    py iq_replay.py capture.wav --freq 160650000 --bandwidth 12500 --out arms/bw12k
    py iq_replay.py capture.wav --freq 160650000 --bandwidth 25000 --out arms/bw25k \\
        --segments arms/bw12k/segments.txt
    py bench.py --captures arms/bw12k --references references-iq.txt --out-json bw12k.json
    py bench.py --captures arms/bw25k --references references-iq.txt --out-json bw25k.json
    py bench_prompt_ab.py bw12k=bw12k.json bw25k=bw25k.json

Pass --segments on every arm after the first, so all arms share one cut list. Without that
each arm segments its own audio, clip ids stop naming the same transmission, and
bench_prompt_ab.py compares unrelated clips while still reporting a confident number.

WHAT THIS CANNOT MEASURE, so that silence is never mistaken for a null result:
  * RF gain -- applied before the ADC and baked into the recording. Sweeping it needs a
    fresh capture per setting, and those can never be paired because the traffic differs.
  * The SDR# audio-NR plugins (AudioProcessor, AudioEqualizer) -- other plugins' algorithms,
    downstream of the tap point.
"""

from __future__ import annotations

import argparse
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from iq import baseband, demod, plugin_dsp, segments as segmod  # noqa: E402

WHISPER_RATE = 16_000


def write_clip(path: str | Path, samples: np.ndarray, rate: int = WHISPER_RATE) -> None:
    """Mono 16-bit PCM, the format ChunkRecorder writes and bench.py reads."""
    pcm = np.clip(np.asarray(samples) * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())


def plan_segments(iq: np.ndarray, iq_rate: float, bandwidth_hz: float,
                  offset_hz: float, audio_rate: float = demod.DEFAULT_AUDIO_RATE
                  ) -> list[tuple[float, float]]:
    """Compute the shared cut list from one reference arm.

    Use the WIDEST bandwidth under test: it preserves the most information, so it both
    segments most reliably and is the easiest arm to transcribe by ear when the references
    are made. The reference text is a property of the transmission, not of the arm, so this
    introduces no bias toward any arm.
    """
    audio = demod.demodulate(iq, iq_rate, bandwidth_hz, offset_hz=offset_hz,
                             audio_rate=audio_rate)
    return segmod.detect_segments(audio, audio_rate)


def replay_arm(iq: np.ndarray, iq_rate: float, out_dir: str | Path, bandwidth_hz: float,
               offset_hz: float, squelch_db: float | None,
               segments: list[tuple[float, float]] | None,
               audio_rate: float = demod.DEFAULT_AUDIO_RATE) -> int:
    """Demodulate one arm and write its clips. Returns the number written."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    audio = demod.demodulate(iq, iq_rate, bandwidth_hz, offset_hz=offset_hz,
                             audio_rate=audio_rate)
    audio = demod.apply_squelch(audio, audio_rate, squelch_db)

    if segments is None:
        segments = segmod.detect_segments(audio, audio_rate)
    segmod.write_segments(out_dir / "segments.txt", segments)

    # Count clips WRITTEN, not segments requested: cut() drops a slice that lands entirely
    # past the end of this arm's audio, and arms can differ by a sample or two after
    # resampling. Returning len(segments) would over-report and disagree with what
    # bench.py::discover_clips finds.
    written = 0
    for index, chunk in enumerate(segmod.cut(audio, audio_rate, segments)):
        clip = plugin_dsp.process_chunk(chunk, audio_rate, float(WHISPER_RATE))
        write_clip(out_dir / f"{index:04d}_sent.wav", clip)
        written += 1
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="SDR# baseband wav (interleaved I/Q)")
    ap.add_argument("--out", required=True, help="output directory for this arm")
    ap.add_argument("--bandwidth", type=float, required=True, help="channel bandwidth in Hz")
    ap.add_argument("--freq", type=float, default=None,
                    help="channel frequency in Hz (default: the recording's centre)")
    ap.add_argument("--squelch-db", type=float, default=None,
                    help="squelch threshold in dBFS; omit for squelch off")
    ap.add_argument("--segments", default=None,
                    help="reuse this cut list; pass it on every arm after the first")
    ap.add_argument("--audio-rate", type=float, default=demod.DEFAULT_AUDIO_RATE,
                    help="SDR# audio rate to emulate (default 37500, this setup's rate)")
    args = ap.parse_args(argv)

    iq, iq_rate, centre = baseband.read_baseband(args.capture)
    if args.freq is not None and centre is None:
        print("warning: no centre frequency in the filename; treating --freq as the offset",
              file=sys.stderr)
        offset = args.freq
    elif args.freq is not None:
        offset = args.freq - centre
    else:
        offset = 0.0

    shared = segmod.read_segments(args.segments) if args.segments else None
    count = replay_arm(iq, iq_rate, args.out, args.bandwidth, offset,
                       args.squelch_db, shared, args.audio_rate)

    print(f"{count} clips -> {args.out}  "
          f"(bandwidth {args.bandwidth:.0f} Hz, offset {offset:+.0f} Hz, "
          f"squelch {'off' if args.squelch_db is None else f'{args.squelch_db:.0f} dBFS'})")
    if shared is None:
        print(f"cut list written to {Path(args.out) / 'segments.txt'} -- "
              f"pass it as --segments on every other arm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `py -m pytest server/tests/test_iq_replay.py -v`
Expected: 4 passed

- [ ] **Step 5: Run the whole suite**

Run: `py -m pytest server/tests -q`
Expected: all pass (468 existing + 30 new)

- [ ] **Step 6: Document the harness**

Append to `docs/design-notes.md`, immediately before the `## Testing` section:

```markdown
## Measuring receiver settings by IQ replay (2026-08-08)

The plugin taps demodulated audio, so every receiver setting is baked in before anything
here sees it, and none had ever been measured. `server/iq_replay.py` replays one raw-IQ
recording through different demodulator settings: identical RF into every arm, one variable
changed, references hand-verified once.

Arms are written as captures-style directories, so `bench.py` and `bench_prompt_ab.py` score
them unchanged — including the bootstrap confidence interval, which matters because a bare
WER delta of a point or two carries no information.

Two things that are easy to get wrong and are pinned by tests:

* **Segmentation is computed once and shared.** Per-arm VAD would move clip boundaries, and
  `bench_prompt_ab.py` pairs on `clip_id`, so it would compare different transmissions under
  the same id and still print a number.
* **The plugin's DSP chain is transcribed, not improved.** `iq/plugin_dsp.py` is pinned
  against real `_raw.wav` -> `_sent.wav` pairs at correlation 1.000000. A first attempt
  scored 0.946 because `Decimator.LinearResample` steps by `(len-1)/(outLen-1)`, not by
  `fromRate/toRate`.

Cannot be measured this way: RF gain (applied before the ADC, so baked into the recording)
and the SDR# audio-NR plugins (downstream of the tap point).
```

- [ ] **Step 7: Commit**

```bash
git add server/iq_replay.py server/tests/test_iq_replay.py docs/design-notes.md
git commit -m "Replay IQ through different receiver settings into scoreable arms"
```

---

## Operator runbook (after the code is in)

1. **Test capture first.** Record two minutes of baseband at 250 kSPS and confirm the sample
   count matches the duration; some RTL-SDR devices drop samples below 900 kSPS and the
   failure is silent. Only then record the full hour.
2. Record ~60 minutes of Ch 01 (160.650 MHz) baseband at 250 kSPS, ~3.6 GB.
3. Build the widest arm first — it defines the shared cut list and is the arm to listen to
   when writing references:
   `py iq_replay.py capture.wav --freq 160650000 --bandwidth 25000 --out arms/bw25k`
4. Hand-verify `references-iq.txt` against `arms/bw25k`, square brackets for annotations
   (`bench.py::_normalize` strips `[...]` only).
5. Build the other arms, reusing the cut list:
   `--bandwidth 12500 --out arms/bw12k --segments arms/bw25k/segments.txt`, likewise 16000,
   and a squelch arm with `--squelch-db -40`.
6. Score each arm with `bench.py`, then compare with `bench_prompt_ab.py`.

## Self-review notes

- **Spec coverage:** stages 1–8 map to Tasks 3 (1–5), 4 (6), 2 (7), 5–6 (8); fixed
  segmentation → Task 5; scipy dependency → Task 1; testing section → every task; capture
  parameters and "cannot answer" → the runbook and `iq_replay.py`'s docstring.
- **Spec correction carried into the Global Constraints:** the spec says 48 kHz throughout;
  production is 37,500 Hz, discovered from the `_raw.wav` headers. The plan supersedes the
  spec on this point and the rate is a parameter everywhere rather than a constant.
- **Deferred deliberately:** the spec's success criterion 3–4 (a real capture replayed and
  scored) needs the recording, so it lives in the runbook, not in a task.
