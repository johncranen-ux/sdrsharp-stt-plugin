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
