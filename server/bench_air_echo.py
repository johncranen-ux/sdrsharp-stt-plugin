"""Has the autopilot clue earned AIR_ECHO_ENABLED=on? Three conditions, all required.

Reads the airband archive the proxy has been filling in shadow mode. See the spec's
"Measuring the autopilot clue" section:
docs/superpowers/specs/2026-09-24-airband-conversations-design.md.

Usage:
    py bench_air_echo.py                       # everything archived so far
    py bench_air_echo.py --from 2026-09-25T00:00:00+02:00 --to 2026-09-29T00:00:00+02:00
"""
from __future__ import annotations

import argparse
import datetime
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVER_DIR))

import air_archive  # noqa: E402
import conversation_archive  # noqa: E402

MIN_OVERLAP = 50
MIN_AGREEMENT = 0.95
MIN_RECOVERY = 0.10


def _echo_hex(row: dict) -> str | None:
    echo = row.get("echo_clue") or {}
    return echo.get("hex") if echo.get("status") == "match" else None


def _is_flight_key(key: str) -> bool:
    return key not in ("unassigned", "review") and not key.startswith("heard:")


def _has_evidence(row: dict) -> bool:
    """False when stage 2 never ran or had no ADS-B to look at -- an outage, not a negative."""
    status = (row.get("echo_clue") or {}).get("status")
    return status is not None and status != "no_snapshots"


def gate(rows: list[dict]) -> dict:
    overlap = agree = 0
    moved = echo_right = cs_right = 0
    unassigned = recovers = 0
    excluded = 0
    for row in rows:
        if not _has_evidence(row):
            excluded += 1
            continue
        cs = row.get("callsign_clue") or {}
        echo_hex = _echo_hex(row)
        if cs.get("hex") and echo_hex:
            overlap += 1
            agree += cs["hex"] == echo_hex
        if row.get("moved") and _is_flight_key(row.get("effective_key") or ""):
            moved += 1
            echo_right += echo_hex == row["effective_key"]
            cs_right += cs.get("hex") == row["effective_key"]
        if not cs.get("candidate") and not cs.get("hex"):
            unassigned += 1
            recovers += echo_hex is not None

    agreement = agree / overlap if overlap else None
    recovery = recovers / unassigned if unassigned else None
    reasons = []
    if overlap < MIN_OVERLAP:
        reasons.append(f"only {overlap} transmissions have both clues; need {MIN_OVERLAP}")
    if agreement is None or agreement < MIN_AGREEMENT:
        reasons.append(f"agreement with the callsign is {agreement}; need >= {MIN_AGREEMENT}")
    if echo_right < cs_right:
        reasons.append(f"on hand-moved rows the echo was right {echo_right}x, "
                       f"the callsign {cs_right}x")
    if recovery is None or recovery < MIN_RECOVERY:
        reasons.append(f"recovers {recovery} of unassigned; need >= {MIN_RECOVERY}")
    return {"overlap": overlap, "agree": agree, "agreement": agreement, "moved": moved,
            "echo_right_on_moves": echo_right, "callsign_right_on_moves": cs_right,
            "unassigned": unassigned, "echo_recovers": recovers, "recovery": recovery,
            "excluded_no_evidence": excluded, "passes": not reasons, "reasons": reasons}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="")
    ap.add_argument("--from", dest="frm")
    ap.add_argument("--to")
    args = ap.parse_args(argv)
    db = conversation_archive.resolve_db_path(args.db, _SERVER_DIR)
    start = datetime.datetime.fromisoformat(args.frm).timestamp() if args.frm else 0.0
    end = datetime.datetime.fromisoformat(args.to).timestamp() if args.to else 4e9
    with air_archive.open_db(db) as conn:
        rows = air_archive.transmissions(conn, start, end)
    checked = [r for r in rows if r.get("echo_clue")]
    statuses: dict[str, int] = {}
    for r in checked:
        statuses[r["echo_clue"]["status"]] = statuses.get(r["echo_clue"]["status"], 0) + 1
    result = gate(checked)
    print(f"{len(rows)} transmissions, {len(checked)} with stage 2 done   db: {db}")
    print(f"echo status: {statuses}")
    print(f"excluded (no ADS-B evidence): {result['excluded_no_evidence']}")
    print(f"1. agreement  {result['agree']}/{result['overlap']} = {result['agreement']}")
    print(f"2. moves      echo {result['echo_right_on_moves']} vs callsign "
          f"{result['callsign_right_on_moves']} of {result['moved']}")
    print(f"3. recovery   {result['echo_recovers']}/{result['unassigned']} = {result['recovery']}")
    print("PASS -- AIR_ECHO_ENABLED may be switched on" if result["passes"]
          else "FAIL -- keep it off:\n  " + "\n  ".join(result["reasons"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
