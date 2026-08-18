"""Two questions the dashboard must answer: do the configured paths exist here, and is the
proxy still hearing anything.

The proxy is asked over HTTP, server-side, rather than the browser asking it directly: that
sidesteps CORS, keeps the proxy the single source of live truth, and means the browser needs to
reach only one host.
"""
from __future__ import annotations

import json
import urllib.request
from collections.abc import Callable
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


class Health(BaseModel):
    paths: list[PathCheck]
    proxy: dict
    proxy_error: str | None = None


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


def health(values: dict[str, str], paths: Paths,
           fetch: Callable[[str, float], dict] | None = None) -> Health:
    payload, error = proxy_status(values, fetch=fetch)
    return Health(paths=path_checks(values, paths), proxy=payload, proxy_error=error)
