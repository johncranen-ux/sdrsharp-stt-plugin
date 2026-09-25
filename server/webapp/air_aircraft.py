"""The aircraft in ADS-B range at one moment, read from the proxy's snapshot log.

What the Airband tab's "New flight..." list offers: a transmission the app could not attribute
is moved by hand to an aircraft that really was in range, so the new strip gets a real callsign,
type and registration and stays usable as a label for the autopilot-echo measurement.

The proxy writes one line per poll to <logs>/adsb-YYYY-MM-DD.jsonl (stt_proxy/adsb.py, local
date). The panel may not import the proxy package, so this is its own small reader. 2026-09-10
was captured by hand as adsb-snapshots-2026-09-10.jsonl, which is read too.
"""
from __future__ import annotations

import datetime
import json
import math
from pathlib import Path

MAX_GAP_S = 30        # the proxy polls every 15 s; further than this is not "that moment"

# The poll centre, so "how far out" is answerable. Mirrors adsb.py's defaults.
_ORIGIN_LAT, _ORIGIN_LON = 52.15, 4.3
# What a strip shows, the same fields flight_attribution stores for an attributed row.
_STATE_FIELDS = ("flight", "t", "r", "alt_baro", "nav_altitude_mcp", "nav_heading", "track")
_T_PREFIX = '{"t": "'


def _day_files(log_dir: Path, day: str) -> list[Path]:
    return [p for p in (Path(log_dir) / f"adsb-{day}.jsonl",
                        Path(log_dir) / f"adsb-snapshots-{day}.jsonl") if p.exists()]


def _epoch_of_line(line: str) -> float | None:
    """The snapshot time without parsing the whole line -- a day file is ~20 MB."""
    if not line.startswith(_T_PREFIX):
        return None
    end = line.find('"', len(_T_PREFIX))
    try:
        return datetime.datetime.fromisoformat(line[len(_T_PREFIX):end]).timestamp()
    except ValueError:
        return None


def nearest_snapshot(log_dir: Path, epoch: float, max_gap_s: float = MAX_GAP_S) -> dict | None:
    """The snapshot closest to `epoch`, or None when none is within `max_gap_s`."""
    days = {datetime.datetime.fromtimestamp(epoch + d).astimezone().date().isoformat()
            for d in (-max_gap_s, max_gap_s)}
    near: list[tuple[float, str]] = []
    for day in sorted(days):
        for path in _day_files(log_dir, day):
            with path.open(encoding="utf-8") as handle:
                for line in handle:
                    when = _epoch_of_line(line)
                    if when is not None and abs(when - epoch) <= max_gap_s:
                        near.append((abs(when - epoch), line))
    for _gap, line in sorted(near, key=lambda pair: pair[0]):
        try:
            return json.loads(line)
        except ValueError:      # the proxy was mid-write on the last line: try the next nearest
            continue
    return None


def _km(lat, lon) -> float | None:
    if lat is None or lon is None:
        return None
    dlat = math.radians(lat - _ORIGIN_LAT)
    dlon = math.radians(lon - _ORIGIN_LON)
    a = (math.sin(dlat / 2) ** 2 + math.cos(math.radians(_ORIGIN_LAT))
         * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2)
    return round(6371.0 * 2 * math.asin(math.sqrt(a)), 1)


def _is_named_icao(a: dict) -> bool:
    """A callsign to pick by, and a hex a strip key can be (TIS-B '~' ids cannot)."""
    hex_ = str(a.get("hex") or "")
    return bool((a.get("flight") or "").strip()) and len(hex_) == 6 and all(
        c in "0123456789abcdefABCDEF" for c in hex_)


def listing(snapshot: dict) -> list[dict]:
    """Every named aircraft in the snapshot, nearest first."""
    out = [{"hex": a["hex"].lower(), "flight": a["flight"].strip(), "type": a.get("t"),
            "reg": a.get("r"), "alt": a.get("alt_baro"), "km": _km(a.get("lat"), a.get("lon"))}
           for a in snapshot.get("aircraft") or [] if _is_named_icao(a)]
    return sorted(out, key=lambda a: (a["km"] is None, a["km"] or 0.0, a["flight"]))


def state_of(snapshot: dict, hex_: str) -> dict | None:
    """What a strip shows for that aircraft, or None when it is not in the snapshot.

    airline is the ICAO code ('KLM', 'DAL'): the spoken name lives in the proxy's telephony
    table, which the panel cannot import.
    """
    want = (hex_ or "").lower()
    for a in snapshot.get("aircraft") or []:
        if _is_named_icao(a) and a["hex"].lower() == want:
            state = {k: a.get(k) for k in _STATE_FIELDS}
            state["flight"] = a["flight"].strip()
            state["airline"] = state["flight"][:3].upper() or None
            return state
    return None
