"""Filtering, paging and projecting conversation records. Pure functions, no I/O.

The list projection drops transcripts and candidate lists on purpose: the list is polled every
few seconds and the store is 613 KB. Detail is complete, and is fetched once when a row is
opened.
"""
from __future__ import annotations

import ship_types
from pydantic import BaseModel


class Page(BaseModel):
    rows: list[dict]
    total: int
    offset: int
    limit: int


def conversation_id(record: dict) -> str:
    """Stable within one proxy run. Start instant plus channel: the store has no id of its own,
    and index position shifts as conversations are added."""
    return f"{record.get('start') or ''}|{record.get('channel') or ''}"


def _identified(record: dict) -> bool:
    return bool(record.get("mmsi") or record.get("vessel"))


def _label(identified: bool, vessel: str | None, mmsi: str | None) -> str:
    """Never the bare name: where two cached vessels share one, the name is not an
    identification and reading it as one distorted seven labelled conversations.

    Shared by summarise() (the list) and detail() (a single record) so a detail response can
    never fall back to the bare, ambiguous name just because it reached the API by a path that
    forgot to compute this."""
    if identified and vessel and mmsi:
        return f"{vessel} ({mmsi})"
    return vessel or mmsi or "unidentified"


def summarise(record: dict) -> dict:
    identified = _identified(record)
    vessel, mmsi = record.get("vessel"), record.get("mmsi")
    return {
        "id": conversation_id(record),
        "start": record.get("start"),
        "end": record.get("end"),
        "channel": record.get("channel"),
        "vessel": vessel,
        "mmsi": mmsi,
        "label": _label(identified, vessel, mmsi),
        "type": record.get("type"),
        # The stored `type` is the category, which has already dropped the hazard digit. The
        # code is what still carries it, and the proxy only began storing it on 2026-08-20 --
        # so this is None on every older record rather than a reading invented from the word.
        "type_detail": ship_types.describe(record.get("type_code")),
        "destination": record.get("destination"),
        "identified": identified,
        # Dropped on unidentified rows: the confidence describes the reasoning, and printed
        # beside "unidentified" it reads as a contradiction.
        "confidence": record.get("confidence") if identified else None,
        "turn_count": len(record.get("turns") or []),
        "candidate_count": len(record.get("resolver_candidates") or []),
    }


def _turn(turn: dict) -> dict:
    raw, text = turn.get("raw"), turn.get("text")
    conv = turn.get("conv")
    live_vessel, live_mmsi = turn.get("live_vessel"), turn.get("live_mmsi")
    return {
        "time": turn.get("time"),
        "raw": raw,
        "text": text,
        # Absent means the correction pass changed nothing OR failed; the store cannot tell
        # them apart, so this says only what is known.
        "conv": conv,
        "changed_by_regex": bool(raw is not None and text is not None and raw != text),
        "changed_by_llm": bool(conv is not None and conv != text),
        "live_vessel": live_vessel,
        "live_mmsi": live_mmsi,
        "live_match": None if not live_vessel
                      else ("ais-confirmed" if live_mmsi else "heard-only"),
    }


def detail(record: dict) -> dict:
    out = dict(record)
    out["id"] = conversation_id(record)
    identified = _identified(record)
    out["identified"] = identified
    out["turns"] = [_turn(t) for t in (record.get("turns") or [])]
    if not identified:
        out["confidence"] = None
    # Same computation as summarise()'s row -- a caller that only fetched detail (the vessel ->
    # conversation link on the Vessels screen is exactly this) must see the same shared-name-safe
    # title the list would have shown, not the raw `vessel` field.
    out["label"] = _label(identified, record.get("vessel"), record.get("mmsi"))
    # Same reason as label: a screen reached only through detail must offer the same reading of
    # the type as the list row would.
    out["type_detail"] = ship_types.describe(record.get("type_code"))
    return out


def _haystack(record: dict) -> str:
    parts = [str(record.get(k) or "") for k in ("vessel", "mmsi", "callsign", "destination",
                                                "channel", "evidence")]
    for turn in record.get("turns") or []:
        parts += [str(turn.get(k) or "") for k in ("raw", "text", "conv")]
    return " ".join(parts).lower()


def query(records: list[dict], *, identified: bool | None = None, channel: str | None = None,
          text: str | None = None, limit: int = 50, offset: int = 0) -> Page:
    found = list(records)
    if identified is not None:
        found = [r for r in found if _identified(r) is identified]
    if channel:
        found = [r for r in found if (r.get("channel") or "") == channel]
    if text:
        needle = text.strip().lower()
        found = [r for r in found if needle in _haystack(r)]

    found.sort(key=lambda r: str(r.get("start") or ""), reverse=True)
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    return Page(rows=[summarise(r) for r in found[offset:offset + limit]],
                total=len(found), offset=offset, limit=limit)
