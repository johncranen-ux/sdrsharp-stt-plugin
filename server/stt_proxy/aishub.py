"""AISHub as the vessel source.

Polls AISHub's REST webservice for every vessel in a bounding box and writes each one into
the shared cache through `ais.record()`. Nothing here touches the cache directly -- the merge
lives in one place so two providers cannot get it wrong two different ways.

Replaces aisstream, which has delivered nothing since 2026-08-05. The aisstream path is still
live and still tested; `AIS_SOURCE` chooses between them.

The failure mode that shapes this module: AISHub answers a rate-limit violation with HTTP 200
and a valid-JSON body carrying `ERROR: true` and no ships. Read naively that is
indistinguishable from "the box is empty", which would mark every cached vessel out of scope
and silently destroy identification. Every non-observation raises AisHubError instead, and the
caller leaves the cache untouched.
"""

import datetime
import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from stt_proxy import ais
from stt_proxy.ais import _clean_destination

API_URL = "https://data.aishub.net/ws.php"

# AISHub's documented limit, verbatim: "Don't access the webservice more frequently than once
# per minute! The web service will return nothing if executed more frequently!" The penalty is
# silent data denial, so this is enforced in code and not left to configuration.
MIN_INTERVAL_SEC = 60

_TIME_FMT = "%Y-%m-%d %H:%M:%S"


class AisHubError(Exception):
    """The response was not an observation. The cache must not be updated from it."""


def parse_time(raw: str) -> float | None:
    """AISHub's "2026-08-12 10:02:58 GMT" as a UNIX timestamp, or None.

    Always UTC: the field is documented as a UTC datetime and carries a literal "GMT" suffix,
    so it is stripped rather than parsed as a zone name (%Z does not round-trip reliably).
    """
    text = (raw or "").strip()
    if text.endswith(" GMT"):
        text = text[:-4]
    try:
        naive = datetime.datetime.strptime(text, _TIME_FMT)
    except (ValueError, TypeError):
        return None
    return naive.replace(tzinfo=datetime.timezone.utc).timestamp()


def parse_response(payload: bytes) -> list[dict]:
    """The ships in an AISHub response, or raise AisHubError.

    An empty list is a real answer -- a box that genuinely holds nothing. ERROR, a missing
    ships array, and unparseable content are all "we learned nothing", which is a different
    fact and must never reach the cache as an emptiness claim.
    """
    try:
        body = json.loads(payload.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError) as exc:
        raise AisHubError(f"response was not JSON: {exc}") from exc

    if not isinstance(body, list) or not body:
        raise AisHubError(f"unexpected response shape: {type(body).__name__}")

    if not isinstance(body[0], dict):
        raise AisHubError(f"unexpected envelope shape: {type(body[0]).__name__}")

    envelope = body[0]
    if envelope.get("ERROR"):
        detail = envelope.get("ERROR_MESSAGE") or "no message"
        raise AisHubError(f"server reported ERROR: {detail}")

    if len(body) < 2 or not isinstance(body[1], list):
        raise AisHubError("response carried no ships array")

    return body[1]


def _dimension(ship: dict, near: str, far: str):
    """Length or beam from the two half-dimensions, or None when neither was reported."""
    total = (ship.get(near) or 0) + (ship.get(far) or 0)
    return total or None


def _coerce_float(value) -> float | None:
    """A position coordinate as float, or None if it is missing or not numeric.

    `ais.record()` -> `_apply()` -> `ais._km_from_maas()` now runs INSIDE `record()`'s
    `_cache_lock`, called from `_refresh_name_view` -> `_candidate_sort_key`. A non-numeric
    LATITUDE/LONGITUDE reaching that far would raise TypeError mid-way through poll_once's
    write loop, leaving the cache partly written with `set_in_scope` already published --
    exactly the partial-write poll_once's own docstring says cannot happen. Mapping it to
    None here instead means `ais.record()` treats the position as absent, the same as any
    other AISHub field it never received.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def map_ship(ship: dict) -> dict | None:
    """One AISHub record as the fields `ais.record()` understands, or None if unusable."""
    mmsi = str(ship.get("MMSI") or "").strip()
    if not mmsi:
        return None
    return {
        "mmsi": mmsi,
        "name": (ship.get("NAME") or "").strip(),
        "callsign": (ship.get("CALLSIGN") or "").strip(),
        "type": ship.get("TYPE"),
        "imo": ship.get("IMO"),
        "length": _dimension(ship, "A", "B"),
        "beam": _dimension(ship, "C", "D"),
        "draught": ship.get("DRAUGHT"),
        "destination": _clean_destination(ship.get("DEST") or ""),
        "latitude": _coerce_float(ship.get("LATITUDE")),
        "longitude": _coerce_float(ship.get("LONGITUDE")),
        "sog": ship.get("SOG"),
        "cog": ship.get("COG"),
        "heading": ship.get("HEADING"),
    }


def _resolve_bbox() -> tuple[float, float, float, float]:
    """(latmin, latmax, lonmin, lonmax) for the poll.

    Wide on purpose. The margin is what buys lead time: the western edge sits ~140 km from
    Maas Center, which is over two hours of steaming at 15 knots, so a vessel is cached long
    before it calls and the poll can be slow. The cost is that a wide box carries 777
    duplicate-name groups against the approach box's 17 -- which is why matching ranks
    candidates by proximity rather than trusting the box to disambiguate.
    """
    raw = os.environ.get("AISHUB_BBOX", "51.0,53.2,2.0,6.0")
    try:
        latmin, latmax, lonmin, lonmax = (float(p) for p in raw.split(","))
    except ValueError:
        print(f"[AISHub] bad AISHUB_BBOX {raw!r}, using the default", flush=True)
        return (51.0, 53.2, 2.0, 6.0)
    return (latmin, latmax, lonmin, lonmax)


def _resolve_poll_sec() -> int:
    """Seconds between polls, never below the rate limit whatever the environment says."""
    try:
        wanted = int(os.environ.get("AISHUB_POLL_SEC", "900"))
    except ValueError:
        wanted = 900
    return max(wanted, MIN_INTERVAL_SEC)


BBOX     = _resolve_bbox()
POLL_SEC = _resolve_poll_sec()


def build_url(username: str, bbox: tuple[float, float, float, float]) -> str:
    latmin, latmax, lonmin, lonmax = bbox
    query = urllib.parse.urlencode({
        "username": username,
        "format": 1,            # human-readable: degrees and knots, not raw AIS scaling
        "output": "json",
        "latmin": latmin, "latmax": latmax,
        "lonmin": lonmin, "lonmax": lonmax,
    })
    return f"{API_URL}?{query}"


def _fetch(url: str) -> bytes:
    """GET the URL with gzip. Uncompressed this box is 2.66 MB a poll."""
    request = urllib.request.Request(url, headers={
        "Accept-Encoding": "gzip",
        "User-Agent": "sdrsharp-stt-proxy/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return raw
    except (urllib.error.URLError, OSError) as exc:
        raise AisHubError(f"fetch failed: {exc}") from exc


def poll_once(username: str, bbox, fetch=None) -> int:
    """One poll. Returns vessels recorded, or raises AisHubError having changed nothing.

    Every ship in the response is validated and mapped to a (fields, observed_at) pair in a
    first pass, entirely before the second pass writes anything to the cache. A malformed
    element anywhere in the list -- `parse_response` checks that `body[1]` is a list but not
    that its elements are dicts -- therefore raises AisHubError with the cache and the
    in-scope set exactly as they were, the same guarantee the ERROR-flag case already gave.
    Splitting validation from writing is what makes that true rather than merely documented.

    `set_in_scope` runs immediately BEFORE the write loop, not after: `_refresh_name_view`
    (called from inside every `ais.record()` in the loop) ranks candidates against whatever
    scope is currently published, so publishing it after the loop would leave every ranking
    decision made during THIS poll working off the PREVIOUS poll's scope -- self-correcting a
    poll later, but meaning `_vessel_cache[NAME]` and `candidates_for_name(NAME)[0]` (which
    reads the now-current scope) could disagree for one whole poll interval. Moving this
    earlier does not weaken the atomicity guarantee above: validation above still fails, when
    it fails, before this line is ever reached, so a failed poll still changes nothing.
    """
    ships = parse_response((fetch or _fetch)(build_url(username, bbox)))

    to_record: list[tuple[dict, float | None]] = []
    seen: set[str] = set()
    for ship in ships:
        if not isinstance(ship, dict):
            raise AisHubError(f"malformed ship record: {type(ship).__name__}")
        fields = map_ship(ship)
        if fields is None:
            continue
        to_record.append((fields, parse_time(ship.get("TIME", ""))))
        seen.add(fields["mmsi"])

    ais.set_in_scope(seen)
    for fields, observed_at in to_record:
        ais.record(fields, source="aishub", observed_at=observed_at)

    return len(to_record)


def poll_loop(username: str) -> None:
    """Poll forever. Daemon-thread entry point; never raises."""
    print(f"[AISHub] polling {BBOX} every {POLL_SEC}s", flush=True)
    failures = 0
    while True:
        try:
            count = poll_once(username, BBOX)
            if failures:
                print(f"[AISHub] recovered after {failures} failed poll(s)", flush=True)
            failures = 0
            print(f"[AISHub] {count} vessels", flush=True)
        except AisHubError as exc:
            failures += 1
            # Rate-limited every time would be a configuration bug, so say so early and then
            # stop repeating it; a long outage should not drown the console the way the
            # aisstream silence warning did.
            if failures <= 3 or failures % 20 == 0:
                print(f"[AISHub] poll failed ({failures}): {exc}. "
                      f"Cache and scope left untouched.", flush=True)
        except Exception as exc:
            failures += 1
            print(f"[AISHub] unexpected poll error: {exc}", flush=True)
        time.sleep(POLL_SEC)


def start(username: str) -> None:
    threading.Thread(target=poll_loop, args=(username,), daemon=True).start()
