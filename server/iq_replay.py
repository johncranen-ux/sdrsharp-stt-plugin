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

Cut boundaries come from RF channel power measured on the IQ, never from the demodulated
audio -- an FM discriminator is exactly as loud with no carrier as with one, so an
audio-amplitude gate cuts dead air into clips. Same for --squelch-over-floor-db. See
iq/segments.py::detect_segments.

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

# How much silence to write in place of a zero-length clip (see write_clip). Long enough
# that no downstream reader mistakes the file for empty/malformed, short enough to be
# negligible next to a multi-second real clip.
_PLACEHOLDER_SILENCE_S = 0.02


def write_clip(path: str | Path, samples: np.ndarray, rate: int = WHISPER_RATE) -> None:
    """Mono 16-bit PCM, the format ChunkRecorder writes and bench.py reads.

    `samples` can arrive zero-length: segments.cut() (task 5) deliberately returns one entry
    per requested segment, including an empty array for a segment that lies entirely past a
    shorter arm's (slightly shorter after resampling) end -- dropping that entry instead
    would shift every later clip_id in that one arm and desync the pairing
    bench_prompt_ab.py relies on. A wav with zero frames is not guaranteed to be a
    well-formed file for whatever reads it next (bench.py posts the raw bytes to the STT
    server), so an empty `samples` is replaced with a short burst of silence rather than
    written as-is. Silence, not a dropped file: the index still exists, the content is
    honestly "nothing recorded here in this arm".
    """
    samples = np.asarray(samples)
    if len(samples) == 0:
        samples = np.zeros(max(1, int(_PLACEHOLDER_SILENCE_S * rate)))
    pcm = np.clip(samples * 32767.0, -32768, 32767).astype("<i2")
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(rate))
        w.writeframes(pcm.tobytes())


def _demodulate_capture(iq, iq_rate: float, bandwidth_hz: float, offset_hz: float,
                        audio_rate: float) -> tuple[np.ndarray, np.ndarray, float]:
    """Demodulate either an in-memory array or a stream of blocks.

    Returns (audio, channel_power_db, power_frame_rate). The power track comes back
    alongside the audio because everything that has to decide "is anyone transmitting?" --
    segmentation and the squelch -- has to ask the RF domain, not the audio. See
    segments.detect_segments.

    `iq` is a path for real captures -- an hour of 250 kSPS is 14.4 GB as complex128, so it
    is streamed from disk and only the demodulated audio (1.1 GB/hour at 37500 Hz as
    float64) and the power track (29 MB/hour) are held. Tests pass an array.
    """
    d = demod.Demodulator(iq_rate, bandwidth_hz, offset_hz=offset_hz, audio_rate=audio_rate)
    if isinstance(iq, (str, Path)):
        parts = [d.process(block) for block in baseband.iter_baseband(iq)]
    else:
        parts = [d.process(np.asarray(iq))]
    parts.append(d.flush())
    audio = np.concatenate([p for p in parts if len(p)]) if parts else np.zeros(0)
    return audio, d.power_db, d.power_frame_rate


def plan_segments(iq, iq_rate: float, bandwidth_hz: float,
                  offset_hz: float, audio_rate: float = demod.DEFAULT_AUDIO_RATE
                  ) -> list[tuple[float, float]]:
    """Compute the shared cut list from one reference arm.

    Use the WIDEST bandwidth under test: it preserves the most information, so it both
    segments most reliably and is the easiest arm to transcribe by ear when the references
    are made. The reference text is a property of the transmission, not of the arm, so this
    introduces no bias toward any arm.
    """
    _, power_db, power_rate = _demodulate_capture(iq, iq_rate, bandwidth_hz, offset_hz,
                                                  audio_rate)
    return segmod.detect_segments(power_db, power_rate)


def replay_arm(iq: np.ndarray, iq_rate: float, out_dir: str | Path, bandwidth_hz: float,
               offset_hz: float, squelch_over_floor_db: float | None,
               segments: list[tuple[float, float]] | None,
               audio_rate: float = demod.DEFAULT_AUDIO_RATE) -> int:
    """Demodulate one arm and write its clips. Returns the number written.

    `squelch_over_floor_db` is how far above the capture's own measured noise floor the
    gate opens -- what a squelch knob does -- or None for squelch off.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    audio, power_db, power_rate = _demodulate_capture(iq, iq_rate, bandwidth_hz, offset_hz,
                                                      audio_rate)

    # Segment BEFORE squelching, and from the power track either way. The cut list must
    # describe where the transmissions are, which is a fact about the RF and not about
    # whether this particular arm has its gate switched on.
    if segments is None:
        segments = segmod.detect_segments(power_db, power_rate)
    segmod.write_segments(out_dir / "segments.txt", segments)

    if squelch_over_floor_db is not None:
        threshold_db = segmod.noise_floor_db(power_db) + squelch_over_floor_db
        audio = demod.apply_squelch(audio, audio_rate, threshold_db, power_db, power_rate)

    # cut() returns exactly one entry per requested segment (segments.cut, task 5) and
    # write_clip never skips a zero-length one -- it writes silence instead (see write_clip's
    # docstring) -- so `written` always equals len(segments) for every arm. Counted here
    # rather than assumed, so a future change to either invariant shows up as a mismatch
    # instead of silently producing arms with different clip_id sets.
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
    ap.add_argument("--squelch-over-floor-db", type=float, default=None,
                    help="squelch opens this many dB above the capture's measured RF noise "
                         "floor (a squelch knob); omit for squelch off")
    ap.add_argument("--segments", default=None,
                    help="reuse this cut list; pass it on every arm after the first")
    ap.add_argument("--audio-rate", type=float, default=demod.DEFAULT_AUDIO_RATE,
                    help="SDR# audio rate to emulate (default 37500, this setup's rate)")
    args = ap.parse_args(argv)

    # Header only. The samples are streamed later; a real capture is gigabytes.
    info = baseband.open_baseband(args.capture)
    iq, iq_rate, centre = args.capture, info.rate, info.centre_hz
    print(f"{args.capture}: {info.frames/info.rate/60:.1f} min at {info.rate/1000:.0f} kSPS, "
          f"{info.bits}-bit, centre "
          f"{'unknown' if centre is None else f'{centre/1e6:.4f} MHz'}")

    if args.freq is not None and centre is None:
        print("warning: no centre frequency in the filename; treating --freq as the offset",
              file=sys.stderr)
        offset = args.freq
    elif args.freq is not None:
        offset = args.freq - centre
    else:
        offset = 0.0

    shared = segmod.read_segments(args.segments) if args.segments else None
    squelch = args.squelch_over_floor_db
    count = replay_arm(iq, iq_rate, args.out, args.bandwidth, offset,
                       squelch, shared, args.audio_rate)

    print(f"{count} clips -> {args.out}  "
          f"(bandwidth {args.bandwidth:.0f} Hz, offset {offset:+.0f} Hz, "
          f"squelch {'off' if squelch is None else f'floor +{squelch:.0f} dB'})")

    # Both ways this can be confidently wrong are silent -- see segments.duty_warning.
    cuts = segmod.read_segments(Path(args.out) / "segments.txt")
    covered = sum(b - a for a, b in cuts)
    capture_s = info.frames / info.rate
    print(f"speech: {covered/60:.1f} of {capture_s/60:.1f} min "
          f"({100*covered/capture_s:.1f}% duty)")
    complaint = segmod.duty_warning(cuts, capture_s)
    if complaint:
        print(f"warning: {complaint}", file=sys.stderr)
    if shared is None:
        print(f"cut list written to {Path(args.out) / 'segments.txt'} -- "
              f"pass it as --segments on every other arm")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
