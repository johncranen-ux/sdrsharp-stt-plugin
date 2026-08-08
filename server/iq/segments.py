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
