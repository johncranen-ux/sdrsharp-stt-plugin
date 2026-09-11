"""Count how often each arm writes a given word. The prompt measurement's primary metric.

Reference-free by design. 51 of the 136 `heard` lines in the 2026-09-10 corpus are
byte-identical to the machine text -- the operator accepted the pre-fill -- so a
reference-based metric would partly measure the decoder agreeing with itself. Counting
production needs no ground truth at all: the question "does this arm write QNH 27 times or 12
times" is answerable from the arm alone, and 12 is what the operator heard.

Usage:
    py bench_word_production.py air_shipped.json air_both.json
    py bench_word_production.py air_shipped.json --words QNH KLM ILS
"""

import argparse
import re
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVER_DIR))

from bench_flight_identify import load_transcripts  # noqa: E402

# What the operator actually heard across the 136-transmission corpus, for comparison.
EAR_COUNTS = {"QNH": 12, "KLM": 15, "ILS": 8}
DEFAULT_WORDS = ("QNH", "KLM", "ILS")


def count_words(texts: list[str], words: tuple[str, ...]) -> dict[str, int]:
    """Rows containing each word, counted once per row.

    Per row rather than per occurrence because the published corpus figures are per
    transmission; counting repetitions would produce a number that cannot be compared to them.
    """
    patterns = {w: re.compile(rf"\b{re.escape(w)}\b", re.I) for w in words}
    return {w: sum(1 for t in texts if p.search(t or "")) for w, p in patterns.items()}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", nargs="+", help="bench-results JSON files, one per arm")
    ap.add_argument("--words", nargs="+", default=list(DEFAULT_WORDS))
    ap.add_argument("--config", help="which arm inside each file to read")
    args = ap.parse_args()

    words = tuple(args.words)
    print(f"{'arm':28} {'clips':>6} " + " ".join(f"{w:>6}" for w in words))
    for path in args.results:
        texts = list(load_transcripts(Path(path), args.config).values())
        counts = count_words(texts, words)
        print(f"  {Path(path).stem:26} {len(texts):>6} "
              + " ".join(f"{counts[w]:>6}" for w in words))
    print(f"  {'(operator heard)':26} {136:>6} "
          + " ".join(f"{EAR_COUNTS.get(w, '?'):>6}" for w in words))


if __name__ == "__main__":
    main()
