"""Searching and projecting the AIS cache. Pure functions, no I/O."""
from __future__ import annotations

from collections import defaultdict

from webapp.conversations_view import Page, summarise

_FIELDS = ("mmsi", "name", "callsign", "destination")


def duplicate_names(entries: list[dict]) -> dict[str, list[str]]:
    """Names carried by more than one MMSI. A shared name is not an identification."""
    by_name: dict[str, list[str]] = defaultdict(list)
    for entry in entries:
        name = (entry.get("name") or "").strip().upper()
        if name:
            by_name[name].append(str(entry.get("mmsi") or ""))
    return {name: mmsis for name, mmsis in by_name.items() if len(mmsis) > 1}


def _row(entry: dict, shared: dict[str, list[str]]) -> dict:
    name = (entry.get("name") or "").strip().upper()
    return {
        "mmsi": str(entry.get("mmsi") or ""),
        "name": entry.get("name"),
        "callsign": entry.get("callsign"),
        "type": entry.get("type"),
        "destination": entry.get("destination"),
        "draught": entry.get("draught"),
        "last_seen": entry.get("last_seen"),
        "source": entry.get("source"),
        "name_shared": name in shared,
    }


def search(entries: list[dict], *, text: str | None = None,
           limit: int = 50, offset: int = 0) -> Page:
    found = list(entries)
    if text:
        needle = text.strip().lower()
        found = [e for e in found
                 if any(needle in str(e.get(f) or "").lower() for f in _FIELDS)]
    # "" sorts before any timestamp, so a vessel never heard sorts last under reverse.
    found.sort(key=lambda e: str(e.get("last_seen") or ""), reverse=True)

    shared = duplicate_names(entries)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return Page(rows=[_row(e, shared) for e in found[offset:offset + limit]],
                total=len(found), offset=offset, limit=limit)


def detail(entries: list[dict], mmsi: str) -> dict | None:
    for entry in entries:
        if str(entry.get("mmsi") or "") == str(mmsi):
            return dict(entry)
    return None


def conversations_for(records: list[dict], mmsi: str) -> list[dict]:
    """By MMSI, never by name -- the name is exactly what cannot be trusted here."""
    found = [r for r in records if str(r.get("mmsi") or "") == str(mmsi)]
    found.sort(key=lambda r: str(r.get("start") or ""), reverse=True)
    return [summarise(r) for r in found]
