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


def test_the_final_unterminated_segment_excludes_trailing_silence():
    """A terminated segment already excludes its hangover silence (end = i - silent + 1).
    But the recording can end mid-hangover -- fewer than hang_frames of trailing silence --
    and that partial silence must be excluded the same way, or the last clip of every
    recording carries a slab of dead air (plus pad_ms on top of it).

    This is the second burst in test_two_bursts_become_two_segments: it ends with 0.5s of
    silence against a 600ms hang_ms, so the recording ends before the hangover completes.
    That test only checked the segment count, so the bug shipped invisibly.
    """
    audio = np.concatenate([_silence(0.5), _burst(1.0), _silence(1.5), _burst(1.0), _silence(0.5)])
    found = segments.detect_segments(audio, RATE, pad_ms=0.0)
    assert len(found) == 2
    _, end = found[1]
    assert end == pytest.approx(4.0, abs=0.02), (
        f"end {end} should sit at the burst's end (4.0s), not include the trailing 0.5s silence"
    )


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
    """The property the whole design rests on. Real arms differ in both content AND length
    (each is independently resampled from the same recording, so lengths drift by a sample
    or two), but a fixed segment list must select the same transmission out of each -- proven
    by matching sliced *content* against each arm's own audio, not merely by matching clip
    lengths. (A rescale-only arm_b = arm_a * k, as this test used before, keeps the same
    length no matter what cut() does, so it can't catch cut() slicing the wrong offsets.)
    """
    fixed = [(0.5, 1.5), (2.0, 3.0)]
    arm_a = np.concatenate([_silence(0.5), _burst(1.0), _silence(0.5), _burst(1.0), _silence(1.0)])
    arm_b = arm_a[:-3]            # a different arm: independently resampled, a few samples shorter

    cuts_a = segments.cut(arm_a, RATE, fixed)
    cuts_b = segments.cut(arm_b, RATE, fixed)
    assert len(cuts_a) == len(cuts_b) == 2

    for (start_s, end_s), clip_a, clip_b in zip(fixed, cuts_a, cuts_b):
        a, b = int(start_s * RATE), int(end_s * RATE)
        np.testing.assert_array_equal(clip_a, arm_a[a:b])
        np.testing.assert_array_equal(clip_b, arm_b[a:b])


def test_a_segment_past_the_end_of_a_short_arm_still_gets_an_index():
    """clip_id is assigned by enumeration order in the downstream tool (bench_prompt_ab.py).
    If a segment that runs past a shorter arm's end were dropped instead of yielding an
    (empty) clip, every later index in that arm would shift by one and silently pair the
    wrong transmissions across arms under the same clip_id -- for the rest of the capture."""
    short_arm = _burst(1.0)  # 1.0s of audio
    fixed = [(0.1, 0.2), (5.0, 6.0), (0.3, 0.4)]  # middle segment starts past the arm's end
    cuts = segments.cut(short_arm, RATE, fixed)
    assert len(cuts) == 3
    assert len(cuts[0]) > 0
    assert len(cuts[1]) == 0
    assert len(cuts[2]) > 0


def test_a_segment_past_the_end_is_clipped_not_crashed():
    audio = _burst(1.0)
    cuts = segments.cut(audio, RATE, [(0.5, 99.0)])
    assert len(cuts) == 1 and len(cuts[0]) > 0
