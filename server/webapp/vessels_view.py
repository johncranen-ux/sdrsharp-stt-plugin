"""Searching and projecting the AIS cache. Pure functions, no I/O."""
from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable

from webapp.conversations_view import Page, summarise

_FIELDS = ("mmsi", "name", "callsign", "destination")
_WILDCARDS = ("?", "*")


def _matcher(text: str) -> Callable[[str], bool]:
    """A predicate over one lowercased field value.

    `?` is one character and `*` is any run, because a callsign is spelled out letter by letter
    over VHF and a single lost character is the usual failure -- "P-something-Q-Q" has to be
    expressible.

    Three things this deliberately does:
    - Everything else is escaped first, so a vessel named CONDOR (II) is searchable and a bare
      "." cannot quietly become "match anything" and return the whole cache.
    - Substring semantics, via search() rather than fullmatch(): plain text already behaves
      that way, so P?QQ finds PBQQ1 exactly as PBQQ would.
    - Plain text keeps the untouched fast path. This runs over ~6000 entries x 4 fields on
      every debounced keystroke, and there is no reason to make the common case pay for the
      rare one. The pattern is compiled once here, not per entry, for the same reason.
    """
    needle = text.strip().lower()
    if not any(w in needle for w in _WILDCARDS):
        return lambda value: needle in value
    pattern = re.compile(re.escape(needle).replace(r"\?", ".").replace(r"\*", ".*"))
    return lambda value: pattern.search(value) is not None


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
        matches = _matcher(text)
        found = [e for e in found
                 if any(matches(str(e.get(f) or "").lower()) for f in _FIELDS)]
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
