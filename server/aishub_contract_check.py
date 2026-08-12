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

# Fields AISHub documents as JSON numbers and that reach a numeric format string unconverted.
# map_ship coerces LATITUDE/LONGITUDE/TYPE defensively but passes these through raw, and
# conversations._format_particulars then applies `:.1f` (DRAUGHT, SOG) or `int()` (COG, and
# A-D via _dimension) to them. A string here would not be a wrong number on one row -- it
# raises inside the page renderer and takes the whole /conversations page down with a 500.
# Presence alone never caught that, which is the gap this list closes: the only thing in this
# project that touches the real service should check the TYPE of what it gets, not just that
# it arrived.
NUMERIC = ["LATITUDE", "LONGITUDE", "TYPE", "IMO", "A", "B", "C", "D",
           "DRAUGHT", "COG", "SOG", "HEADING"]


def _is_number(value) -> bool:
    """True for a JSON number, or for None -- absent is handled everywhere, a string is not.

    bool is excluded deliberately: `isinstance(True, int)` is True in Python, and a boolean
    reaching `:.1f` is a contract break wearing a number's clothes.
    """
    return value is None or (isinstance(value, (int, float)) and not isinstance(value, bool))


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

    # Every ship, not a sample: one string in nine thousand rows is enough to 500 the page,
    # and it costs nothing here -- the request is already paid for.
    offenders: dict[str, tuple] = {}
    for ship in ships:
        for field in NUMERIC:
            if field not in offenders and not _is_number(ship.get(field)):
                offenders[field] = (ship.get("MMSI"), ship.get(field))
    if offenders:
        for field, (mmsi, value) in sorted(offenders.items()):
            print(f"FAIL: {field} is {type(value).__name__} {value!r} on MMSI {mmsi}, "
                  f"expected a JSON number", file=sys.stderr)
        return 1
    print(f"all {len(NUMERIC)} numeric fields are numbers on all {len(ships)} ships")

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
