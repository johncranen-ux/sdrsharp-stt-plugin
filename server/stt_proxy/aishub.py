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
import json

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

    envelope = body[0] if isinstance(body[0], dict) else {}
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
        "latitude": ship.get("LATITUDE"),
        "longitude": ship.get("LONGITUDE"),
        "sog": ship.get("SOG"),
        "cog": ship.get("COG"),
        "heading": ship.get("HEADING"),
    }
