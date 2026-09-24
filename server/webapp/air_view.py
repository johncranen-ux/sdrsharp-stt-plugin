"""The Airband tab's shape: strips per flight, one flight's thread, and where a row may move.

Pure -- reads nothing from disk. The routes in app.py fetch rows from air_archive and hand
them here, the same split conversations_view.py keeps.
"""
from __future__ import annotations

import datetime

LIVE_WINDOW_S = 900
MOVE_TARGET_WINDOW_S = 600

_SPECIAL_ORDER = {"review": 1, "unknown": 2, "unassigned": 3}


def _kind_of(key: str) -> str:
    if key == "unassigned":
        return "unassigned"
    if key == "review":
        return "review"
    if key.startswith("heard:"):
        return "unknown"
    return "flight"


def _label(key: str, state: dict | None) -> str:
    kind = _kind_of(key)
    if kind == "review":
        return "Needs review"
    if kind == "unassigned":
        return "Unassigned"
    if kind == "unknown":
        return key.split(":", 1)[1]
    return (state or {}).get("flight") or key.upper()


def parse_range(frm: str | None, to: str | None, now: float) -> tuple[float, float, bool]:
    if not frm and not to:
        return now - LIVE_WINDOW_S, now, True
    try:
        start = datetime.datetime.fromisoformat(frm).timestamp() if frm else now - 3600
        end = datetime.datetime.fromisoformat(to).timestamp() if to else now
    except (TypeError, ValueError) as exc:
        raise ValueError(f"not an ISO-8601 time: {exc}") from None
    if start > end:
        raise ValueError("from is after to")
    return start, end, False


def strips(rows: list[dict], now: float, live: bool) -> list[dict]:
    groups: dict[str, dict] = {}
    for r in sorted(rows, key=lambda r: r["epoch"]):
        key = r["effective_key"]
        g = groups.setdefault(key, {"key": key, "kind": _kind_of(key), "count": 0,
                                    "last_epoch": 0.0, "last_t": None, "state": None})
        g["count"] += 1
        g["last_epoch"] = r["epoch"]
        g["last_t"] = r["t"]
        if r.get("state") and not r.get("moved"):
            # A moved row still carries the state of the aircraft it was moved away from.
            g["state"] = r["state"]
    out = []
    for g in groups.values():
        if live and g["last_epoch"] < now - LIVE_WINDOW_S:
            continue
        state = g["state"] or {}
        g.update(label=_label(g["key"], g["state"]), airline=state.get("airline"),
                 type=state.get("t"), reg=state.get("r"))
        out.append(g)
    out.sort(key=lambda g: (_SPECIAL_ORDER.get(g["kind"], 0), -g["last_epoch"]))
    return out


def evidence(row: dict) -> str:
    if row.get("moved"):
        return "moved by hand"
    parts = []
    clue = row.get("callsign_clue") or {}
    if clue.get("flight") and clue.get("repaired_from"):
        # Not heard as a callsign: the decoder wrote "QNH" where "KLM" was said, and the
        # number after it named exactly one KLM flight in range. Say so -- the text shows QNH.
        parts.append(f'callsign {clue["flight"]} read from "{clue["repaired_from"]}"')
    elif clue.get("flight"):
        parts.append(f"callsign {clue['flight']} heard")
    elif clue.get("candidate"):
        parts.append(f"callsign {clue['candidate']} heard, not in ADS-B")
    echo = row.get("echo_clue") or {}
    status = echo.get("status")
    if status == "match":
        n = echo.get("number") or {}
        unit = "ft" if n.get("kind") == "altitude" else "°"
        parts.append(f"{echo.get('flight') or echo.get('hex')} selected {n.get('value')} "
                     f"{unit} {round(echo.get('delay_s') or 0)} s later")
    elif status == "ambiguous":
        parts.append("autopilot: several aircraft changed to that number")
    elif status == "no_snapshots":
        parts.append("autopilot: no ADS-B data for that moment")
    elif status is None:
        parts.append("autopilot: not checked yet")
    return "; ".join(parts) or "no clue"


def thread(rows: list[dict], key: str) -> list[dict]:
    mine = sorted((r for r in rows if r["effective_key"] == key), key=lambda r: r["epoch"])
    return [{"id": r["id"], "t": r["t"], "epoch": r["epoch"], "text": r["text"],
             "badge": "moved" if r.get("moved") else r["badge"], "moved": r.get("moved", False),
             "evidence": evidence(r), "effective_key": r["effective_key"]} for r in mine]


def move_targets(rows: list[dict], epoch: float) -> list[dict]:
    """Flights heard within +/-10 minutes of `epoch`, oldest first, then Unassigned."""
    labels: dict[str, str] = {}
    for r in sorted(rows, key=lambda r: r["epoch"]):
        key = r["effective_key"]
        if _kind_of(key) != "flight" or abs(r["epoch"] - epoch) > MOVE_TARGET_WINDOW_S:
            continue
        state = None if r.get("moved") else r.get("state")
        if key not in labels or state:
            labels[key] = _label(key, state)
    out = [{"key": k, "label": v} for k, v in labels.items()]
    out.append({"key": "unassigned", "label": "Unassigned"})
    return out
