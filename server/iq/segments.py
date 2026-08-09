"""Cutting replayed audio into clips at FIXED boundaries.

Boundaries are computed once, from one reference arm, and reused unchanged by every other
arm. That is not an optimisation, it is what makes the comparison valid: bench_prompt_ab.py
pairs arms on clip_id, so boundaries that drifted between arms would silently compare
different transmissions under the same id.

Where the boundaries come FROM is the other half of the design, and it is the half that was
wrong first time round: they come from RF channel power measured on the IQ, never from the
demodulated audio. See detect_segments for why the audio domain cannot work.

The cut list is a plain two-column text file so it can be inspected and corrected by hand.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np


# The noise floor is read off the capture as a low percentile of its own channel power.
# 20th, not the minimum: the minimum is one unlucky frame, while a marine VHF hour is ~95%
# dead air, so the 20th percentile is solidly inside the floor with room to spare even on a
# far busier channel than this one. Taken from the survey script that measured the real
# 2026-08-08 capture correctly (23 transmissions, 4.8% duty) while this module was reporting
# 42 clips covering 57.6 of 60.1 minutes.
_FLOOR_PERCENTILE = 20.0

# How far above the floor a frame has to sit to count as a carrier. Measured on the real
# 2026-08-08 hour capture: floor -38.9 dB, strongest transmission -20.0 dB, so 10 dB sits
# roughly midway in a ~19 dB gap. (A synthetic fixture's gap is ~32 dB, which would make
# this look far safer than it is -- real signals are weaker, and a marginal one is weaker
# still.) The cross-check that this is the right neighbourhood is not the margin itself but
# the result: at 10 dB, 4.83% of the hour reads as active, against 4.8% measured
# independently by the survey script over a completely different code path (FFT bin power
# at the channel offset, no channel filter, no demodulator).
_DEFAULT_MARGIN_DB = 10.0


def noise_floor_db(power_db: np.ndarray, percentile: float = _FLOOR_PERCENTILE) -> float:
    """The capture's own noise floor, in dB. Measured rather than configured, because the
    absolute level depends on RF gain -- a fixed threshold would need retuning per capture
    and would mis-segment silently when it wasn't."""
    power_db = np.asarray(power_db, dtype=np.float64)
    if len(power_db) == 0:
        return 0.0
    return float(np.percentile(power_db, percentile))


def detect_segments(power_db: np.ndarray, frame_rate: float,
                    threshold_db: float | None = None,
                    margin_db: float = _DEFAULT_MARGIN_DB,
                    min_ms: float = 400.0, hang_ms: float = 600.0,
                    pad_ms: float = 300.0) -> list[tuple[float, float]]:
    """Find transmissions as (start_s, end_s) from an RF channel-power track.

    `power_db` is Demodulator.power_db: channel power measured on the IQ, before the
    discriminator. NOT audio. Segmenting on demodulated-audio amplitude cannot work at all,
    and the failure does not look like a failure -- an FM discriminator emits full-scale
    hiss with no carrier, so dead air and speech leave it equally loud (measured on
    synthetic IQ: dead air 1.44x LOUDER than speech). Gating on it cut 57.6 of 60.1 minutes
    of a real hour into "clips", with peak-normalisation downstream hiding the evidence.
    Carrier presence lives in RF amplitude, which is exactly what the discriminator
    discards, and it is why radios have a squelch at all.

    `threshold_db` defaults to the measured noise floor plus `margin_db`, which makes the
    result independent of RF gain. Pass it explicitly to override.

    `hang_ms` bridges the pauses inside a single transmission so one turn does not become
    five clips. `pad_ms` widens each segment at both ends, because the opening syllables --
    where the vessel name almost always is -- start before the threshold is crossed.
    """
    power_db = np.asarray(power_db, dtype=np.float64)
    if len(power_db) == 0:
        return []

    if threshold_db is None:
        threshold_db = noise_floor_db(power_db) + margin_db
    active = power_db >= threshold_db

    frame_s = 1.0 / frame_rate
    hang_frames = max(1, int(round(hang_ms * 1e-3 * frame_rate)))
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
        # The recording can end mid-hangover: fewer than hang_frames of trailing silence.
        # A terminated segment already excludes its hangover (end = i - silent + 1 above);
        # do the same here, or the last clip of every recording carries a slab of dead air
        # that then gets pad_ms added on top of it.
        out.append((start, len(active) - silent))

    pad = pad_ms / 1000.0
    limit = len(power_db) * frame_s
    result = []
    for a, b in out:
        start_s, end_s = a * frame_s, b * frame_s
        if end_s - start_s < min_ms / 1000.0:
            continue
        result.append((max(0.0, start_s - pad), min(limit, end_s + pad)))
    return result


# A voice channel busier than this is not traffic, it is a broken measurement. Marine Ch 01
# runs ~5% duty overnight and the busiest daytime hour measured so far is nowhere near 50%.
_IMPLAUSIBLE_DUTY = 0.5


def duty_warning(segments: list[tuple[float, float]], capture_s: float) -> str | None:
    """A message if this cut list cannot be right, else None.

    Both ways of being wrong here are silent, which is why this exists: the audio-RMS
    segmenter this module replaced covered 57.6 of 60.1 minutes and reported 42 healthy
    clips, and at the other end a threshold set too high produces ZERO segments and scores
    as a clean run. Measured on the real hour capture, the margin only has to move from
    12 dB to 14 dB to fall off that cliff (39 segments -> 0), so this is a live risk on a
    capture with weaker traffic, not a hypothetical one.

    Advisory only -- it returns text rather than raising, because a genuinely unusual
    capture is the operator's call to make, not this function's.
    """
    if capture_s <= 0:
        return None
    if not segments:
        return ("no transmissions found in the whole capture -- the threshold is probably "
                "above the traffic; try a smaller margin_db, or check the tuning offset")
    covered = sum(b - a for a, b in segments)
    duty = covered / capture_s
    if duty > _IMPLAUSIBLE_DUTY:
        return (f"segments cover {100 * duty:.1f}% of the capture ({covered / 60:.1f} of "
                f"{capture_s / 60:.1f} min) -- a voice channel is not that busy; the "
                f"threshold is probably below the noise floor")
    return None


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
    arms can differ in length by a sample or two after resampling.

    Every segment yields exactly one entry, in order -- including an empty array when the
    segment starts past a shorter arm's end. clip_id downstream (bench_prompt_ab.py) is
    assigned by enumeration order, so dropping an entry here would shift every later index
    in that arm and silently pair unrelated transmissions across arms under the same id.
    """
    audio = np.asarray(audio, dtype=np.float64)
    n = len(audio)
    out = []
    for start_s, end_s in segments:
        a = min(n, max(0, int(start_s * rate)))
        b = min(n, max(a, int(end_s * rate)))
        out.append(audio[a:b])
    return out
