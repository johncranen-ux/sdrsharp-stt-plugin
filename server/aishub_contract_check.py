"""Assert AISHub's response still has the shape stt_proxy/aishub.py assumes.

Run by hand, never in CI: it needs the credential and burns one of a rate-limited budget of
sixty requests an hour.

    python aishub_contract_check.py

This exists because of a failure this project has already had. The local-AIS work shipped with
a wrong assumption about transport shape and no test caught it, because -- as its design note
records -- "all fixtures were synthetic JSON in the assumed shape". Synthetic fixtures check
code against an assumption. Only a real call checks the assumption against the server.
"""

import os
import sys

from stt_proxy import aishub

REQUIRED = ["MMSI", "TIME", "LATITUDE", "LONGITUDE", "NAME", "CALLSIGN",
            "IMO", "TYPE", "A", "B", "C", "D", "DRAUGHT", "DEST",
            "COG", "SOG", "HEADING"]


def main() -> int:
    username = os.environ.get("AISHUB_USERNAME", "")
    if not username:
        print("AISHUB_USERNAME is not set", file=sys.stderr)
        return 2

    try:
        ships = aishub.parse_response(aishub._fetch(
            aishub.build_url(username, aishub.BBOX)))
    except aishub.AisHubError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print("If this says ERROR, wait 60s -- one request per minute.", file=sys.stderr)
        return 1

    if not ships:
        print("FAIL: no ships returned; cannot check the contract", file=sys.stderr)
        return 1

    print(f"{len(ships)} ships returned")

    sample = ships[0]
    missing = [f for f in REQUIRED if f not in sample]
    if missing:
        print(f"FAIL: fields absent from the response: {missing}", file=sys.stderr)
        return 1
    print(f"all {len(REQUIRED)} expected fields present")

    stamped = sum(1 for s in ships[:200] if aishub.parse_time(s.get("TIME", "")) is not None)
    if stamped < 190:
        print(f"FAIL: only {stamped}/200 TIME values parsed", file=sys.stderr)
        return 1
    print(f"TIME parsed on {stamped}/200 sampled ships")

    mapped = sum(1 for s in ships if aishub.map_ship(s) is not None)
    print(f"map_ship accepted {mapped}/{len(ships)}")

    named = [s for s in ships if (s.get("NAME") or "").strip()]
    seen: dict[str, int] = {}
    for s in named:
        key = s["NAME"].strip().upper()
        seen[key] = seen.get(key, 0) + 1
    dupes = {k: v for k, v in seen.items() if v > 1}
    print(f"{len(named)} named, {len(dupes)} duplicate-name groups covering "
          f"{sum(dupes.values())} vessels")

    print("\nCONTRACT OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
