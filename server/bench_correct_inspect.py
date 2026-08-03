#!/usr/bin/env python3
"""Show what a correction contender actually did, clip by clip.

The pooled WER in bench_correct.py says whether a model helped; this says how. It exists
because the decision here is not only "which number is lowest": a pass that invents content
can score well on the clips it leaves alone, and the 2026-07-30 identification work is a
standing reminder that fabrication is the failure this pipeline weighs most heavily.

Usage:
    py bench_correct_inspect.py --contender haiku-4.5 --mode regressions
    py bench_correct_inspect.py --contender nemotron-120b --mode wins --top 5
    py bench_correct_inspect.py --mode invented        # every contender, added content only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench  # noqa: E402

HERE = Path(__file__).resolve().parent


def load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def clips_by_id(results_path: Path, config: str) -> dict[str, dict]:
    data = json.loads(results_path.read_text(encoding="utf-8"))
    return {r["clip_id"]: r for r in data["results"][config]}


def added_words(hypothesis: str, output: str) -> list[str]:
    """Words the correction inserted that were not in the STT output.

    Insertions against the *hypothesis* (not the reference) are what matter here: a word
    that appears in neither is content the model made up rather than recovered.
    """
    ops = bench.word_alignment_ops(hypothesis, output) or []
    return [hyp for op, _ref, hyp in ops if op == "ins" and hyp]


def show(title: str, rows: list[dict], sources: dict[str, dict], limit: int) -> None:
    if not rows:
        print(f"\n{title}: none")
        return
    print(f"\n{title}")
    print("=" * 78)
    for row in rows[:limit]:
        src = sources[row["clip_id"]]
        print(f"\nclip {row['clip_id']}   WER {row['before']:.0%} -> {row['after']:.0%}"
              f"   ({row['delta']:+.0%})")
        print(f"  ref : {src['reference'].strip()}")
        print(f"  stt : {src['text'].strip()}")
        print(f"  out : {row['output'].strip()}")
        extra = added_words(src["text"], row["output"])
        if extra:
            print(f"  ADDED: {' '.join(extra)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", default=str(HERE / "bench-correct-results.json"))
    parser.add_argument("--source", default=str(HERE / "bench-results-groq.json"))
    parser.add_argument("--config", default="groq_prompt")
    parser.add_argument("--contender", default=None, help="default: every contender in the file")
    parser.add_argument("--mode", choices=["wins", "regressions", "invented", "all"],
                        default="regressions")
    parser.add_argument("--top", type=int, default=8)
    args = parser.parse_args()

    data = load(Path(args.results))
    sources = clips_by_id(Path(args.source), args.config)
    names = [args.contender] if args.contender else list(data["contenders"])

    for name in names:
        summary = data["contenders"].get(name)
        if not summary:
            print(f"no such contender: {name}", file=sys.stderr)
            return 2
        per_clip = summary["per_clip"]
        print(f"\n\n######## {name}  ({summary['label']})  "
              f"pooled WER {summary['pooled_wer']:.1%}  "
              f"+{summary['improved']}/-{summary['regressed']}")

        if args.mode in ("regressions", "all"):
            worse = sorted((p for p in per_clip if p["after"] > p["before"] + 1e-9),
                           key=lambda p: -p["delta"])
            show("REGRESSIONS (correction made it worse)", worse, sources, args.top)

        if args.mode in ("wins", "all"):
            better = sorted((p for p in per_clip if p["after"] < p["before"] - 1e-9),
                            key=lambda p: p["delta"])
            show("WINS", better, sources, args.top)

        if args.mode in ("invented", "all"):
            invented = []
            for p in per_clip:
                extra = added_words(sources[p["clip_id"]]["text"], p["output"])
                if extra:
                    invented.append((len(extra), p))
            invented.sort(key=lambda t: -t[0])
            show("INVENTED CONTENT (words inserted into the transcript)",
                 [p for _n, p in invented], sources, args.top)
            print(f"\n  clips with any inserted word: {len(invented)}/{len(per_clip)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
