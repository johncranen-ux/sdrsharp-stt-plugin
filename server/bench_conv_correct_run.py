"""Run the conversation-correction pass offline over a stored conversations.json.

correct_conversation() has only ever been called from the live pipeline
(conversations.py::_resolve_window), on transmissions as the resolver hands them over --
never on what actually ended up on disk. bench_conversation_correct.py can score a "conv" key
if one is already on a stored turn, but nothing produces that key outside the live server. So
the bake-off this branch exists to run cannot be run: there is no way to try a prompt or model
change against a captured conversation without waiting for it to happen live again.

This tool closes that gap. It reads a conversations.json, re-runs correct_conversation() over
each stored exchange exactly as _resolve_window does, and writes an ANNOTATED COPY with
"conv"/"changes" added to the turns that were corrected -- the same shape _store_resolved
already produces live, so bench_conversation_correct.py can score the result unmodified. The
input file is never touched, so the same capture can be re-annotated under different prompts
or models and compared.

Usage:
    py bench_conv_correct_run.py --conversations stt_proxy/conversations.json \\
        --out annotated-2026-08-10.json --day 2026-08-07

    py bench_conversation_correct.py --conversations annotated-2026-08-10.json \\
        --references references-2026-08-07-verified.txt --captures ...
"""

import argparse
import copy
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stt_proxy import conversation_correct


def _correction_turns(stored_turns: list[dict]) -> list[dict]:
    """Stored turns -> the {"id", "corrected"} shape correct_conversation()/validate_reply()
    require.

    A stored turn (written by _store_resolved) has "time"/"text"/"raw"/"live_vessel" and no
    "id" at all -- ids only exist during live resolution, scoped to one journal window, and
    are never persisted. validate_reply reads a turn's original text as
    t.get("corrected") or t.get("text"), so the stored "text" (already the best-known text at
    storage time) is handed over as "corrected". Ids are minted fresh here, 1-based and
    call-local: they only need to be unique within this one call, and the caller maps the
    reply back to stored turns by that same position.
    """
    return [{"id": i, "corrected": t.get("text", "")} for i, t in enumerate(stored_turns, start=1)]


def annotate(rows: list[dict], correct=None) -> tuple[list[dict], dict]:
    """Run `correct` over every stored exchange in `rows`, returning an annotated deep copy.

    `correct` is looked up on the module at call time rather than bound as a literal default,
    so a test can monkeypatch conversation_correct.correct_conversation and have a bare
    annotate(rows) call pick up the fake -- without annotate() itself needing a network mock
    threaded through it.
    """
    correct = correct or conversation_correct.correct_conversation
    rows = copy.deepcopy(rows)

    stats = {"exchanges": 0, "corrected_exchanges": 0, "corrected_turns": 0, "failed": 0}

    for row in rows:
        stored_turns = row.get("turns") or []
        stats["exchanges"] += 1
        if not stored_turns:
            continue

        call_turns = _correction_turns(stored_turns)
        try:
            fixed = correct(call_turns, row.get("vessel"))
        except Exception:
            # correct_conversation's own contract is "every failure returns None"; this except
            # is a second line of defence so a fake or a future bug in it cannot take the
            # whole run down over one bad exchange.
            fixed = None

        if not fixed:
            stats["failed"] += 1
            continue

        exchange_corrected = False
        for call_id, result in fixed.items():
            # call_id is 1-based into stored_turns, matching _correction_turns' enumeration.
            idx = call_id - 1
            if not (0 <= idx < len(stored_turns)):
                continue
            # Same rule _store_resolved uses: a fix is only written when it declared a
            # non-empty "changes" list, so "not corrected" stays distinguishable on disk from
            # "corrected to the same thing".
            if not result.get("changes"):
                continue
            stored_turns[idx]["conv"] = result.get("text")
            stored_turns[idx]["changes"] = result.get("changes")
            stats["corrected_turns"] += 1
            exchange_corrected = True

        if exchange_corrected:
            stats["corrected_exchanges"] += 1

    return rows, stats


def _matches_days(row: dict, days: set[str]) -> bool:
    return (row.get("start") or "")[:10] in days


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversations", required=True, help="stored conversations.json to read")
    ap.add_argument("--out", required=True, help="where the annotated copy is written")
    ap.add_argument("--day", action="append", metavar="YYYY-MM-DD",
                    help="only annotate exchanges from this day; repeatable")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="cap on exchanges processed, applied AFTER --day filtering -- a cost "
                         "control, so it must not spend budget on days --day was meant to exclude")
    args = ap.parse_args(argv)

    rows = json.loads(Path(args.conversations).read_text(encoding="utf-8"))

    if args.day:
        days = set(args.day)
        rows = [row for row in rows if _matches_days(row, days)]
    if args.limit:
        rows = rows[:args.limit]

    new_rows, stats = annotate(rows)

    Path(args.out).write_text(json.dumps(new_rows, indent=1), encoding="utf-8")

    print(f"exchanges           {stats['exchanges']}")
    print(f"corrected_exchanges {stats['corrected_exchanges']}")
    print(f"corrected_turns     {stats['corrected_turns']}")
    print(f"failed              {stats['failed']}")
    print(f"wrote {stats['exchanges']} exchange(s) to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
