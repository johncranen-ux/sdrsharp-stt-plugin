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

# Measured twice on 2026-08-10 over the 34 exchanges of the verified 08-07 corpus, Haiku,
# scored by bench_conversation_correct.py. Round 2 is the current prompt; round 1 is kept
# because the gap between them is the whole lesson.
#
#                              WER     errors  invented  turns rewritten
#   baseline                 19.06%      211      167      -
#   r1 no examples           18.34%      203      166      -
#   r1 examples              17.71%      196      158     42
#   r2 no examples           17.98%      199      157      -
#   r2 examples              17.25%      191      151     36
#
# ROUND 1 LOOKED LIKE A WIN AND WAS NOT. On the BERGE TOWNSEND exchange (10:17:50), whose
# stored resolution was VISION at medium confidence and therefore wrong, the pass rewrote the
# shore station's "We have the Townsend" into "We have the Vision" and turned "Bergy Township"
# -- BERGE TOWNSEND misheard, the best surviving evidence of the real ship -- into "Berkey
# Fountain". It spread a resolver error across every turn and deleted the evidence against it.
# WER fell and invented content fell while that happened. Neither metric can see this class of
# failure; only reading the changes could. Worth remembering the next time a harness reports an
# improvement.
#
# The cause was a contradiction in the prompt rather than a defect in the code: rule 1 makes
# the shore station's rendition authoritative, rule 2 propagated the resolved name, and nothing
# said which governs when they disagree -- though that disagreement is exactly the signal that
# the identification is wrong. Rule 2 is now bounded (repair a garbled rendering of the SAME
# name only; never replace a different one; rule 1 outranks it) and the prompt states that the
# identification comes from a fallible pass.
#
# ROUND 2 KEEPS "We have the Townsend" INTACT, rewrites 36 turns instead of 42, and improves
# every number anyway. Being more conservative cost nothing.
#
# REPEATED 3x PER ARM, 2026-08-10, because one run cannot separate a gain from sampling noise:
#
#   arm             WER mean   range          spread   invented mean
#   baseline         19.06%    pinned (6/6)    0.00     167
#   no examples      17.86%    17.71-18.07     0.36     156.3
#   examples         17.07%    16.98-17.16     0.18     150.3
#
# The gain is 1.99 points against a within-arm spread of 0.18 -- roughly eleven times the
# noise, so it is real. The baseline is identical in all six runs, which is what says the
# harness itself is deterministic and the comparison means anything.
#
# Examples are worth 0.79 points, larger than either arm's spread and larger than both summed.
# That was not the expected result: the examples in question are the two SYNTHETIC ones in
# fewshot.py, so curated examples drawn from real corrected exchanges are probably worth more.
#
# Calibration note: the ~1-point run-to-run noise recorded for the 2026-08-03 correction
# bake-off does NOT apply here. That figure came from whole-pipeline STT correction with one
# arm accidentally running at temperature 1.0. This pass runs at temperature 0 and only edits
# existing text, and its measured spread is 0.18-0.36. Judging it against the old bar would
# have thrown away a real effect.
#
# The bound on rule 2 holds in all three repeats: "We have the Townsend" is kept every time.
#
# Residual, and the reason this is a judgement rather than an automatic yes: the pass still
# writes "Motorvessel Vision" into two turns of that exchange. The identification was wrong and
# the pass is faithfully repairing a garbled rendering of the name it was given, so this is the
# resolver's error propagating rather than a fault here -- but it is still a wrong name on the
# page. Expected to resolve when the `motor vision` -> Motorvessel correction merges from
# feat/local-ais-receiver, since the phrase is a garbled TYPE WORD rather than a name.
# ON by default as of 2026-08-10, on the evidence above. CONVERSATION_CORRECT=off restores the
# previous behaviour exactly: the pass never runs, no `conv` key is stored, and the page renders
# the verbatim text with the wording it had before this feature existed.
CONVERSATION_CORRECT = os.environ.get("CONVERSATION_CORRECT", "on").strip().lower() != "off"
CONVERSATION_CORRECT_PROVIDER = os.environ.get("CONVERSATION_CORRECT_PROVIDER", "anthropic").strip()
CONVERSATION_CORRECT_MODEL = os.environ.get("CONVERSATION_CORRECT_MODEL",
                                            "claude-haiku-4-5-20251001").strip()
CONVERSATION_CORRECT_FEWSHOT = os.environ.get("CONVERSATION_CORRECT_FEWSHOT", "on").strip().lower() != "off"
_TIMEOUT_DEFAULT_S = 60.0
try:
    # Parsed defensively: this runs at import time regardless of the flag, so a malformed
    # value here must not crash proxy startup and break the default-off promise.
    CONVERSATION_CORRECT_TIMEOUT_S = float(
        os.environ.get("CONVERSATION_CORRECT_TIMEOUT_S", str(_TIMEOUT_DEFAULT_S)))
except ValueError:
    CONVERSATION_CORRECT_TIMEOUT_S = _TIMEOUT_DEFAULT_S

_failure_count = 0
_FAILURE_LOG_LIMIT = 3
_FAILURE_LOG_PERIOD = 200


SYSTEM_PROMPT = """\
You are given the transmissions of ONE VHF radio exchange near Rotterdam (Maas Approach /
Rotterdam VTS), in time order, already transcribed, together with the vessel that a separate
identification pass has already resolved for this exchange. That pass is sometimes wrong, so
treat its answer as a strong hint, not as ground truth.

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
2. Use the identified vessel's name only to repair a garbled rendering of that same name --
   never to replace a different name. A turn that named nobody must still name nobody: never
   add a name to a turn that did not have one. If a turn plainly says a different name from
   the identified vessel, leave it exactly as spoken and declare no change for it -- that
   disagreement is evidence the identification is wrong, not something to smooth away.
   Rule 1 outranks this rule -- a name the shore station actually said is never overwritten.
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
        # bool is a subclass of int (True == 1, same hash), so it must be excluded explicitly
        # or it silently aliases a real id instead of being rejected on its own. Checked before
        # any dict lookup: an unhashable id (e.g. a list) raises TypeError as a dict key, which
        # correct_conversation does not catch, and that TypeError previously escaped through
        # _resolve_window and lost the rest of the reaper's batch.
        if not isinstance(turn_id, int) or isinstance(turn_id, bool):
            raise CorrectionRejected(f"id is not an integer: {turn_id!r}")
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
        for change in changes:
            if not isinstance(change, dict) or not isinstance(change.get("from"), str) \
                    or not isinstance(change.get("to"), str):
                raise CorrectionRejected(f"id {turn_id!r} has a malformed changes entry: {change!r}")

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
    be visible. Same shape as _report_unrecognised_frame.

    The counter is process-lifetime and shared between the examples-render path and the LLM
    path, so a permanent cutoff after the first few failures would go silent forever the moment
    a systematic fault starts -- the same failure-by-going-quiet class as a dead feed.
    Suppression is periodic instead: log the first few, then one in every
    _FAILURE_LOG_PERIOD after that, so a standing fault stays visible without spamming the
    console on every call. Plain global increment, no lock: an occasional missed or double
    count on this counter only shifts which call logs, it never suppresses the fault entirely.
    """
    global _failure_count
    _failure_count += 1
    n = _failure_count
    due = n <= _FAILURE_LOG_LIMIT or (n - _FAILURE_LOG_LIMIT) % _FAILURE_LOG_PERIOD == 0
    if due:
        suffix = " (further failures suppressed)" if n == _FAILURE_LOG_LIMIT else ""
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
