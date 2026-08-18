"""Bounded reads of a growing log file.

The UI polls with the offset it last received, so a refresh costs only what was appended.
A file that shrank was rotated or truncated; the reader restarts at zero and says so, rather
than seeking past the end and returning nothing forever.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

MAX_LIMIT = 262_144
DEFAULT_LIMIT = 65_536


class TailWindow(BaseModel):
    path: str
    offset: int
    next_offset: int
    size: int
    text: str
    restarted: bool = False


def latest_log(log_dir: Path, prefix: str) -> Path | None:
    """The newest dated log for a process. Names sort chronologically by construction."""
    try:
        found = sorted(Path(log_dir).glob(f"{prefix}-*.log"))
    except OSError:
        return None
    return found[-1] if found else None


def read_tail(path: Path, offset: int | None = None, limit: int = DEFAULT_LIMIT) -> TailWindow:
    path = Path(path)
    limit = max(1, min(int(limit), MAX_LIMIT))
    try:
        size = path.stat().st_size
    except OSError:
        return TailWindow(path=str(path), offset=0, next_offset=0, size=0, text="")

    restarted = False
    if offset is None:
        start = max(0, size - limit)
    elif offset > size:
        start, restarted = 0, True
    else:
        start = max(0, int(offset))

    try:
        with path.open("rb") as handle:
            handle.seek(start)
            chunk = handle.read(limit)
    except OSError:
        return TailWindow(path=str(path), offset=start, next_offset=start, size=size, text="")

    return TailWindow(
        path=str(path), offset=start, next_offset=start + len(chunk), size=size,
        # errors="replace" because AIS vessel names have arrived mis-encoded before, and a log
        # viewer that dies on one bad byte is worse than one that shows a box.
        text=chunk.decode("utf-8", errors="replace"), restarted=restarted)
