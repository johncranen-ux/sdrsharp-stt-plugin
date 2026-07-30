"""Replay captured traffic through the vessel-identification pipeline and diff the result.

There is no ground truth for vessel identification -- the project has never had one, and
WER does not apply to enriched output. So this does not produce an accuracy score. It
replays real chunks in their original order and timing with the identification filters on
and off, and shows exactly what changed, for a human to judge.

Uses only the stored transcripts, so it needs Claude but no audio, no Groq and no GPU.

Usage:
    set ANTHROPIC_API_KEY=...
    py replay_sessions.py --captures "D:\\SDR\\...\\captures\\2026-07-28" --compare
    py replay_sessions.py --captures ... --compare --limit 60
"""
import argparse
import datetime
import importlib.util
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent


def _load_proxy():
    # whisper-proxy.py has a hyphen in its name, so it cannot be imported normally.
    spec = importlib.util.spec_from_file_location("whisper_proxy", _HERE / "whisper-proxy.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["whisper_proxy"] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(_HERE))
from make_references import strip_enrichment  # noqa: E402

proxy = _load_proxy()


def load_chunks(captures: Path, limit: int | None) -> list[dict]:
    index = captures / "index.jsonl"
    if not index.exists():
        sys.exit(f"no index.jsonl in {captures}")

    rows = []
    for line in index.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        # Stored text is the enriched display string on CH01; recover the transcript.
        text = strip_enrichment(row.get("text") or "").strip()
        if not text:
            continue
        rows.append({
            "id": f"{row['index']:04d}",
            "time": datetime.datetime.fromisoformat(row["timestamp"]).replace(tzinfo=None),
            "channel": (row.get("channel") or "").strip(),
            "text": text,
        })
    rows.sort(key=lambda r: r["time"])
    return rows[:limit] if limit else rows


def run(chunks: list[dict], session: bool, hints: bool, echo: bool) -> list[dict]:
    """Replay every chunk against a fresh buffer with the given filter settings."""
    proxy.SESSION_CONTEXT     = session
    proxy.AIS_HINT_FILTER     = hints
    proxy.PROMPT_ECHO_FILTER  = echo
    proxy._vessel_buffer[:] = []

    out = []
    for i, c in enumerate(chunks, 1):
        if proxy._is_prompt_echo(c["text"], proxy.DEFAULT_MARITIME_PROMPT):
            out.append({**c, "display": "", "vessel": None, "echo": True})
            continue

        result = proxy.extract_vessel(c["text"], c["channel"], now=c["time"])
        result = proxy.enrich_with_ais(result)
        proxy._add_to_buffer(result, c["text"], c["channel"], when=c["time"])
        out.append({
            **c,
            "display": proxy.format_for_plugin(result),
            "vessel": result.get("vessel"),
            "mmsi": result.get("mmsi"),
            "inferred": result.get("vessel_source") == "inferred",
            "echo": False,
        })
        if i % 25 == 0:
            print(f"    ...{i}/{len(chunks)}", file=sys.stderr, flush=True)
    return out


def report(before: list[dict], after: list[dict]) -> None:
    changed = [(b, a) for b, a in zip(before, after)
               if b.get("vessel") != a.get("vessel") or b.get("echo") != a.get("echo")]

    dropped   = [(b, a) for b, a in changed if b.get("vessel") and not a.get("vessel")]
    added     = [(b, a) for b, a in changed if a.get("vessel") and not b.get("vessel")]
    swapped   = [(b, a) for b, a in changed
                 if b.get("vessel") and a.get("vessel") and b["vessel"] != a["vessel"]]
    echoes    = [a for a in after if a.get("echo")]
    inherited = [a for a in after if a.get("inferred")]

    print()
    print("=" * 78)
    print(f"{len(before)} chunks replayed")
    print(f"  identifications changed : {len(changed)}")
    print(f"    vessel removed        : {len(dropped)}   (phantom identifications dropped)")
    print(f"    vessel added          : {len(added)}")
    print(f"    vessel replaced       : {len(swapped)}")
    print(f"  flagged as prompt echo  : {len(echoes)}")
    print(f"  identity inherited      : {len(inherited)}   (shown as [~NAME])")
    print("=" * 78)
    print()
    print("No accuracy score is reported: there is no ground truth for vessel identity.")
    print("Review the diff below and judge whether the 'after' column is more truthful.")
    print()

    for b, a in changed:
        print(f"[{b['id']}] {b['time'].strftime('%H:%M:%S')}  ch {b['channel']}")
        print(f"   text   {b['text'][:96]}")
        print(f"   before {b.get('display') or '(none)'}")
        print(f"   after  {'(suppressed: prompt echo)' if a.get('echo') else (a.get('display') or '(none)')}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--captures", required=True, help="Capture directory containing index.jsonl")
    ap.add_argument("--limit", type=int, default=None, help="Replay only the first N chunks")
    ap.add_argument("--compare", action="store_true",
                    help="Replay twice (filters off, then on) and diff")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set")

    proxy._load_cache()
    chunks = load_chunks(Path(args.captures), args.limit)
    print(f"{len(chunks)} chunks with usable transcripts", file=sys.stderr)

    if not args.compare:
        report(run(chunks, False, False, False), run(chunks, True, True, True))
        return 0

    print("  replaying with all filters OFF (current behaviour)...", file=sys.stderr)
    before = run(chunks, session=False, hints=False, echo=False)
    print("  replaying with all filters ON...", file=sys.stderr)
    after = run(chunks, session=True, hints=True, echo=True)
    report(before, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
