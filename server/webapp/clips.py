"""Finding the audio the plugin captured for a conversation turn.

There is no shared identifier to join on. A stored turn carries HH:MM:SS; the plugin's capture
carries its own `index` per clip and a full timestamp, in `captures/<YYYY-MM-DD>/index.jsonl`.
The proxy's `chunk_ids` are a third numbering and match neither. So the join is by wall-clock
time within a tolerance, which `clip_index.clip_for_time` already does for the bench tooling --
this module puts the same join behind the control panel.

The `_sent` file, not `_raw`: it is the audio that was actually handed to the model, after VAD
trimming and normalisation, so it is what a transcription has to be judged against. `_raw` is
truer to what was said but proves less about why the text came out as it did.

Everything here treats the captures directory as hostile input, because the endpoint built on
it reads files off disk on behalf of a browser.
"""
from __future__ import annotations

import datetime
import re
from pathlib import Path

import clip_index

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLIP = re.compile(r"^\d{4}$")
_CLIP_SUFFIX = "_sent.wav"

# The turn's time and the capture's timestamp are taken at different points in the pipeline,
# so they are close rather than equal. Same value the bench tooling joins on.
TOLERANCE_S = 2.0


def _parse_start(start) -> datetime.datetime | None:
    """A conversation's start instant.

    The live store writes "2026-08-14 23:52:58", but ISO-8601 with an offset also occurs, so
    both are accepted rather than the feature silently switching itself off if the stored
    spelling changes. Awareness is dropped: turn times are naive local everywhere else in this
    project, and clip_index drops it too, so mixing the two here would compare across zones.
    """
    text = str(start or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _parse_turn_time(turn_time) -> datetime.time | None:
    """A turn's time of day, from "HH:MM:SS" or from a full timestamp."""
    text = str(turn_time or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text, "%H:%M:%S").time()
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text).replace(tzinfo=None).time()
    except (ValueError, TypeError):
        return None


def turn_day(start: str | None, turn_time: str | None) -> str | None:
    """The dated capture directory a turn belongs to.

    Usually the conversation's own day. A conversation that begins at 23:52 can run past
    midnight, though, and the plugin will have written those later turns into the NEXT dated
    directory -- so a turn whose time of day precedes the start's rolls over. Conversations run
    for minutes, so "earlier in the day than the start" cannot mean anything else.
    """
    began = _parse_start(start)
    spoken = _parse_turn_time(turn_time)
    if began is None or spoken is None:
        return None
    day = began.date()
    if spoken < began.time():
        day += datetime.timedelta(days=1)
    return day.isoformat()


def _index_for(captures_root, day: str) -> dict:
    if captures_root is None or not _DAY.match(day or ""):
        return {}
    return clip_index.load_clip_index(Path(captures_root) / day)


def clip_path(captures_root, day: str, clip: str) -> Path | None:
    """The WAV for one clip, or None if anything about the request is not exactly right.

    Validated twice over: the day and clip must match their formats, and the resolved path must
    still sit inside the captures root. The format checks alone would be enough today, but this
    is the one place the panel turns a URL into a file read.
    """
    if captures_root is None or not _DAY.match(day or "") or not _CLIP.match(clip or ""):
        return None
    root = Path(captures_root).resolve()
    candidate = (root / day / f"{clip}{_CLIP_SUFFIX}").resolve()
    if not candidate.is_relative_to(root):
        return None
    return candidate if candidate.is_file() else None


def resolve(captures_root, day: str, turn_time: str, index: dict | None = None) -> str | None:
    """The clip id recorded at `turn_time` on `day`, or None.

    None covers every way this legitimately fails -- capture was off, the day was pruned, the
    turn fell between clips -- and the caller shows no play button rather than a broken one.
    """
    spoken = _parse_turn_time(turn_time)
    if spoken is None or not _DAY.match(day or ""):
        return None
    when = datetime.datetime.combine(datetime.date.fromisoformat(day), spoken)

    if index is None:
        index = _index_for(captures_root, day)
    clip = clip_index.clip_for_time(index, when, TOLERANCE_S)
    # The index outlives the audio: capture directories get pruned by hand, and a play button
    # for a file that is no longer there is worse than no button.
    if clip and clip_path(captures_root, day, clip) is None:
        return None
    return clip


def annotate(turns: list[dict], start: str | None, captures_root) -> list[dict]:
    """Each turn with `clip` and `clip_day` added; both None when there is no audio.

    Indexes are read once per day rather than once per turn -- a conversation is a few dozen
    turns and they nearly always share one capture directory.
    """
    cache: dict[str, dict] = {}
    out = []
    for turn in turns or []:
        day = turn_day(start, turn.get("time"))
        clip = None
        if day is not None and captures_root is not None:
            if day not in cache:
                cache[day] = _index_for(captures_root, day)
            clip = resolve(captures_root, day, turn.get("time"), cache[day])
        out.append({**turn, "clip": clip, "clip_day": day if clip else None})
    return out
