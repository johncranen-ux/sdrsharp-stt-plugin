"""Two questions the dashboard must answer: do the configured paths exist here, and is the
proxy still hearing anything.

The proxy is asked over HTTP, server-side, rather than the browser asking it directly: that
sidesteps CORS, keeps the proxy the single source of live truth, and means the browser needs to
reach only one host.
"""
from __future__ import annotations

import json
import time
import urllib.request
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from webapp.registry import Paths
from webapp.settings_schema import SETTINGS, SettingType

TIMEOUT_SEC = 2.0


class PathCheck(BaseModel):
    key: str
    label: str
    value: str
    resolves: bool


class Feed(BaseModel):
    """One lamp on the feeds panel.

    `since_sec` is deliberately a number rather than a formatted string: the browser already
    owns the house format for an elapsed time, and two implementations of it would drift.
    `owner` names the process whose liveness governs the lamp -- a feed whose process the
    operator stopped on purpose is unlit, not failed, and only the browser knows which
    processes are running.
    """
    key: str
    label: str
    owner: str
    lamp: str            # green | amber | red | unlit
    verb: str            # what since_sec refers to: "checked", "polled"
    since_sec: float | None = None
    detail: str = ""
    note: str | None = None


class Health(BaseModel):
    paths: list[PathCheck]
    proxy: dict
    proxy_error: str | None = None
    feeds: list[Feed] = []


def path_checks(values: dict[str, str], paths: Paths) -> list[PathCheck]:
    """Every PATH setting that names something, plus the log directory.

    An empty PATH setting means "use the built-in default" and is a working configuration, so
    it is not listed -- a red mark against a setting nobody set would train the operator to
    ignore the strip.
    """
    checks = [
        PathCheck(key=spec.key, label=spec.group, value=(values.get(spec.key) or "").strip(),
                  resolves=Path((values.get(spec.key) or "").strip()).exists())
        for spec in SETTINGS
        if spec.type is SettingType.PATH and spec.key != "LOG_DIR"
        and (values.get(spec.key) or "").strip()
    ]
    # LOG_DIR is appended with its RESOLVED value: empty means server/logs, which is a real
    # directory the panel must still be able to write to.
    checks.append(PathCheck(key="LOG_DIR", label="Paths", value=str(paths.log_dir),
                            resolves=paths.log_dir.exists()))
    return checks


def _fetch_json(url: str, timeout: float) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def proxy_status(values: dict[str, str],
                 fetch: Callable[[str, float], dict] | None = None) -> tuple[dict, str | None]:
    """(payload, error). Loopback by address, because the proxy binds 0.0.0.0 and the panel is
    on the same machine by definition -- it is the thing that started it."""
    port = (values.get("PROXY_PORT") or "9000").strip()
    url = f"http://127.0.0.1:{port}/api/status"
    getter = fetch or _fetch_json
    try:
        payload = getter(url, TIMEOUT_SEC)
    except Exception as exc:
        # Named plainly, because the alternative -- an empty table -- reads as "nothing is
        # happening" when it means "the source is gone".
        return {}, f"the proxy is not answering on {url} ({type(exc).__name__})"
    return (payload if isinstance(payload, dict) else {}), None


# -- the feeds panel ---------------------------------------------------------
#
# A running process and an arriving feed are two different facts, and the cards only answer the
# first. aisstream demonstrated the gap for five days: the proxy was up, the dashboard was
# green, and no vessel data had arrived since the 5th. These read the second fact from each
# source's own record of it.

# The counter writes a heartbeat every 60s whatever the traffic, so a gap means the process is
# not running. Two and a half intervals allows for a slow write without letting a dead counter
# look alive for long.
STATION_STALE_SEC = 150
STATION_LOG = "ais-station-count.jsonl"


def _last_heartbeat(path: Path) -> dict | None:
    """The newest complete heartbeat record, or None.

    Read as a bounded tail from the end: this file grows all day and the panel re-reads it
    every few seconds. The last line is routinely a partial write -- another process is
    appending to it -- so lines are tried newest-first until one parses.
    """
    from webapp.logs import read_tail

    window = read_tail(path, limit=8192)
    if not window.text:
        return None
    for line in reversed(window.text.splitlines()):
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict) and record.get("type") == "heartbeat":
            return record
    return None


def _age(then: float | None, now: float) -> float | None:
    return None if then is None else max(0.0, now - then)


def ais_station_feed(paths: Paths, now: float | None = None) -> Feed:
    """Is the local AIS station still being heard?

    Measured against this machine's clock rather than the proxy's, because the counter writing
    the log and the panel reading it are the same host by definition -- the panel is what
    started it.
    """
    now = time.time() if now is None else now
    feed = Feed(key="ais-station", label="AIS station", owner="counter",
                lamp="unlit", verb="checked")

    record = _last_heartbeat(Path(paths.log_dir) / STATION_LOG)
    if record is None:
        feed.note = "no heartbeat log yet"
        return feed

    beat_at = _parse_iso(record.get("t"))
    poll_at = _parse_iso(record.get("last_poll_at"))
    feed.since_sec = _age(poll_at if poll_at is not None else beat_at, now)
    vessels = record.get("vessels_this_hour")
    feed.detail = "—" if vessels is None else f"{vessels} vessels this hour"

    beat_age = _age(beat_at, now)
    if beat_age is None or beat_age > STATION_STALE_SEC:
        feed.lamp = "red"
        feed.note = "the counter has stopped writing heartbeats"
    elif not record.get("connected"):
        # The counter is fine and AIS-catcher is not talking to it. Red here would send
        # someone to restart the wrong machine.
        feed.lamp = "amber"
        feed.note = "AIS-catcher is not connected"
    elif record.get("last_poll_ok") is False:
        feed.lamp = "amber"
        feed.note = "the station's web page did not answer the last poll"
    else:
        feed.lamp = "green"
    return feed


def _parse_iso(raw) -> float | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def aishub_feed(proxy: dict, proxy_error: str | None) -> Feed:
    """Is the vessel feed still delivering?

    Ages come from the proxy's own clock, which travels in its payload, because the timestamps
    being subtracted were taken by the proxy.
    """
    feed = Feed(key="aishub", label="AISHub", owner="proxy", lamp="unlit", verb="polled")

    if proxy_error:
        # Not red: we did not learn that the feed failed, only that we cannot see it. The
        # proxy's own lamp is already saying the thing that is actually wrong.
        feed.note = "the proxy is not answering"
        return feed

    source = (proxy.get("ais_source") or "").strip().lower()
    if source and source != "aishub":
        feed.note = f"AIS_SOURCE is {source}"
        return feed

    block = proxy.get("aishub")
    if not isinstance(block, dict):
        feed.note = "this proxy does not report feed state"
        return feed

    now = proxy.get("now") or time.time()
    last_ok = block.get("last_ok_at")
    feed.since_sec = _age(last_ok, now)
    count = block.get("last_count")
    feed.detail = "—" if count is None else f"{count} vessels in the box"

    poll_sec = block.get("poll_sec") or 900
    if block.get("consecutive_failures"):
        feed.lamp = "red"
        feed.note = block.get("last_error") or "the last poll failed"
    elif last_ok is None:
        feed.note = "no poll has completed yet"
    elif feed.since_sec > poll_sec * 2:
        # Nothing reported a failure, yet nothing has succeeded in two intervals either. That
        # is a wedged poll thread, which is exactly how aisstream died.
        feed.lamp = "amber"
        feed.note = "a poll is overdue"
    else:
        feed.lamp = "green"
    return feed


def feeds(paths: Paths, proxy: dict, proxy_error: str | None) -> list[Feed]:
    return [ais_station_feed(paths), aishub_feed(proxy, proxy_error)]


def health(values: dict[str, str], paths: Paths,
           fetch: Callable[[str, float], dict] | None = None) -> Health:
    payload, error = proxy_status(values, fetch=fetch)
    return Health(paths=path_checks(values, paths), proxy=payload, proxy_error=error,
                  feeds=feeds(paths, payload, error))
