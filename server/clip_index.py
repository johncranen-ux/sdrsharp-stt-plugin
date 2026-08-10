"""Join capture clip ids to wall-clock times.

Stored conversation turns carry HH:MM:SS; reference files are keyed by clip id. The capture
directory's index.jsonl holds both, so it is the authoritative join between a turn and the
reference text it should be scored against.

Two things about that file cost time if you meet them the hard way: it is written UTF-8 with
a BOM, so plain "utf-8" raises on line 1, and its "index" is a number while reference keys are
zero-padded strings.
"""

import datetime
import json
from pathlib import Path

_INDEX_NAME = "index.jsonl"


def load_clip_index(day_dir: str | Path) -> dict[str, datetime.datetime]:
    """{clip_id: naive local timestamp} for one capture day. Missing file -> {}."""
    path = Path(day_dir) / _INDEX_NAME
    if not path.is_file():
        return {}

    out: dict[str, datetime.datetime] = {}
    # utf-8-sig, not utf-8: the plugin writes a BOM.
    with open(path, "r", encoding="utf-8-sig") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
                clip = f"{int(row['index']):04d}"
                stamp = datetime.datetime.fromisoformat(row["timestamp"])
            except (ValueError, KeyError, TypeError):
                # One bad row must not cost the whole capture.
                continue
            # Turn times are naive local; drop the offset rather than mixing awareness.
            out[clip] = stamp.replace(tzinfo=None)
    return out


def clip_for_time(index: dict[str, datetime.datetime], when: datetime.datetime,
                  tolerance_s: float = 2.0) -> str | None:
    """The clip whose timestamp is nearest `when`, or None if none is within tolerance.

    None rather than nearest-regardless on purpose: a wrong join scores one turn's text
    against another turn's reference, which shows up as a quality change that never happened.
    """
    best, best_delta = None, None
    for clip, stamp in index.items():
        delta = abs((stamp - when).total_seconds())
        if best_delta is None or delta < best_delta:
            best, best_delta = clip, delta
    if best_delta is None or best_delta > tolerance_s:
        return None
    return best
