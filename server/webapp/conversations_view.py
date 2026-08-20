"""Filtering, paging and projecting conversation records. Pure functions, no I/O.

The list projection drops transcripts and candidate lists on purpose: the list is polled every
few seconds and the store is 613 KB. Detail is complete, and is fetched once when a row is
opened.
"""
from __future__ import annotations

import datetime

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


# The resolver refuses to promote a live match whose ship was not seen inside
# AIS_LIVE_MATCH_MAX_AGE_MIN. That is an operator SETTING, so the screen reads the same setting
# rather than hard-coding its default: a hard-coded six hours agreed with the resolver only
# while nobody changed it, and the moment it was tightened the screen would go on calling a
# match confirmed that the resolver had already refused. Same fact, one source.
LIVE_CONFIRM_DEFAULT_MIN = 360


def confirm_max_age_hours(values: dict | None) -> float:
    """How old a live match may be and still be called confirmed, in hours.

    0 means "no bound" to the resolver -- a rollback lever for identification behaviour. It is
    NOT an instruction to call a week-old fix a confirmation, so the display keeps its default
    there; honouring it would restore exactly the misleading label this replaced. Anything
    unparseable falls back the same way: a bad setting must not decide what the screen asserts.
    """
    raw = (values or {}).get("AIS_LIVE_MATCH_MAX_AGE_MIN")
    try:
        minutes = int(str(raw).strip())
    except (TypeError, ValueError):
        minutes = LIVE_CONFIRM_DEFAULT_MIN
    if minutes <= 0:
        minutes = LIVE_CONFIRM_DEFAULT_MIN
    return minutes / 60.0


def _live_age_hours(start, live_seen) -> float | None:
    """Hours between the ship's last AIS fix and the call, or None if that cannot be known.

    Negative ages clamp to zero rather than failing the freshness test: the cache is written
    asynchronously, so a fix stamped a little after the turn is ordinary, not suspicious.
    """
    began, seen = _parse_stamp(start), _parse_stamp(live_seen)
    if began is None or seen is None:
        return None
    return max(0.0, (began - seen).total_seconds() / 3600.0)


def _parse_stamp(value):
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(text).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def _live_match(live_vessel, live_mmsi, age_hours, confirm_max_age_h) -> str | None:
    """What the screen is allowed to claim about a per-turn AIS match.

    The per-turn matcher runs at AIS_NAME_MIN_SCORE=76 with no recency check, so "confirmed"
    was being printed for ships days away -- 21% of labelled turns on the live store, AUGUSTA
    among them at seven days old, matched off the fragment "Gustav" by 0.9 of a point. The
    resolver had already rejected that same match as stale. Four states, because "we cannot
    tell" is genuinely different from "it is stale":

      heard-only    the model heard a name and AIS has no such ship
      ais-confirmed matched, and the ship was there around the time of the call
      ais-stale     matched, but the ship's last fix is old enough that it means little
      ais-matched   matched, age unknown -- every turn stored before 2026-08-20. Silence
                    about the age is not evidence of freshness, so it does not get "confirmed".
    """
    if not live_vessel:
        return None
    if not live_mmsi:
        return "heard-only"
    if age_hours is None:
        return "ais-matched"
    return "ais-confirmed" if age_hours <= confirm_max_age_h else "ais-stale"


def _turn(turn: dict, start=None, confirm_max_age_h: float = LIVE_CONFIRM_DEFAULT_MIN / 60.0) -> dict:
    raw, text = turn.get("raw"), turn.get("text")
    conv = turn.get("conv")
    live_vessel, live_mmsi = turn.get("live_vessel"), turn.get("live_mmsi")
    age_hours = _live_age_hours(start, turn.get("live_seen"))
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
        "live_match": _live_match(live_vessel, live_mmsi, age_hours, confirm_max_age_h),
        # Shown beside a stale match, because "last seen 174 hours before this call" is the
        # whole argument -- the label alone would just look like hedging.
        "live_age_hours": age_hours,
    }


def detail(record: dict, confirm_max_age_h: float = LIVE_CONFIRM_DEFAULT_MIN / 60.0) -> dict:
    out = dict(record)
    out["id"] = conversation_id(record)
    identified = _identified(record)
    out["identified"] = identified
    out["turns"] = [_turn(t, record.get("start"), confirm_max_age_h)
                    for t in (record.get("turns") or [])]
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
