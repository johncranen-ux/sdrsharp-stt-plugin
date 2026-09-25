"""THROWAWAY: search the side-car aircraft snapshots by callsign pattern.

For hand-labelling: you hear a garbled callsign at a known time and want to know what was
actually in range. Often the digits come through clearly while the airline word does not, so
a bare pattern with no wildcard is searched as a substring -- `adsb_find.py 162` finds
DAL162, KLM162 and BAW1620 alike.

Usage:
    py adsb_find.py 162                 # anything containing 162, with its time window
    py adsb_find.py "DAL1*"             # explicit glob
    py adsb_find.py "DAL*" --at 11:20   # what was in range at that moment, with altitude
    py adsb_find.py "*" --at 2026-09-10T11:20:03.5361684+02:00     # paste from index.jsonl
"""

import argparse
import datetime
import fnmatch
import json
import math
import re
from collections import defaultdict
from pathlib import Path

LOGS = Path(r"D:\Claudecode\projects\SDRSharp-Plugin\server\logs")
# The poll centre, so "how far out" is answerable. Mirrors adsb.py's defaults.
ORIGIN_LAT, ORIGIN_LON = 52.15, 4.3
_DAY_NAME = re.compile(r"adsb-(?:snapshots-)?(\d{4}-\d{2}-\d{2})\.jsonl")


def _day_path(day: str) -> Path | None:
    """The proxy's own log (adsb-<day>.jsonl) first, then the hand-made 09-10 side-car name."""
    for name in (f"adsb-{day}.jsonl", f"adsb-snapshots-{day}.jsonl"):
        if (LOGS / name).exists():
            return LOGS / name
    return None


def _resolve_day(day: str | None) -> str:
    """An explicit --day must exist; otherwise today, else the newest capture on disk."""
    if day:
        if not _day_path(day):
            raise SystemExit(f"no snapshots for {day} in {LOGS}")
        return day
    today = datetime.date.today().isoformat()
    if _day_path(today):
        return today
    days = sorted({m.group(1) for p in LOGS.glob("adsb-*.jsonl")
                   if (m := _DAY_NAME.fullmatch(p.name))})
    if not days:
        raise SystemExit(f"no snapshot files at all in {LOGS}")
    print(f"no snapshots for {today}; using {days[-1]}")
    return days[-1]


def _load(day: str) -> list[dict]:
    path = _day_path(day)
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def _as_glob(pattern: str) -> str:
    """A pattern with no wildcard is a substring search -- the common labelling case."""
    if any(ch in pattern for ch in "*?["):
        return pattern.upper()
    return f"*{pattern.upper()}*"


def _km(lat, lon) -> float | None:
    if lat is None or lon is None:
        return None
    dlat = math.radians(lat - ORIGIN_LAT)
    dlon = math.radians(lon - ORIGIN_LON)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(ORIGIN_LAT)) * math.cos(math.radians(lat)) * math.sin(dlon / 2) ** 2)
    return 6371.0 * 2 * math.asin(math.sqrt(a))


def _hhmmss(row: dict) -> str:
    return row["t"][11:19]


def _nearest(rows: list[dict], want: str) -> dict:
    """The snapshot closest to `want`, which may be HH:MM, HH:MM:SS, or a full ISO stamp."""
    stamp = want.split("T")[-1][:8] if "T" in want else want
    parts = stamp.split(":")
    if len(parts) == 2:
        stamp = f"{stamp}:00"
    target = datetime.time.fromisoformat(stamp)

    def gap(row: dict) -> float:
        t = datetime.time.fromisoformat(_hhmmss(row))
        return abs((t.hour * 3600 + t.minute * 60 + t.second)
                   - (target.hour * 3600 + target.minute * 60 + target.second))

    return min(rows, key=gap)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern")
    ap.add_argument("--at", help="HH:MM, HH:MM:SS, or a full ISO timestamp")
    ap.add_argument("--day", help="default: today, else the newest capture on disk")
    args = ap.parse_args()

    rows = _load(_resolve_day(args.day))
    glob = _as_glob(args.pattern)

    if args.at:
        row = _nearest(rows, args.at)
        hits = [a for a in row["aircraft"]
                if a["flight"] and fnmatch.fnmatch(a["flight"].upper(), glob)]
        print(f"snapshot {_hhmmss(row)}  ({row.get('n', len(row['aircraft']))} aircraft in range)  pattern {glob}")
        if not hits:
            print("  no match")
            return
        print(f"  {'callsign':10} {'type':6} {'alt':>7}  {'km':>6}  reg")
        for a in sorted(hits, key=lambda a: a["flight"]):
            dist = _km(a.get("lat"), a.get("lon"))
            alt = a.get("alt_baro")
            print(f"  {a['flight']:10} {str(a.get('t') or '-'):6} {str(alt):>7}  "
                  f"{(f'{dist:.1f}' if dist is not None else '-'):>6}  {a.get('r') or '-'}")
        return

    seen: dict[str, list[str]] = defaultdict(list)
    for row in rows:
        for a in row["aircraft"]:
            if a["flight"] and fnmatch.fnmatch(a["flight"].upper(), glob):
                seen[a["flight"]].append(_hhmmss(row))
    print(f"{len(rows)} snapshots {_hhmmss(rows[0])} -> {_hhmmss(rows[-1])}   pattern {glob}")
    if not seen:
        print("  no match")
        return
    for callsign, times in sorted(seen.items()):
        print(f"  {callsign:10} {times[0]}-{times[-1]}  ({len(times)} snaps)")


if __name__ == "__main__":
    main()
