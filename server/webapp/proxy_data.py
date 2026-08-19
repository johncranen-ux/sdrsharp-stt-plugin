# server/webapp/proxy_data.py
"""Recent copies of the proxy's collections, fetched server-side.

Two things shape this module. The browser must never be handed the raw collections -- the AIS
cache alone is 1.8 MB for 6046 vessels -- so everything is fetched here and paged before it
leaves. And the proxy is a separate process that can be restarted, stalled or gone, so a fetch
failure must degrade a screen rather than hang it: the last good copy is kept and served with
its age, and a screen showing stale data says so.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request
from collections.abc import Callable

from pydantic import BaseModel

CONVERSATIONS_TTL_SEC = 15
# The AIS cache changes only when the feed polls, which is every 900s by default. A minute of
# staleness costs nothing and a 1.8 MB fetch is not free.
VESSELS_TTL_SEC = 60
# 2.0 rather than something generous, and the same value health.py already uses. A
# successful fetch measures 0.03s, so this is ~60x headroom; and a fetch that is going to
# fail fails by TCP reset at 18.9s (Task 1), so every value well under that buys the same
# outcome -- the smaller one just shortens the window where a screen waits on a transfer
# that is already dead.
FETCH_TIMEOUT_SEC = 2.0


class Snapshot(BaseModel):
    fetched_at: float
    age_sec: float
    stale: bool
    error: str | None = None
    count: int = 0


def _fetch_json(url: str, timeout: float):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


class _Cell:
    __slots__ = ("records", "fetched_at", "error", "lock")

    def __init__(self):
        self.records: list[dict] | None = None
        self.fetched_at = 0.0
        self.error: str | None = None
        self.lock = threading.Lock()


class ProxyData:
    def __init__(self, load_values: Callable[[], dict],
                 fetch: Callable[[str, float], object] | None = None,
                 clock: Callable[[], float] = time.time):
        self._load_values = load_values
        self._fetch = fetch or _fetch_json
        self._clock = clock
        self._cells = {"conversations": _Cell(), "ais-cache": _Cell()}

    def conversations(self):
        return self._get("conversations", CONVERSATIONS_TTL_SEC)

    def vessels(self):
        return self._get("ais-cache", VESSELS_TTL_SEC)

    def _url(self, path: str) -> str:
        port = (self._load_values().get("PROXY_PORT") or "9000").strip()
        return f"http://127.0.0.1:{port}/api/{path}"

    def _get(self, path: str, ttl: float) -> tuple[list[dict], Snapshot]:
        cell = self._cells[path]
        now = self._clock()
        # One fetch at a time per collection: without this, three browser tabs refreshing
        # together would pull 1.8 MB three times over.
        with cell.lock:
            if cell.records is None or now - cell.fetched_at >= ttl:
                self._refresh(cell, path)
            now = self._clock()
            records = cell.records or []
            return records, Snapshot(
                fetched_at=cell.fetched_at,
                age_sec=max(0.0, now - cell.fetched_at) if cell.records is not None else 0.0,
                stale=cell.error is not None,
                error=cell.error,
                count=len(records))

    def _refresh(self, cell: _Cell, path: str) -> None:
        try:
            payload = self._fetch(self._url(path), FETCH_TIMEOUT_SEC)
        except Exception as exc:
            # Not "the proxy did not answer": Task 1 proved the proxy has already finished
            # writing and is healthy 18.9s before the client sees a reset -- this machine's
            # loopback TCP intermittently drops a run of segments on bulk transfers, and
            # Windows' retransmission timeout turns that into a reset at a constant 18.9s.
            # The fault is the transfer, not the proxy, so the wording says so; proxy_error
            # in health.py is the separate, correct place to report a proxy that is actually
            # unreachable.
            cell.error = f"the transfer failed before completing ({type(exc).__name__})"
            return
        if not isinstance(payload, list):
            cell.error = f"unexpected response shape: {type(payload).__name__}"
            return
        cell.records = payload
        cell.fetched_at = self._clock()
        cell.error = None
