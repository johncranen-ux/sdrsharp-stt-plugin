"""Which flight an Approach 4 transmission belongs to, from two independent clues.

Clue 1 is the callsign tag flight_identify already produces. Clue 2 -- the "echo" -- is an
aircraft whose autopilot SELECTED altitude or heading changes to a number spoken in the
transmission within seconds of it. The echo clue is computed and stored for every
transmission, but only counts towards the displayed outcome when AIR_ECHO_ENABLED is on, which
it is not until bench_air_echo.py says it has earned it.

Two stages, because a cleared altitude reaches ADS-B only after the transmission: stage 1
stores the callsign outcome at once; stage 2 runs once t+60 s has passed.

See docs/superpowers/specs/2026-09-24-airband-conversations-design.md.
"""
from __future__ import annotations

import datetime
import os
import re
import threading
import time

import air_archive
from stt_proxy import adsb, flight_identify
from stt_proxy.air_numbers import Number, extract_numbers, from_dict, to_dict

AIR_CONVERSATION_CHANNELS = frozenset({"121.200", "121,200", "121.205", "121,205"})

AIR_ECHO_ENABLED = os.environ.get("AIR_ECHO_ENABLED", "off").strip().lower() == "on"

WINDOW_BEFORE_S = 10.0
WINDOW_AFTER_S = 60.0
ALT_TOLERANCE_FT = 100
HEADING_TOLERANCE_DEG = 5.0
RECHECK_EVERY_S = 15.0

_STATE_FIELDS = ("flight", "t", "r", "alt_baro", "nav_altitude_mcp", "nav_heading", "track")


def _field_for(n: Number) -> str:
    return "nav_altitude_mcp" if n.kind == "altitude" else "nav_heading"


def _close(n: Number, value) -> bool:
    if not isinstance(value, (int, float)):
        return False
    if n.kind == "altitude":
        return abs(value - n.value) <= ALT_TOLERANCE_FT
    diff = abs((value - n.value + 180.0) % 360.0 - 180.0)
    return diff <= HEADING_TOLERANCE_DEG


def echo_clue(numbers: list[Number], t: float, snapshots: list[dict]) -> dict:
    empty = {"hex": None, "flight": None, "number": None, "delay_s": None, "candidates": []}
    if not numbers:
        return {"status": "no_numbers", **empty}
    before = [s for s in snapshots if s["t"] <= t - WINDOW_BEFORE_S]
    after = [s for s in snapshots if t - WINDOW_BEFORE_S < s["t"] <= t + WINDOW_AFTER_S]
    if not before or not after:
        return {"status": "no_snapshots", **empty}
    baseline = {a.get("hex"): a for a in before[-1]["aircraft"]}

    hits: dict[str, dict] = {}
    for n in numbers:
        field = _field_for(n)
        for snap in after:
            for a in snap["aircraft"]:
                hex_ = a.get("hex")
                if hex_ in hits or hex_ not in baseline or a.get("alt_baro") == "ground":
                    continue
                baseline_val = baseline[hex_].get(field)
                if not isinstance(baseline_val, (int, float)):
                    continue    # "already set" needs a known value to differ from
                if _close(n, a.get(field)) and not _close(n, baseline_val):
                    hits[hex_] = {"hex": hex_, "flight": a.get("flight"),
                                  "number": to_dict(n), "delay_s": snap["t"] - t}
    if not hits:
        return {"status": "none", **empty}
    if len(hits) > 1:
        return {"status": "ambiguous", **empty, "candidates": sorted(hits)}
    only = next(iter(hits.values()))
    return {"status": "match", **only, "candidates": [only["hex"]]}


def _heard_key(candidate: str) -> str:
    return "heard:" + re.sub(r"[^A-Z0-9]", "", candidate.upper())[:12]


def combine(callsign: dict, echo: dict | None, echo_enabled: bool) -> dict:
    cs_hex = callsign.get("hex")
    echo_hex = (echo or {}).get("hex") if (echo or {}).get("status") == "match" else None
    if not echo_enabled:
        echo_hex = None

    if callsign.get("candidate") and not cs_hex:
        return {"kind": "unknown", "key": _heard_key(callsign["candidate"]), "badge": "unknown"}
    if cs_hex and echo_hex and cs_hex != echo_hex:
        return {"kind": "review", "key": "review", "badge": "conflict"}
    if cs_hex and echo_hex:
        return {"kind": "flight", "key": cs_hex, "badge": "confirmed"}
    if cs_hex:
        return {"kind": "flight", "key": cs_hex, "badge": "callsign"}
    if echo_hex:
        return {"kind": "flight", "key": echo_hex, "badge": "echo"}
    return {"kind": "unassigned", "key": "unassigned", "badge": "none"}


def _state_of(hex_: str | None, aircraft: list[dict], airline: str | None = None) -> dict | None:
    if not hex_:
        return None
    for a in aircraft:
        if a.get("hex") == hex_:
            state = {k: a.get(k) for k in _STATE_FIELDS}
            state["airline"] = airline or _airline_name(a.get("flight"))
            return state
    return None


def _airline_name(flight: str | None) -> str | None:
    """'KLM12B' -> 'KLM', 'DAL73' -> 'Delta', from flight_identify's own telephony table."""
    code = (flight or "")[:3].upper()
    for word, icao in flight_identify.AIRLINE_TELEPHONY.items():
        if icao == code and len(word) > 3:
            return word.capitalize()
    return code or None


def _db_path():
    from stt_proxy import conversations           # late: a heavy module, and conftest patches it
    return conversations.CONVERSATIONS_DB


def record_transmission(text: str, channel: str, now: float | None = None,
                        db_path=None) -> int | None:
    """Stage 1. Never raises: the plugin is waiting for its transcription."""
    try:
        t = time.time() if now is None else now
        clue = flight_identify.callsign_clue(text)
        numbers = extract_numbers(text)
        outcome = combine(clue, None, echo_enabled=AIR_ECHO_ENABLED)
        state_hex = outcome["key"] if outcome["kind"] == "flight" else None
        row = {
            "t": datetime.datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds"),
            "epoch": t, "channel": channel, "text": text,
            "numbers": [to_dict(n) for n in numbers], "callsign_clue": clue,
            "state": _state_of(state_hex, adsb.current_aircraft()),
            "outcome_kind": outcome["kind"], "outcome_key": outcome["key"],
            "badge": outcome["badge"],
        }
        with air_archive.open_db(db_path or _db_path()) as conn:
            return air_archive.insert_transmission(conn, row)
    except Exception as exc:
        print(f"[air] could not record transmission: {type(exc).__name__}: {exc}", flush=True)
        return None


def recheck_pending(now: float | None = None, db_path=None, snapshots_between=None) -> int:
    """Stage 2 for every transmission older than 60 s plus one poll that has no echo clue yet.

    One row's failure (a bad log line, a locked DB, a malformed stored number) must not abort
    the pass -- pending_echo orders by epoch, so an unhandled exception here would permanently
    stall every later row behind the broken one.
    """
    t_now = time.time() if now is None else now
    source = snapshots_between or adsb.snapshots_between
    done = 0
    with air_archive.open_db(db_path or _db_path()) as conn:
        # One poll interval past the window, so the poll covering t+60 s has landed.
        for row in air_archive.pending_echo(conn, t_now - WINDOW_AFTER_S - adsb.POLL_SEC):
            try:
                t = row["epoch"]
                snaps = source(t - WINDOW_BEFORE_S - adsb.POLL_SEC * 2, t + WINDOW_AFTER_S)
                echo = echo_clue([from_dict(n) for n in row["numbers"] or []], t, snaps)
                outcome = combine(row["callsign_clue"], echo, echo_enabled=AIR_ECHO_ENABLED)
                # Spec Section 5: each transmission stores the aircraft's state AT THAT MOMENT.
                # Keep stage 1's state (state=None -> set_echo's COALESCE preserves it) unless
                # stage 2 actually reassigns the outcome to a different flight, in which case
                # the state must come from the snapshot at-or-before t, not the end of the
                # lookback window (which is up to WINDOW_AFTER_S later than the transmission).
                state = None
                if outcome["kind"] == "flight" and outcome["key"] != row["outcome_key"]:
                    at_or_before = [s for s in snaps if s["t"] <= t]
                    aircraft = at_or_before[-1]["aircraft"] if at_or_before else []
                    state = _state_of(outcome["key"], aircraft)
                air_archive.set_echo(conn, row["id"], echo, outcome, state)
                done += 1
            except Exception as exc:
                print(f"[air] stage-2 row {row['id']} failed: {type(exc).__name__}: {exc}",
                      flush=True)
    return done


def _loop() -> None:
    while True:
        try:
            recheck_pending()
        except Exception as exc:
            print(f"[air] stage-2 pass failed: {type(exc).__name__}: {exc}", flush=True)
        time.sleep(RECHECK_EVERY_S)


def start() -> None:
    threading.Thread(target=_loop, daemon=True).start()
