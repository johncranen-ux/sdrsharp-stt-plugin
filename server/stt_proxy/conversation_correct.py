"""Correct each transmission using the rest of its conversation.

Runs after resolve_conversation, which has already decided who was speaking, and before
storage. Where the per-transmission pass sees one transmission, this one sees the exchange --
so a garbled opening call can be repaired from the shore station's clean answer in the very
next turn, which is information nothing in the system used before.

Every failure returns None and the conversation is stored uncorrected. A conversation is never
lost or half-rewritten because a model misbehaved.
"""


class CorrectionRejected(Exception):
    """The reply did not honour the contract, so none of it is used."""


def validate_reply(payload: dict, turns: list[dict]) -> dict[int, dict]:
    """{turn_id: {"text", "changes"}}, or raise.

    All-or-nothing on purpose. A reply that got one turn wrong has demonstrated it is not
    following the contract, and picking the good parts out of it is how a half-corrected
    conversation reaches the page.
    """
    rows = payload.get("turns") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or not rows:
        raise CorrectionRejected("reply has no turns")

    original = {t["id"]: (t.get("corrected") or t.get("text") or "") for t in turns}
    seen: dict[int, dict] = {}

    for row in rows:
        if not isinstance(row, dict):
            raise CorrectionRejected(f"turn entry is not an object: {row!r}")
        turn_id = row.get("id")
        if turn_id not in original:
            raise CorrectionRejected(f"unknown id {turn_id!r}")
        if turn_id in seen:
            raise CorrectionRejected(f"id {turn_id!r} appears twice")

        text = row.get("text")
        if not isinstance(text, str) or not text.strip():
            raise CorrectionRejected(f"id {turn_id!r} has empty text")

        changes = row.get("changes")
        if not isinstance(changes, list):
            raise CorrectionRejected(f"id {turn_id!r} has no changes list")

        unchanged = text.strip() == original[turn_id].strip()
        if unchanged and changes:
            raise CorrectionRejected(f"id {turn_id!r} declared a change it did not make")
        if not unchanged and not changes:
            raise CorrectionRejected(f"id {turn_id!r} is an undeclared rewrite")

        seen[turn_id] = {"text": text.strip(), "changes": changes}

    missing = sorted(set(original) - set(seen))
    if missing:
        raise CorrectionRejected(f"missing turns: {missing}")
    return seen
