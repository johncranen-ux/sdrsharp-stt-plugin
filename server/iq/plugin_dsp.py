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
