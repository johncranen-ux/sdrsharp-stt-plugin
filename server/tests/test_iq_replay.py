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
    # deviation_hz=3000 (the brief's original value) measures at ~0.009 RMS after the
    # demodulator's default 750us de-emphasis -- below detect_segments' default 0.02 RMS
    # threshold, so nothing is ever detected as a transmission and every test using this
    # fixture fails the same way regardless of what iq_replay.py does. 6000 clears the
    # threshold with margin and reliably yields the intended two segments (checked over
    # 6000-20000 Hz); it's still well inside the demodulator's Nyquist for both bandwidths
    # under test here (12.5/25 kHz).
    return baseband.synth_nfm(audio, 8000.0, IQ_RATE, deviation_hz=6000.0)


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


def test_write_clip_never_writes_a_zero_sample_wav(tmp_path):
    """segments.cut() (task 5) can hand back a zero-length array for a segment that lies past
    a shorter arm's end. bench.py posts the clip's raw bytes to the STT server, and a
    zero-frame wav is not guaranteed to be a well-formed file to receive -- write_clip must
    turn that into silence, not an empty RIFF payload."""
    iq_replay.write_clip(tmp_path / "0000_sent.wav", np.zeros(0), 16000)
    with wave.open(str(tmp_path / "0000_sent.wav"), "rb") as w:
        assert w.getnframes() > 0
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert np.all(pcm == 0)


def test_a_segment_past_every_arms_end_still_gets_a_valid_indexed_clip(tmp_path):
    """The property test_every_arm_produces_the_same_clip_ids depends on, forced: a segment
    placed well past the synthetic capture's end must still produce a real, indexed,
    non-zero-sample wav -- not a dropped index (which would desync clip_id between arms that
    do and don't hit this case) and not a malformed empty file."""
    iq = _two_transmissions()
    shared = iq_replay.plan_segments(iq, IQ_RATE, bandwidth_hz=25_000.0, offset_hz=0.0)
    segments_with_tail = shared + [(100.0, 101.0)]  # far past a ~5s synthetic capture

    written = iq_replay.replay_arm(iq, IQ_RATE, tmp_path, bandwidth_hz=25_000.0,
                                   offset_hz=0.0, squelch_db=None,
                                   segments=segments_with_tail)
    assert written == len(segments_with_tail)

    last_id = f"{len(segments_with_tail) - 1:04d}"
    with wave.open(str(tmp_path / f"{last_id}_sent.wav"), "rb") as w:
        assert w.getnframes() > 0
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert np.all(pcm == 0)

    found = {cid for cid, _ in bench.discover_clips(tmp_path)}
    assert last_id in found
