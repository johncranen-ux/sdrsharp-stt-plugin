"""Fixed segmentation: one cut list, reused by every arm.

Two properties are load-bearing here.

**Segments come from RF channel power, not audio amplitude.** The first version of this
module gated on demodulated-audio RMS and cut 57.6 of 60.1 minutes of a real capture into
42 "clips", three of them over six minutes long, against an RF-measured truth of 23
transmissions at 4.8% duty. An FM discriminator emits full-scale hiss when there is no
carrier -- noise and speech leave it equally loud -- so no threshold in the audio domain
could have worked, and no synthetic fixture caught it because synth_nfm always had a
carrier. test_dead_air_is_not_a_segment is that bug.

**One cut list, shared.** bench_prompt_ab.py pairs arms on clip_id, so if each arm
segmented its own audio the same id would name different transmissions in different arms
and the comparison would be meaningless while still producing a number.
"""

import sys
from pathlib import Path

import numpy as np
import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from iq import segments  # noqa: E402

FRAME_RATE = 1000.0        # power frames per second, as Demodulator emits them
AUDIO_RATE = 16_000.0      # for the cut() tests, which still slice audio

_FLOOR_DB = -32.0          # a realistic in-channel noise floor for this capture setup
_SIGNAL_DB = 0.0           # a transmission, ~32 dB above it (measured on synthetic IQ)

_rng = np.random.default_rng(0x5E6)   # fixed: a flaky DSP test is worthless


def _dead(seconds, level=_FLOOR_DB):
    """Dead air: the noise floor, with the ~1 dB frame-to-frame wobble a real one has."""
    n = int(seconds * FRAME_RATE)
    return level + _rng.normal(scale=1.0, size=n)


def _carrier(seconds, level=_SIGNAL_DB):
    n = int(seconds * FRAME_RATE)
    return level + _rng.normal(scale=1.0, size=n)


def test_dead_air_is_not_a_segment():
    """THE regression test. An hour of a marine channel is ~95% no-carrier, and the shipped
    version called all of it speech."""
    assert segments.detect_segments(_dead(60.0), FRAME_RATE) == []


def test_two_transmissions_become_two_segments():
    track = np.concatenate([_dead(0.5), _carrier(1.0), _dead(1.5), _carrier(1.0), _dead(0.5)])
    assert len(segments.detect_segments(track, FRAME_RATE)) == 2


def test_a_segment_lands_on_the_transmission():
    """Counting segments is not enough -- they have to sit where the carrier actually was."""
    track = np.concatenate([_dead(1.0), _carrier(2.0), _dead(1.0)])
    (start, end), = segments.detect_segments(track, FRAME_RATE, pad_ms=0.0)
    assert start == pytest.approx(1.0, abs=0.05)
    assert end == pytest.approx(3.0, abs=0.05)


def test_the_final_unterminated_segment_excludes_trailing_silence():
    """A terminated segment already excludes its hangover. The recording can also end
    mid-hangover -- less trailing dead air than hang_ms -- and that must be excluded the
    same way, or the last clip of every recording carries a slab of dead air with pad_ms
    added on top."""
    track = np.concatenate([_dead(0.5), _carrier(1.0), _dead(1.5), _carrier(1.0), _dead(0.5)])
    found = segments.detect_segments(track, FRAME_RATE, pad_ms=0.0)
    assert len(found) == 2
    _, end = found[1]
    assert end == pytest.approx(4.0, abs=0.05), (
        f"end {end} should sit at the carrier's end (4.0s), not include the trailing 0.5s")


def test_a_short_blip_is_not_a_segment():
    """Squelch tails and key-up clicks are not transmissions."""
    track = np.concatenate([_dead(0.5), _carrier(0.05), _dead(1.5)])
    assert segments.detect_segments(track, FRAME_RATE) == []


def test_a_pause_inside_one_transmission_does_not_split_it():
    """Someone drawing breath mid-sentence is one transmission, not two clips -- and the
    hangover is what keeps it that way."""
    track = np.concatenate([_dead(0.5), _carrier(1.0), _dead(0.3), _carrier(1.0), _dead(0.5)])
    assert len(segments.detect_segments(track, FRAME_RATE)) == 1


def test_a_segment_is_padded_before_the_first_word():
    """The vessel name is in the opening syllables; a cut that starts exactly on the
    threshold crossing loses them."""
    track = np.concatenate([_dead(1.0), _carrier(1.0), _dead(1.0)])
    start, _ = segments.detect_segments(track, FRAME_RATE, pad_ms=300.0)[0]
    assert 0.6 <= start <= 1.0, f"start {start} should sit before the carrier at 1.0s"


def test_the_threshold_follows_the_noise_floor():
    """A fixed absolute threshold would have to be retuned for every capture, because the
    level depends on RF gain -- and silently mis-segment when someone forgot. The same
    traffic 20 dB hotter must give the same cuts."""
    track = np.concatenate([_dead(0.5), _carrier(1.0), _dead(1.5), _carrier(1.0), _dead(0.5)])
    quiet = segments.detect_segments(track, FRAME_RATE)
    loud = segments.detect_segments(track + 20.0, FRAME_RATE)
    assert quiet == loud


def test_an_explicit_threshold_overrides_the_measured_floor():
    """The auto floor assumes the capture is mostly dead air, which an hour of marine VHF
    is. A capture that is not needs the escape hatch."""
    track = np.concatenate([_dead(1.0), _carrier(1.0), _dead(1.0)])
    assert segments.detect_segments(track, FRAME_RATE, threshold_db=+50.0) == []


def test_the_noise_floor_ignores_the_transmissions():
    """It is the floor of the capture, so busy traffic must not drag it upward."""
    track = np.concatenate([_dead(3.0), _carrier(1.0)])
    assert segments.noise_floor_db(track) == pytest.approx(_FLOOR_DB, abs=1.5)


def test_an_empty_track_is_not_a_crash():
    assert segments.detect_segments(np.zeros(0), FRAME_RATE) == []


# The cut list itself: written, re-read, and applied to each arm's audio.


def _burst(seconds, amp=0.5):
    t = np.arange(int(seconds * AUDIO_RATE)) / AUDIO_RATE
    return np.sin(2 * np.pi * 800 * t) * amp


def _silence(seconds):
    return np.zeros(int(seconds * AUDIO_RATE))


def test_segments_survive_a_file_round_trip(tmp_path):
    original = [(1.25, 3.5), (10.0, 12.75)]
    p = tmp_path / "segments.txt"
    segments.write_segments(p, original)
    assert segments.read_segments(p) == original


def test_cutting_is_identical_for_two_different_arms():
    """The property the whole design rests on. Real arms differ in both content AND length
    (each is independently resampled from the same recording, so lengths drift by a sample
    or two), but a fixed segment list must select the same transmission out of each --
    proven by matching sliced *content* against each arm's own audio, not merely by matching
    clip lengths. (A rescale-only arm_b = arm_a * k, as this test used before, keeps the
    same length no matter what cut() does, so it can't catch cut() slicing wrong offsets.)
    """
    fixed = [(0.5, 1.5), (2.0, 3.0)]
    arm_a = np.concatenate([_silence(0.5), _burst(1.0), _silence(0.5), _burst(1.0), _silence(1.0)])
    arm_b = arm_a[:-3]            # a different arm: independently resampled, a few samples shorter

    cuts_a = segments.cut(arm_a, AUDIO_RATE, fixed)
    cuts_b = segments.cut(arm_b, AUDIO_RATE, fixed)
    assert len(cuts_a) == len(cuts_b) == 2

    for (start_s, end_s), clip_a, clip_b in zip(fixed, cuts_a, cuts_b):
        a, b = int(start_s * AUDIO_RATE), int(end_s * AUDIO_RATE)
        np.testing.assert_array_equal(clip_a, arm_a[a:b])
        np.testing.assert_array_equal(clip_b, arm_b[a:b])


def test_a_segment_past_the_end_of_a_short_arm_still_gets_an_index():
    """clip_id is assigned by enumeration order in the downstream tool (bench_prompt_ab.py).
    If a segment that runs past a shorter arm's end were dropped instead of yielding an
    (empty) clip, every later index in that arm would shift by one and silently pair the
    wrong transmissions across arms under the same clip_id -- for the rest of the capture."""
    short_arm = _burst(1.0)  # 1.0s of audio
    fixed = [(0.1, 0.2), (5.0, 6.0), (0.3, 0.4)]  # middle segment starts past the arm's end
    cuts = segments.cut(short_arm, AUDIO_RATE, fixed)
    assert len(cuts) == 3
    assert len(cuts[0]) > 0
    assert len(cuts[1]) == 0
    assert len(cuts[2]) > 0


def test_a_segment_past_the_end_is_clipped_not_crashed():
    audio = _burst(1.0)
    cuts = segments.cut(audio, AUDIO_RATE, [(0.5, 99.0)])
    assert len(cuts) == 1 and len(cuts[0]) > 0


# A guard against the failure mode that cost a session: a cut list that is confidently wrong.
#
# Measured on the real hour capture, sweeping the threshold margin: 8 dB gives 40 segments,
# 10 and 12 dB give 39, and 14 dB gives ZERO -- every candidate run falls under min_ms once
# the threshold passes the weaker transmissions, so the harness would produce an empty arm
# and report it as a clean run. In the other direction, 4 dB lets noise in (164 segments,
# 11% duty against a true 4.8%). Neither extreme raises an error on its own.


def test_a_plausible_cut_list_gets_no_warning():
    """The real capture's own numbers: 39 segments over 5.5% of an hour."""
    cuts = [(60.0 * i, 60.0 * i + 5.0) for i in range(39)]
    assert segments.duty_warning(cuts, 3606.0) is None


def test_an_empty_cut_list_is_reported():
    """What a too-high threshold produces. An arm with no clips scores as a clean run."""
    assert segments.duty_warning([], 3606.0) is not None


def test_a_cut_list_covering_most_of_the_capture_is_reported():
    """What the audio-RMS segmenter produced: 57.6 of 60.1 minutes. A voice channel that is
    busy 96% of the hour is a broken measurement, not heavy traffic."""
    assert segments.duty_warning([(0.0, 3400.0)], 3606.0) is not None


def test_the_warning_says_what_was_measured():
    """A warning that does not carry the number is one more thing to go and check."""
    message = segments.duty_warning([(0.0, 3400.0)], 3606.0)
    assert "94" in message and "%" in message
