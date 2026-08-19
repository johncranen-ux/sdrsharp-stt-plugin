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

    `ais.record()` -> `_apply()` -> `ais._km_from_maas()` runs INSIDE `record()`'s
    `_cache_lock`, called from `_refresh_name_view` -> `_candidate_sort_key`. A non-numeric
    LATITUDE/LONGITUDE reaching that far would raise TypeError partway through poll_once's
    write loop -- one instance of the general class of failure this, `_coerce_str` and
    `_coerce_type_code` below all guard against (poll_once's docstring does not claim the
    write loop is exception-free, only that a failure there cannot corrupt the published
    scope; closing off the failure at the boundary is still worth doing). Mapping a bad value
    to None here means `ais.record()` treats the position as absent, the same as any other
    AISHub field it never received.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_str(value) -> str:
    """A text field as itself, or "" if it is missing or not a string.

    NAME, CALLSIGN and DEST are all unconditionally `.strip()`ed or `.split()`ed downstream,
    which raises AttributeError on anything that is not already a string -- a list or dict
    from a malformed or future AISHub response, say. "" is what those calls already treat an
    absent field as, so a non-string one degrades the same way instead of raising.
    """
    return value if isinstance(value, str) else ""


def _coerce_type_code(value) -> int | None:
    """AIS ship type as an int, or None if it is missing or not coercible.

    `ais._get_ship_type_name` uses this value as an `AIS_SHIP_TYPES` dict key
    (`AIS_SHIP_TYPES.get(type_code, ...)`), reached from `_refresh_name_view` ->
    `_candidate_sort_key` -> `_type_plausibility` while `record()` holds `_cache_lock`. An
    unhashable TYPE (a list or dict) would raise `TypeError: unhashable type` there -- this is
    the mechanism a code review round found still open after `_coerce_float` closed the same
    class of bug for LATITUDE/LONGITUDE.
    """
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def map_ship(ship: dict) -> dict | None:
    """One AISHub record as the fields `ais.record()` understands, or None if unusable."""
    mmsi = str(ship.get("MMSI") or "").strip()
    if not mmsi:
        return None
    return {
        "mmsi": mmsi,
        "name": _coerce_str(ship.get("NAME")).strip(),
        "callsign": _coerce_str(ship.get("CALLSIGN")).strip(),
        "type": _coerce_type_code(ship.get("TYPE")),
        "imo": ship.get("IMO"),
        "length": _dimension(ship, "A", "B"),
        "beam": _dimension(ship, "C", "D"),
        "draught": ship.get("DRAUGHT"),
        "destination": _clean_destination(_coerce_str(ship.get("DEST"))),
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

    `set_in_scope` runs AFTER the write loop, not before -- a previous version of this
    function moved it earlier to fix ranking staleness (see `ais.set_in_scope`'s docstring)
    and that broke a narrower guarantee: `ais.record()` itself can fail partway through the
    loop (a non-scalar AISHub TYPE reaching `_get_ship_type_name` inside `_refresh_name_view`
    was one real mechanism -- `map_ship` now coerces TYPE defensively, closing that specific
    one, but the loop calling into `ais.record()` is not proven exception-free in general, so
    the guarantee below does not depend on it being so). With scope published first, such a
    failure would leave `_in_scope` claiming the FULL scope of a poll that never finished
    writing, while `_mmsi_index` held only the ships processed before the failure -- the two
    disagreeing about which poll they describe. The write loop itself is NOT atomic across
    ships and this does not change that: a failure partway through can leave some ships
    written and others not, same as a real network drop mid-poll always could. What keeping
    `set_in_scope` last DOES guarantee is that the published scope never gets ahead of what
    was actually captured -- a failure anywhere in the loop leaves `_in_scope` exactly as it
    was before this call, describing the last poll that actually completed rather than one
    that didn't. The staleness `set_in_scope` running late used to cause is now handled
    inside `set_in_scope` itself, which re-ranks every name view under the same lock that
    publishes the new scope -- see its docstring.
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

    for fields, observed_at in to_record:
        ais.record(fields, source="aishub", observed_at=observed_at)

    ais.set_in_scope(seen)
    return len(to_record)


# -- what the feed can be asked about itself ---------------------------------
#
# The poll loop used to keep its failure count in a local variable, which meant the only thing
# anything outside this thread could learn was the age of the last SUCCESS -- and an old
# success cannot distinguish "polling normally, the box is just slow" from "every poll has
# been refused for an hour". The control panel needs the difference to light a lamp, so the
# loop's state lives here instead. Read under the lock; the writer is the poll thread and the
# reader is whichever HTTP thread is serving /api/status.

_feed_lock = threading.Lock()
_last_ok_at: float | None = None
_last_error_at: float | None = None
_last_error: str | None = None
_last_count: int | None = None
_consecutive_failures = 0


def reset_feed_state() -> None:
    """Back to "never polled". For tests; nothing in the running proxy calls this."""
    global _last_ok_at, _last_error_at, _last_error, _last_count, _consecutive_failures
    with _feed_lock:
        _last_ok_at = _last_error_at = _last_error = _last_count = None
        _consecutive_failures = 0


def feed_status() -> dict:
    """Enough to say whether the vessel feed is alive, and since when.

    `last_error` survives a recovery on purpose: a lamp that has just gone green is exactly
    when someone wants to know what it was doing red.
    """
    with _feed_lock:
        return {
            "last_ok_at": _last_ok_at,
            "last_error_at": _last_error_at,
            "last_error": _last_error,
            "last_count": _last_count,
            "consecutive_failures": _consecutive_failures,
            # Travels with the status so the dashboard can work out what "overdue" means for
            # this deployment rather than hardcoding the default interval.
            "poll_sec": POLL_SEC,
        }


def _record_success(count: int) -> None:
    global _last_ok_at, _last_count, _consecutive_failures
    with _feed_lock:
        recovered = _consecutive_failures
        _last_ok_at = time.time()
        _last_count = count
        _consecutive_failures = 0
    if recovered:
        print(f"[AISHub] recovered after {recovered} failed poll(s)", flush=True)
    print(f"[AISHub] {count} vessels", flush=True)


def _redact(reason: str, username: str) -> str:
    """The AISHub username IS the API key, and this string ends up in a browser.

    Nothing here deliberately puts the URL in an error message, but the message comes from
    whatever urllib raised, and this is the one place between that exception and the network
    where the key can be taken back out. Cheaper than auditing every exception type urllib
    might grow.
    """
    return reason.replace(username, "***") if username else reason


def _record_failure(reason: str, expected: bool) -> None:
    global _last_error_at, _last_error, _consecutive_failures
    with _feed_lock:
        _last_error_at = time.time()
        _last_error = reason
        _consecutive_failures += 1
        failures = _consecutive_failures
    if not expected:
        print(f"[AISHub] unexpected poll error: {reason}", flush=True)
    # Rate-limited every time would be a configuration bug, so say so early and then stop
    # repeating it; a long outage should not drown the console the way the aisstream silence
    # warning did. The lamp carries the outage now, so the console does not have to.
    elif failures <= 3 or failures % 20 == 0:
        print(f"[AISHub] poll failed ({failures}): {reason}. "
              f"Cache and scope left untouched.", flush=True)


def poll_and_record(username: str, bbox, fetch=None) -> None:
    """One poll and its consequences for the feed's own state. Never raises.

    Split out of `poll_loop` so the outcome recording can be tested without an infinite loop.
    Every exception is caught here because the caller is a daemon thread with nothing above it:
    an error that escaped would end polling silently and leave the cache frozen, which is the
    failure aisstream spent five days demonstrating.
    """
    try:
        _record_success(poll_once(username, bbox, fetch=fetch))
    except AisHubError as exc:
        _record_failure(_redact(str(exc), username), expected=True)
    except Exception as exc:
        _record_failure(_redact(f"{type(exc).__name__}: {exc}", username), expected=False)


def poll_loop(username: str) -> None:
    """Poll forever. Daemon-thread entry point; never raises."""
    print(f"[AISHub] polling {BBOX} every {POLL_SEC}s", flush=True)
    while True:
        poll_and_record(username, BBOX)
        time.sleep(POLL_SEC)


def start(username: str) -> None:
    threading.Thread(target=poll_loop, args=(username,), daemon=True).start()
