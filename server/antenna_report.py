#!/usr/bin/env python3
"""Compare RF/antenna performance between two BasebandRecorder IQ captures.

Built for the outdoor-antenna swap: reuses the same channel-power measurement the
iq_replay harness relies on (iq.demod.Demodulator, iq.segments.detect_segments) so the
"new capture" and "old capture" numbers come from one code path -- self-consistent even
if they don't match SDR#'s own on-screen dBFS reading, which was read by eye in the
2026-08-08 baseline.

Only the antenna should differ between the two captures: same centre frequency, same RF
gain, same bandwidth setting in SDR#, ideally the same time of day. If gain was not held
fixed, the noise-floor delta conflates antenna gain with receiver gain and cannot be
trusted alone -- the per-segment SNR (peak - floor, both measured on the same capture) is
the more robust number because it cancels a gain difference that shifts both equally.

Usage:
    py antenna_report.py new_capture.wav --baseline old_capture.wav
    py antenna_report.py new_capture.wav                    # no comparison, just report
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from iq import baseband, demod, segments as segmod  # noqa: E402

DEFAULT_CHANNEL_HZ = 160_650_000.0   # Ch 01, the baseline's reference channel
DEFAULT_BANDWIDTH_HZ = 12_500.0      # SDR#'s production NFM bandwidth setting


def analyze(path: str | Path, channel_hz: float, bandwidth_hz: float) -> dict:
    info = baseband.open_baseband(path)
    if info.centre_hz is None:
        raise ValueError(f"{path}: no centre frequency in filename; can't locate the channel")
    offset_hz = channel_hz - info.centre_hz

    d = demod.Demodulator(info.rate, bandwidth_hz, offset_hz=offset_hz)
    for block in baseband.iter_baseband(path):
        d.process(block)
    d.flush()
    power_db = d.power_db

    floor = segmod.noise_floor_db(power_db)
    peak = float(np.max(power_db)) if len(power_db) else float("nan")
    segs = segmod.detect_segments(power_db, d.power_frame_rate)

    seg_snrs = []
    for start_s, end_s in segs:
        i0 = int(start_s * d.power_frame_rate)
        i1 = max(i0 + 1, int(end_s * d.power_frame_rate))
        seg_peak = float(np.max(power_db[i0:i1])) if i1 <= len(power_db) and i1 > i0 else float("nan")
        seg_snrs.append(seg_peak - floor)

    return {
        "path": str(path),
        "duration_s": info.frames / info.rate,
        "rate": info.rate,
        "centre_hz": info.centre_hz,
        "channel_hz": channel_hz,
        "floor_db": floor,
        "peak_db": peak,
        "peak_snr_db": peak - floor,
        "n_segments": len(segs),
        "seg_snrs_db": seg_snrs,
    }


def _fmt(r: dict) -> str:
    lines = [
        f"{r['path']}",
        f"  {r['duration_s']/60:.1f} min at {r['rate']/1000:.0f} kSPS, "
        f"centre {r['centre_hz']/1e6:.4f} MHz, channel {r['channel_hz']/1e6:.4f} MHz",
        f"  noise floor: {r['floor_db']:.1f} dB   peak: {r['peak_db']:.1f} dB   "
        f"peak SNR: {r['peak_snr_db']:.1f} dB",
        f"  transmissions detected: {r['n_segments']}",
    ]
    if r["seg_snrs_db"]:
        snrs = np.array(r["seg_snrs_db"])
        lines.append(f"  segment SNR: median {np.median(snrs):.1f} dB, "
                     f"min {snrs.min():.1f} dB, max {snrs.max():.1f} dB")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("capture", help="new SDR# baseband wav (interleaved I/Q)")
    ap.add_argument("--baseline", default=None, help="old capture to compare against")
    ap.add_argument("--freq", type=float, default=DEFAULT_CHANNEL_HZ,
                    help=f"channel frequency in Hz (default {DEFAULT_CHANNEL_HZ:.0f}, Ch 01)")
    ap.add_argument("--bandwidth", type=float, default=DEFAULT_BANDWIDTH_HZ,
                    help=f"channel bandwidth in Hz (default {DEFAULT_BANDWIDTH_HZ:.0f})")
    args = ap.parse_args(argv)

    new = analyze(args.capture, args.freq, args.bandwidth)
    print("=== new capture ===")
    print(_fmt(new))

    if args.baseline:
        old = analyze(args.baseline, args.freq, args.bandwidth)
        print("\n=== baseline ===")
        print(_fmt(old))

        print("\n=== delta (new - baseline) ===")
        print(f"  noise floor: {new['floor_db'] - old['floor_db']:+.1f} dB")
        print(f"  peak SNR:    {new['peak_snr_db'] - old['peak_snr_db']:+.1f} dB")
        if new["seg_snrs_db"] and old["seg_snrs_db"]:
            print(f"  median segment SNR: "
                  f"{np.median(new['seg_snrs_db']) - np.median(old['seg_snrs_db']):+.1f} dB")
        print(f"  transmissions caught: {new['n_segments']} vs {old['n_segments']} "
              f"over comparable capture length")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
