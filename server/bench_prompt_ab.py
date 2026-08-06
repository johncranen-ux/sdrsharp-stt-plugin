#!/usr/bin/env python3
"""Compare two or more bench-results files clip-by-clip and say whether the difference is real.

Written for the shipped-vs-legacy prompt A/B, but nothing here is prompt-specific: it takes
any results files bench.py or bench_stt.py produced over the same captures directory.

Two things it does that eyeballing two pooled WERs does not:

* **Pairs on clip_id.** Only clips that carry a reference *and* transcribed cleanly in every
  arm are scored, so one arm losing a chunk to a 429 cannot flatter the other. Which clips
  were dropped, and why, is printed rather than absorbed.
* **Bootstraps a confidence interval on the difference.** identify.py already records ~1
  point of pooled-WER movement between byte-identical runs, so a bare delta of a point or
  two carries no information on its own. Resampling clips (with replacement, keeping arms
  paired) says how much of the observed gap survives the sampling noise of *this* clip set.

Usage:
    py bench_prompt_ab.py shipped=ab-shipped.json legacy=ab-legacy.json
    py bench_prompt_ab.py shipped=a.json legacy=b.json repeat=c.json --baseline shipped
"""
from __future__ import annotations

import argparse
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench                          # noqa: E402
from stt_proxy import corrections     # noqa: E402


def load_arm(spec: str) -> tuple[str, dict[str, dict]]:
    """"label=path" -> (label, {clip_id: row}). Config key inside the file is ignored;
    these files hold one config each, and pairing is on clip_id."""
    if "=" not in spec:
        raise SystemExit(f"error: expected label=path, got {spec!r}")
    label, path = spec.split("=", 1)
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rows: dict[str, dict] = {}
    for config_rows in payload.get("results", {}).values():
        for row in config_rows:
            # Carried per-row so --echo-filter can apply each arm's OWN prompt. An echo is
            # only an echo of the prompt that produced it, and the arms differ by prompt.
            row["_arm_prompt"] = payload.get("prompt", "")
            rows[row["clip_id"]] = row
    if not rows:
        raise SystemExit(f"error: no rows in {path}")
    return label, rows


def apply_echo_filter(arms: dict[str, dict[str, dict]]) -> dict[str, list[str]]:
    """Blank whatever production's prompt-echo filter would have suppressed.

    bench.py deliberately measures the raw decoder, which is right for decoder settings but
    wrong for a prompt comparison: the echo filter is downstream of the prompt and keyed to
    it, so leaving it out charges an arm for text no user ever sees. It also charges arms
    unequally -- a prompt that echoes more is penalised twice, once in the text it emits and
    once in the WER of text production discards.
    """
    suppressed: dict[str, list[str]] = {}
    for label, rows in arms.items():
        hits = []
        for clip_id, row in rows.items():
            if corrections._is_prompt_echo(row.get("text") or "", row.get("_arm_prompt") or ""):
                row["text"] = ""
                hits.append(clip_id)
        suppressed[label] = sorted(hits)
    return suppressed


def pooled_wer(clip_ids: list[str], rows: dict[str, dict]) -> tuple[float, int, int]:
    edits = words = 0
    for clip_id in clip_ids:
        row = rows[clip_id]
        counts = bench.word_error_counts(row.get("reference") or "", row.get("text") or "")
        if counts:
            edits += counts[0]
            words += counts[1]
    return (edits / words if words else 0.0), edits, words


def _clip_counts(clip_ids: list[str], rows: dict[str, dict]) -> dict[str, tuple[int, int]]:
    out = {}
    for clip_id in clip_ids:
        row = rows[clip_id]
        counts = bench.word_error_counts(row.get("reference") or "", row.get("text") or "")
        if counts:
            out[clip_id] = counts
    return out


def bootstrap_ci(clip_ids: list[str], a: dict[str, dict], b: dict[str, dict],
                 iterations: int, seed: int) -> tuple[float, float]:
    """95% CI on (pooled WER of b) - (pooled WER of a), resampling clips paired."""
    counts_a = _clip_counts(clip_ids, a)
    counts_b = _clip_counts(clip_ids, b)
    shared = [c for c in clip_ids if c in counts_a and c in counts_b]
    rng = random.Random(seed)
    deltas = []
    for _ in range(iterations):
        ea = wa = eb = wb = 0
        for _ in range(len(shared)):
            clip_id = shared[rng.randrange(len(shared))]
            ea += counts_a[clip_id][0]
            wa += counts_a[clip_id][1]
            eb += counts_b[clip_id][0]
            wb += counts_b[clip_id][1]
        if wa and wb:
            deltas.append(eb / wb - ea / wa)
    deltas.sort()
    if not deltas:
        return (0.0, 0.0)
    return deltas[int(0.025 * len(deltas))], deltas[int(0.975 * (len(deltas) - 1))]


def select_scored(arms: dict[str, dict[str, dict]]) -> tuple[list[str], list[str], list[str]]:
    """Split the clips common to every arm into (scored, no_reference, errored).

    A clip is only scored when every arm produced a clean transcription of it. Scoring a
    clip that errored in one arm would charge that arm the full reference length in edits
    -- an API hiccup would then read as a decoding difference, which is exactly the
    conclusion this tool exists to protect.
    """
    if not arms:
        return [], [], []
    common = set.intersection(*(set(rows) for rows in arms.values()))
    scored, no_ref, errored = [], [], []
    for clip_id in sorted(common):
        if not any((arms[a][clip_id].get("reference") or "").strip() for a in arms):
            no_ref.append(clip_id)
        elif any(arms[a][clip_id].get("error") for a in arms):
            errored.append(clip_id)
        else:
            scored.append(clip_id)
    return scored, no_ref, errored


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("arms", nargs="+", help="label=path/to/bench-results.json")
    parser.add_argument("--baseline", default=None,
                        help="arm every other arm is compared against (default: the first)")
    parser.add_argument("--bootstrap", type=int, default=5000)
    parser.add_argument("--seed", type=int, default=20260806)
    parser.add_argument("--movers", type=int, default=12,
                        help="how many largest per-clip changes to print")
    parser.add_argument("--echo-filter", action="store_true",
                        help="blank output the production prompt-echo filter would suppress, "
                             "using each arm's own prompt -- i.e. score the deployed path "
                             "rather than the raw decoder")
    parser.add_argument("--references", default=None,
                        help="re-score against this references file instead of the ones embedded "
                             "in the results. Corrected ground truth costs nothing to apply -- the "
                             "transcripts are already stored, so only the scoring needs redoing.")
    args = parser.parse_args()

    arms = dict(load_arm(spec) for spec in args.arms)
    if args.references:
        refs = bench.load_references(Path(args.references))
        changed = 0
        for rows in arms.values():
            for clip_id, row in rows.items():
                if clip_id in refs and (row.get("reference") or "") != refs[clip_id]:
                    row["reference"] = refs[clip_id]
                    changed += 1
        print(f"re-scored against {args.references}: "
              f"{changed // max(len(arms), 1)} references differ from the stored ones\n")

    if args.echo_filter:
        for label, hits in apply_echo_filter(arms).items():
            print(f"echo filter suppressed {len(hits)} clip(s) in {label}: {', '.join(hits) or '-'}")
        print()
    if len(arms) < 2:
        raise SystemExit("error: need at least two arms")
    baseline = args.baseline or list(arms)[0]
    if baseline not in arms:
        raise SystemExit(f"error: --baseline {baseline!r} is not one of {list(arms)}")

    scored, no_ref, errored = select_scored(arms)

    print(f"arms: {', '.join(arms)}   baseline: {baseline}")
    for label, rows in arms.items():
        print(f"  {label:<10} {len(rows)} clips from file")
    print(f"\nscored on {len(scored)} clips common to all arms "
          f"({len(no_ref)} without a reference, {len(errored)} with an error in some arm)")
    for clip_id in errored:
        which = [f"{a}: {arms[a][clip_id]['error']}" for a in arms if arms[a][clip_id].get("error")]
        print(f"    dropped {clip_id} -- {'; '.join(which)[:110]}")
    if not scored:
        raise SystemExit("error: no clips scoreable in every arm")

    base_wer, base_edits, base_words = pooled_wer(scored, arms[baseline])
    print(f"\n{'arm':<12}{'pooled WER':>11}{'delta':>9}{'95% CI on delta':>24}")
    print("-" * 56)
    print(f"{baseline:<12}{base_wer:>10.1%}{'--':>9}{'(baseline)':>24}")
    for label, rows in arms.items():
        if label == baseline:
            continue
        wer, _, _ = pooled_wer(scored, rows)
        lo, hi = bootstrap_ci(scored, arms[baseline], rows, args.bootstrap, args.seed)
        print(f"{label:<12}{wer:>10.1%}{wer - base_wer:>+8.1%}"
              f"{f'[{lo:+.1%}, {hi:+.1%}]':>24}")
    print(f"\n({base_edits} edits over {base_words} reference words in the baseline arm; "
          f"CIs from {args.bootstrap} paired bootstrap resamples, seed {args.seed})")
    print("A CI spanning zero means this clip set cannot tell the two apart.")

    # Per-clip movers, baseline vs each other arm.
    for label, rows in arms.items():
        if label == baseline:
            continue
        changes = []
        for clip_id in scored:
            ca = bench.word_error_counts(arms[baseline][clip_id].get("reference") or "",
                                         arms[baseline][clip_id].get("text") or "")
            cb = bench.word_error_counts(rows[clip_id].get("reference") or "",
                                         rows[clip_id].get("text") or "")
            if ca and cb:
                changes.append((cb[0] - ca[0], clip_id))
        better = sum(1 for d, _ in changes if d < 0)
        worse  = sum(1 for d, _ in changes if d > 0)
        print(f"\n-- {label} vs {baseline}: {better} clips better, {worse} worse, "
              f"{len(changes) - better - worse} unchanged --")
        changes.sort(key=lambda t: -abs(t[0]))
        for delta, clip_id in changes[:args.movers]:
            if delta == 0:
                break
            print(f"  {delta:+3d} edits  {clip_id}")
            print(f"      ref      {(arms[baseline][clip_id].get('reference') or '')[:96]}")
            print(f"      {baseline:<8} {(arms[baseline][clip_id].get('text') or '')[:96]}")
            print(f"      {label:<8} {(rows[clip_id].get('text') or '')[:96]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
