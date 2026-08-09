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
from iq import baseband, segments  # noqa: E402

IQ_RATE = 250_000.0


def _speechlike(seconds, rate=8000.0):
    """Two tones and an envelope -- enough structure for the energy segmenter to find."""
    t = np.arange(int(seconds * rate)) / rate
    return (np.sin(2 * np.pi * 400 * t) + 0.5 * np.sin(2 * np.pi * 1800 * t)) * 0.5


_NOISE_DB = -20.0            # receiver noise floor, ~32 dB under a carrier in-channel
_TALK_S = 1.2
_CAPTURE_S = 0.5 + _TALK_S + 1.5 + _TALK_S + 0.5


def _two_transmissions():
    """Two keyed transmissions with DEAD AIR between them, which is what a real capture is.

    The previous version of this fixture ran synth_nfm across the whole span and put
    silence in the modulating audio for the gap -- so the "gap" was a carrier at full
    strength, merely unmodulated. Nothing here was ever off the air. That is why the
    audio-RMS segmenter looked fine on synthetic input and then cut 57.6 of 60.1 minutes of
    a real hour into clips: the case it got wrong could not be expressed.
    """
    talk = lambda: baseband.synth_nfm(_speechlike(_TALK_S), 8000.0, IQ_RATE,
                                      deviation_hz=5000.0, noise_db=_NOISE_DB)
    return np.concatenate([
        baseband.synth_noise(0.5, IQ_RATE, _NOISE_DB, seed=1),
        talk(),
        baseband.synth_noise(1.5, IQ_RATE, _NOISE_DB, seed=2),
        talk(),
        baseband.synth_noise(0.5, IQ_RATE, _NOISE_DB, seed=3),
    ])


def test_only_the_transmissions_become_clips(tmp_path):
    """The end-to-end form of the bug this harness shipped with. On the real 2026-08-08
    capture the audio-RMS segmenter produced 42 clips covering 57.6 of 60.1 minutes --
    including single "clips" of 746, 451 and 378 seconds -- against an RF-measured truth of
    23 transmissions at 4.8% duty. Here: 2.4 s of carrier in a 4.9 s capture."""
    iq = _two_transmissions()
    written = iq_replay.replay_arm(iq, IQ_RATE, tmp_path, bandwidth_hz=16_000.0,
                                   offset_hz=0.0, squelch_over_floor_db=None, segments=None)
    assert written == 2, f"{written} clips for two transmissions separated by dead air"

    cuts = segments.read_segments(tmp_path / "segments.txt")
    covered = sum(b - a for a, b in cuts)
    # 2.4 s of carrier plus 300 ms of pad at each end of each segment = ~3.6 s of 4.9.
    assert covered < 0.8 * _CAPTURE_S, (
        f"segments cover {covered:.1f}s of a {_CAPTURE_S:.1f}s capture that is only "
        f"{2 * _TALK_S:.1f}s carrier -- dead air is being cut into clips")


def test_a_clip_is_written_in_the_format_bench_expects(tmp_path):
    iq_replay.write_clip(tmp_path / "0000_sent.wav", np.array([0.0, 0.5, -0.5]), 16000)
    with wave.open(str(tmp_path / "0000_sent.wav"), "rb") as w:
        assert (w.getnchannels(), w.getframerate(), w.getsampwidth()) == (1, 16000, 2)


def test_bench_discovers_what_the_harness_writes(tmp_path):
    """The integration that matters: the existing scorer must find these clips unchanged."""
    iq = _two_transmissions()
    segs = iq_replay.replay_arm(iq, IQ_RATE, tmp_path, bandwidth_hz=16_000.0,
                                offset_hz=0.0, squelch_over_floor_db=None, segments=None)
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
                             squelch_over_floor_db=None, segments=shared)
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
                             squelch_over_floor_db=None, segments=shared)
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
                                   offset_hz=0.0, squelch_over_floor_db=None,
                                   segments=segments_with_tail)
    assert written == len(segments_with_tail)

    last_id = f"{len(segments_with_tail) - 1:04d}"
    with wave.open(str(tmp_path / f"{last_id}_sent.wav"), "rb") as w:
        assert w.getnframes() > 0
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    assert np.all(pcm == 0)

    found = {cid for cid, _ in bench.discover_clips(tmp_path)}
    assert last_id in found
