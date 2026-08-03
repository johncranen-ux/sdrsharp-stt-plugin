#!/usr/bin/env python3
"""Cut a bench-results JSON down to a clip-id range.

references-2026-07-28.txt is only hand-verified as far as clip 0099; past that it is still
the draft pre-fill, which is whisper.cpp output *after* the regex correction pass. Scoring
against that would be circular -- the regex contender would be graded against its own
output, and every LLM penalised for departing from Whisper's wording -- so the unverified
tail is removed rather than left to quietly inflate the result.

Usage:
    py bench_filter.py --in bench-results-0728.json --out bench-results-0728-verified.json \
                       --max-id 0099
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="src", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--min-id", default="0000")
    parser.add_argument("--max-id", required=True)
    args = parser.parse_args()

    data = json.loads(Path(args.src).read_text(encoding="utf-8"))
    kept_total = dropped_total = 0

    for config, rows in data["results"].items():
        kept = [r for r in rows if args.min_id <= r["clip_id"] <= args.max_id]
        dropped_total += len(rows) - len(kept)
        kept_total += len(kept)
        data["results"][config] = kept

    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    scored = sum(1 for rows in data["results"].values() for r in rows
                 if (r.get("reference") or "").strip())
    print(f"kept {kept_total} clips ({scored} with a reference), dropped {dropped_total} "
          f"outside {args.min_id}..{args.max_id}")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
