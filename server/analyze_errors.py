"""Aggregates recurring word-substitution patterns from a bench.py JSON results file,
to find real, evidence-backed candidates for whisper-proxy.py's _apply_sttt_corrections
list -- rather than guessing at nautical-term correction rules.

Usage:
    py analyze_errors.py bench-results.json [--top 40]
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import bench


def aggregate_substitutions(results: dict) -> list[tuple[tuple[str, str], int]]:
    """Returns [((hyp_word, ref_word), count), ...] sorted by count desc, then
    alphabetically. hyp_word first: that's what Whisper actually produced, the thing a
    correction regex matches on; ref_word is what it should have said.
    """
    counter: Counter[tuple[str, str]] = Counter()
    for rows in results.values():
        for row in rows:
            reference = row.get("reference")
            if not reference:
                continue
            ops = bench.word_alignment_ops(reference, row.get("text", ""))
            if ops is None:
                continue
            for op, ref_word, hyp_word in ops:
                if op == "sub":
                    counter[(hyp_word, ref_word)] += 1
    return sorted(counter.items(), key=lambda item: (-item[1], item[0]))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", help="Path to a bench.py --out-json results file")
    parser.add_argument("--top", type=int, default=40, help="How many rows to print")
    args = parser.parse_args()

    payload = json.loads(Path(args.json_path).read_text(encoding="utf-8"))
    counts = aggregate_substitutions(payload["results"])

    print(f"{'hyp -> ref':<40}{'count':>6}")
    print("-" * 46)
    for (hyp_word, ref_word), count in counts[: args.top]:
        print(f"{hyp_word:<18} -> {ref_word:<18}{count:>6}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
