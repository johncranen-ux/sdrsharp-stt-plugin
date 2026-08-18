"""What the control panel manages, and how each command line is built from config.

A registry rather than a handler per process: AIS-catcher becomes one more entry when the
miniPC arrives, and the counter is expected to be retired by a flag rather than by deletion.

Nothing here shells out to start-all.bat. That was proven impossible on 2026-08-18 -- `start`
needs an interactive window station, which a detached or service parent does not have -- and it
is also the wrong shape: the panel owns the environment, so it must own the command line.
"""
from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from webapp.env_builder import build_env


@dataclass(frozen=True)
class Paths:
    server_dir: Path
    log_dir: Path


def resolve_paths(values: dict[str, str], server_dir: Path) -> Paths:
    configured = (values.get("LOG_DIR") or "").strip()
    return Paths(server_dir=Path(server_dir),
                 log_dir=Path(configured) if configured else Path(server_dir) / "logs")


@dataclass(frozen=True)
class ProcessSpec:
    name: str
    label: str
    log_prefix: str
    image_name: str
    enabled_key: str
    port_key: str | None
    build_argv: Callable[[dict[str, str], Paths], list[str]]
    description: str


def _proxy_argv(values: dict[str, str], paths: Paths) -> list[str]:
    return [sys.executable, str(paths.server_dir / "whisper-proxy.py")]


def _counter_argv(values: dict[str, str], paths: Paths) -> list[str]:
    host = (values.get("AIS_STATION_HOST") or "").strip()
    http_port = (values.get("AIS_STATION_HTTP_PORT") or "").strip()
    return [
        sys.executable, str(paths.server_dir / "ais_station_count.py"),
        "--station", f"{host}:{http_port}",
        "--port", (values.get("AIS_STATION_NMEA_PORT") or "").strip(),
        # Its own log file, never interleaved with the proxy's: the proxy log is read to answer
        # questions about transcription, and per-hour MMSI counts scrolling through it would
        # cost more than the separate process does.
        "--log", str(paths.log_dir / "ais-station-count.jsonl"),
    ]


PROCESSES: tuple[ProcessSpec, ...] = (
    ProcessSpec(
        name="proxy", label="Whisper proxy", log_prefix="proxy",
        image_name=Path(sys.executable).name, enabled_key="PROXY_ENABLED",
        port_key="PROXY_PORT", build_argv=_proxy_argv,
        description="Transcribes what the plugin sends, resolves conversations, and serves "
                    "the identified-vessels page.",
    ),
    ProcessSpec(
        name="counter", label="AIS station counter", log_prefix="counter",
        image_name=Path(sys.executable).name, enabled_key="COUNTER_ENABLED",
        # The counter LISTENS on this port -- AIS-catcher is pointed at it with `-P <ip> 10111`
        # and pushes. It was first written here as "connects out and listens on nothing", which
        # was wrong and had a consequence: with no port declared, nothing was cleared before a
        # start, and ais_station_count.py sets SO_REUSEADDR. On 2026-08-18 a second counter
        # bound alongside a hand-started one, took the station's connection over, and left the
        # original alive but starved -- the exact zombie-listener failure this project has had
        # before, reproduced by the one entry that opted out of the fix.
        port_key="AIS_STATION_NMEA_PORT", build_argv=_counter_argv,
        description="Counts distinct vessels per hour. It listens for AIS-catcher to connect "
                    "and push NMEA, and polls the station's web UI for a range map.",
    ),
)

BY_NAME: dict[str, ProcessSpec] = {spec.name: spec for spec in PROCESSES}


def argv_for(spec: ProcessSpec, values: dict[str, str], paths: Paths) -> list[str]:
    return spec.build_argv(values, paths)


def env_for(spec: ProcessSpec, values: dict[str, str]) -> dict[str, str]:
    """The child's environment. Unbuffered, because its stdout is a log file being tailed:
    a buffered child would show nothing for minutes and look hung."""
    env = build_env(values)
    env["PYTHONUNBUFFERED"] = "1"
    return env


def port_for(spec: ProcessSpec, values: dict[str, str]) -> int | None:
    if spec.port_key is None:
        return None
    try:
        return int((values.get(spec.port_key) or "").strip())
    except ValueError:
        return None


def is_enabled(spec: ProcessSpec, values: dict[str, str]) -> bool:
    return (values.get(spec.enabled_key) or "on").strip().lower() != "off"
