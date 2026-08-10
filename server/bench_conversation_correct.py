"""Score the conversation-correction pass: WER, invented content, and what could not be joined.

Three numbers, not one. WER alone cannot see this feature's central risk, which is a fluent
wrong answer -- a readback confidently rewritten into agreement scores well and is exactly the
failure the operator must not be handed silently.

Usage:
    py bench_conversation_correct.py --conversations stt_proxy/conversations.json \\
        --references references-2026-08-07-verified.txt \\
        --captures "D:/SDR/SdrSharp/Plugins/SttPlugin/captures/2026-08-07"
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench
from clip_index import clip_for_time, load_clip_index

_TS_FMT = "%Y-%m-%d %H:%M:%S"


def wer_counts(reference: list[str], hypothesis: list[str]) -> tuple[int, int]:
    """(edit distance, reference length) over word tokens."""
    n, m = len(reference), len(hypothesis)
    previous = list(range(m + 1))
    for i in range(1, n + 1):
        current = [i] + [0] * m
        for j in range(1, m + 1):
            current[j] = min(previous[j] + 1, current[j - 1] + 1,
                             previous[j - 1] + (reference[i - 1] != hypothesis[j - 1]))
        previous = current
    return previous[m], n


def _turn_time(row: dict, turn: dict) -> datetime.datetime | None:
    try:
        day = datetime.datetime.strptime(row.get("start", ""), _TS_FMT).date()
        clock = datetime.datetime.strptime(turn.get("time", ""), "%H:%M:%S").time()
    except ValueError:
        return None
    return datetime.datetime.combine(day, clock)


def score_turns(rows: list[dict], references: dict[str, str], index: dict,
                use_conv: bool) -> dict:
    """Pooled WER plus invented-word count for one arm."""
    errors = ref_words = scored = unmatched = invented = 0

    for row in rows:
        for turn in row.get("turns", []):
            when = _turn_time(row, turn)
            clip = clip_for_time(index, when) if when else None
            if clip is None or clip not in references:
                unmatched += 1
                continue
            text = (turn.get("conv") or turn.get("text", "")) if use_conv else turn.get("text", "")
            reference = bench._normalize(references[clip])
            hypothesis = bench._normalize(text)
            e, n = wer_counts(reference, hypothesis)
            errors += e
            ref_words += n
            scored += 1
            # Words present in the hypothesis and absent from the reference. Crude, and
            # deliberately so: it over-counts a correct synonym and that is the safe
            # direction for a risk metric.
            invented += sum(1 for w in hypothesis if w not in set(reference))

    return {"wer": (100.0 * errors / ref_words) if ref_words else 0.0,
            "errors": errors, "ref_words": ref_words, "scored": scored,
            "unmatched": unmatched, "invented": invented}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversations", required=True)
    ap.add_argument("--references", required=True)
    ap.add_argument("--captures", required=True, help="one capture DAY directory")
    args = ap.parse_args(argv)

    rows = json.loads(Path(args.conversations).read_text(encoding="utf-8"))
    references = bench.load_references(Path(args.references))
    index = load_clip_index(args.captures)
    if not index:
        print(f"no index.jsonl under {args.captures}", file=sys.stderr)
        return 1

    print(f"{'arm':>12} {'WER':>8} {'errors':>8} {'scored':>8} {'unmatched':>10} {'invented':>9}")
    print("-" * 60)
    for label, use_conv in (("baseline", False), ("corrected", True)):
        s = score_turns(rows, references, index, use_conv)
        print(f"{label:>12} {s['wer']:>7.2f}% {s['errors']:>8} {s['scored']:>8} "
              f"{s['unmatched']:>10} {s['invented']:>9}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
