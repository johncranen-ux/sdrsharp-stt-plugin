"""Correct each transmission using the rest of its conversation.

Runs after resolve_conversation, which has already decided who was speaking, and before
storage. Where the per-transmission pass sees one transmission, this one sees the exchange --
so a garbled opening call can be repaired from the shore station's clean answer in the very
next turn, which is information nothing in the system used before.

Every failure returns None and the conversation is stored uncorrected. A conversation is never
lost or half-rewritten because a model misbehaved.
"""

import json
import os

from stt_proxy import fewshot, llm

CONVERSATION_CORRECT = os.environ.get("CONVERSATION_CORRECT", "off").strip().lower() == "on"
CONVERSATION_CORRECT_PROVIDER = os.environ.get("CONVERSATION_CORRECT_PROVIDER", "anthropic").strip()
CONVERSATION_CORRECT_MODEL = os.environ.get("CONVERSATION_CORRECT_MODEL",
                                            "claude-haiku-4-5-20251001").strip()
CONVERSATION_CORRECT_FEWSHOT = os.environ.get("CONVERSATION_CORRECT_FEWSHOT", "on").strip().lower() != "off"
CONVERSATION_CORRECT_TIMEOUT_S = float(os.environ.get("CONVERSATION_CORRECT_TIMEOUT_S", "60"))

_failures_logged = 0
_FAILURE_LOG_LIMIT = 3


SYSTEM_PROMPT = """\
You are given the transmissions of ONE VHF radio exchange near Rotterdam (Maas Approach /
Rotterdam VTS), in time order, already transcribed, together with the vessel that has already
been identified for this exchange.

Correct the transcription of each turn using the rest of the exchange. You are NOT identifying
anybody -- that is decided already. You are NOT improving anyone's English.

Return ONLY raw JSON, no markdown:
{"turns": [{"id": <id>, "text": "<corrected>",
            "changes": [{"from": "<original>", "to": "<replacement>", "reason": "<short>"}]}]}

Contract:
- Every id you were given appears exactly once. Never invent an id.
- If you change nothing in a turn, return its text byte-identical and "changes": [].
- If you change anything, every substitution must appear in "changes". An undeclared
  rewrite is rejected and the whole reply is discarded.

Rules:
1. The shore station's rendition wins. For a vessel name or a type word, prefer the shore
   station's version over the vessel's own opening call: the station reads it off a screen,
   while the opening call is the noisiest turn on the channel. "motor vision" answered by
   "Motorvessel" means the opening call said Motorvessel.
2. Propagate the identified vessel's name into turns that name the vessel -- but ONLY where a
   name was actually spoken. A turn that named nobody must still name nobody. Never add a name
   to a turn that did not have one.
3. Align a readback ONLY when it is garbled. A readback that is clean but different is a real
   disagreement -- a vessel getting it wrong -- and the operator needs to see it. Never edit a
   clean readback into agreement.
4. Numbers spoken digit by digit ("one three zero zero") survive the channel well and stay
   exactly as transcribed. Repair a digit only when the same value appears cleanly elsewhere
   in this exchange. Never reformat in either direction: not "one three zero zero" into
   "1300", not "4.7" into "four point seven".
5. Make the smallest edit that fixes a clear error. If a word is merely unusual, or you are
   unsure what was meant, leave it exactly as it is.
6. Never remove content. Every utterance must survive into the corrected text, even if it is
   garbled, redundant or a filler.
7. Keep the speaker's own words, word order, grammar and disfluencies.
8. Examples, when given, demonstrate the style of correction. They are NOT a list of ships
   that might be speaking. Never take a vessel name from an example.
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


def render_input(turns: list[dict], vessel: str | None) -> str:
    lines = [f"[VESSEL] {vessel or 'unidentified'}", "", "[TRANSMISSIONS]"]
    for turn in turns:
        text = turn.get("corrected") or turn.get("text") or ""
        lines.append(f"  {turn['id']}. {text}")
    return "\n".join(lines)


def _log_failure(reason: str) -> None:
    """Rate-limited, but never silent: a prompt that has started failing on every call must
    be visible. Same shape as _report_unrecognised_frame."""
    global _failures_logged
    if _failures_logged < _FAILURE_LOG_LIMIT:
        _failures_logged += 1
        suffix = " (further failures suppressed)" if _failures_logged == _FAILURE_LOG_LIMIT else ""
        print(f"  [conv-correct] not applied: {reason}{suffix}", flush=True)


def correct_conversation(turns: list[dict], vessel: str | None) -> dict[int, dict] | None:
    """{turn_id: {"text", "changes"}} for one exchange, or None if it could not be corrected."""
    if not turns:
        return None

    system = SYSTEM_PROMPT
    if CONVERSATION_CORRECT_FEWSHOT:
        try:
            rendered = fewshot.render_examples(fewshot.load_examples())
            if rendered:
                system = f"{system}\n\n{rendered}\n"
        except Exception as exc:
            # Failing to render examples must degrade to running without examples, not to returning None.
            _log_failure(f"examples file: {exc}")

    try:
        reply = llm.complete(
            system, render_input(turns, vessel),
            provider=CONVERSATION_CORRECT_PROVIDER,
            model=CONVERSATION_CORRECT_MODEL,
            temperature=0,
            timeout_s=CONVERSATION_CORRECT_TIMEOUT_S,
        )
        payload = json.loads(llm.strip_code_fence(reply))
    except (llm.LLMError, ValueError) as exc:
        _log_failure(str(exc))
        return None

    try:
        return validate_reply(payload, turns)
    except CorrectionRejected as exc:
        _log_failure(str(exc))
        return None
