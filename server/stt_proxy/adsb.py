"""adsb.fi as the live aircraft source for airband flight identification.

Polls adsb.fi's free point/radius endpoint and keeps a snapshot of aircraft currently within
range. Unlike ais.py's cache, this is a full REPLACE on every poll, not an accumulating merge
-- an aircraft is only relevant for the short window it is actually within radio range, so
there is no AIS-style "still in scope from an hour ago" question to answer.

Checked live before choosing this source (2026-09-08): airplanes.live's documented no-key
access now returns HTTP 403 asking for manual approval; adsb.lol did not respond at all;
adsb.one is behind a Cloudflare JS challenge a server-side poller cannot pass. adsb.fi is the
only one of four candidates that actually works.
"""

import json
import os
import threading
import time
import urllib.error
import urllib.request

API_HOST = "https://opendata.adsb.fi"

# Centred to cover Schiphol, Rotterdam, and the Scheveningen coastal corridor -- the exact
# point tested live during design (2026-09-08), which returned 59 real aircraft including
# KLM281, RYR37DV, AFR16JN, EZY85FV and PHVSY (a Dutch-registered light aircraft).
POINT_LAT     = float(os.environ.get("ADSB_LAT", "52.15"))
POINT_LON     = float(os.environ.get("ADSB_LON", "4.3"))
POINT_DIST_NM = float(os.environ.get("ADSB_DIST_NM", "40"))

# No published rate limit was found for adsb.fi during design (unlike AISHub's documented
# "once per minute"), so this is a conservative starting point, not an enforced server fact.
# Aircraft move at 150-250 m/s on approach -- far faster than ships -- so it needs to be much
# more frequent than AISHub's 900s.
POLL_SEC = int(os.environ.get("ADSB_POLL_SEC", "15"))


class AdsbError(Exception):
    """The response was not a real observation. The cache must not be updated from it."""


def parse_response(payload: bytes) -> list[dict]:
    """The aircraft in an adsb.fi response, or raise AdsbError.

    An empty list is a real answer -- a radius that genuinely holds no traffic right now.
    A missing or wrongly-shaped `aircraft` key, or unparseable content, is "we learned
    nothing", which must never reach the cache disguised as "there is no traffic".
    """
    try:
        body = json.loads(payload.decode("utf-8", errors="replace"))
    except (ValueError, AttributeError) as exc:
        raise AdsbError(f"response was not JSON: {exc}") from exc

    if not isinstance(body, dict):
        raise AdsbError(f"unexpected response shape: {type(body).__name__}")

    aircraft = body.get("aircraft")
    if not isinstance(aircraft, list):
        raise AdsbError(f"response carried no aircraft list: {type(aircraft).__name__}")

    return aircraft


def map_aircraft(ac: dict) -> dict | None:
    """One adsb.fi aircraft record as the fields this module caches, or None if unusable.

    hex is the cache key (always present and stable for the aircraft's session, unlike
    `flight` which can be blank or padded) -- the same reasoning aishub.py's map_ship uses
    keying on MMSI rather than NAME.
    """
    hex_id = str(ac.get("hex") or "").strip()
    if not hex_id:
        return None
    return {
        "hex": hex_id,
        "flight": str(ac.get("flight") or "").strip(),
        "r": ac.get("r"),
        "t": ac.get("t"),
        "alt_baro": ac.get("alt_baro"),
        "gs": ac.get("gs"),
        "track": ac.get("track"),
        "lat": ac.get("lat"),
        "lon": ac.get("lon"),
        "squawk": ac.get("squawk"),
    }


def build_url(lat: float, lon: float, dist_nm: float) -> str:
    return f"{API_HOST}/api/v2/lat/{lat}/lon/{lon}/dist/{dist_nm}"


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={
        "User-Agent": "sdrsharp-stt-proxy/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.read()
    except (urllib.error.URLError, OSError) as exc:
        raise AdsbError(f"fetch failed: {exc}") from exc


_cache_lock = threading.Lock()
_aircraft_cache: dict[str, dict] = {}


def current_aircraft() -> list[dict]:
    """A snapshot of the live cache. Safe to iterate without holding the lock yourself."""
    with _cache_lock:
        return list(_aircraft_cache.values())


def poll_once(lat: float, lon: float, dist_nm: float, fetch=None) -> int:
    """One poll. Returns aircraft cached, or raises AdsbError having changed nothing.

    Every aircraft is validated and mapped in a first pass, entirely before the cache is
    replaced -- a malformed element anywhere in the response therefore raises AdsbError with
    the cache exactly as it was, the same guarantee aishub.py's poll_once gives for ships.
    """
    aircraft = parse_response((fetch or _fetch)(build_url(lat, lon, dist_nm)))

    mapped: dict[str, dict] = {}
    for ac in aircraft:
        if not isinstance(ac, dict):
            raise AdsbError(f"malformed aircraft record: {type(ac).__name__}")
        fields = map_aircraft(ac)
        if fields is None:
            continue
        fields["last_seen"] = time.time()
        mapped[fields["hex"]] = fields

    with _cache_lock:
        _aircraft_cache.clear()
        _aircraft_cache.update(mapped)

    return len(mapped)


# -- what the feed can be asked about itself, same shape as aishub.py's feed_status ----

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
    with _feed_lock:
        return {
            "last_ok_at": _last_ok_at,
            "last_error_at": _last_error_at,
            "last_error": _last_error,
            "last_count": _last_count,
            "consecutive_failures": _consecutive_failures,
            "poll_sec": POLL_SEC,
        }


def _record_success(count: int) -> None:
    global _last_ok_at, _last_count, _consecutive_failures
    with _feed_lock:
        _last_ok_at = time.time()
        _last_count = count
        _consecutive_failures = 0
    print(f"[adsb.fi] {count} aircraft", flush=True)


def _record_failure(reason: str) -> None:
    global _last_error_at, _last_error, _consecutive_failures
    with _feed_lock:
        _last_error_at = time.time()
        _last_error = reason
        _consecutive_failures += 1
        failures = _consecutive_failures
    # Same pacing as aishub.py: say it early, then stop repeating so a long outage doesn't
    # drown the console.
    if failures <= 3 or failures % 20 == 0:
        print(f"[adsb.fi] poll failed ({failures}): {reason}. Cache left untouched.",
              flush=True)


def poll_and_record(lat: float, lon: float, dist_nm: float, fetch=None) -> None:
    """One poll and its consequences for the feed's own state. Never raises.

    Every exception is caught here because the caller is a daemon thread with nothing above
    it -- an error that escaped would end polling silently and leave the cache frozen.
    """
    try:
        _record_success(poll_once(lat, lon, dist_nm, fetch=fetch))
    except AdsbError as exc:
        _record_failure(str(exc))
    except Exception as exc:
        _record_failure(f"{type(exc).__name__}: {exc}")


def poll_loop(lat: float, lon: float, dist_nm: float) -> None:
    """Poll forever. Daemon-thread entry point; never raises."""
    print(f"[adsb.fi] polling ({lat}, {lon}) within {dist_nm}nm every {POLL_SEC}s", flush=True)
    while True:
        poll_and_record(lat, lon, dist_nm)
        time.sleep(POLL_SEC)


def start(lat: float, lon: float, dist_nm: float) -> None:
    threading.Thread(target=poll_loop, args=(lat, lon, dist_nm), daemon=True).start()
