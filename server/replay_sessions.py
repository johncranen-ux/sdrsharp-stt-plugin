"""Replay captured traffic through the vessel-identification pipeline and diff the result.

There is no ground truth for vessel identification -- the project has never had one, and
WER does not apply to enriched output. So this does not produce an accuracy score. It
replays real chunks in their original order and timing with the identification filters on
and off, and shows exactly what changed, for a human to judge.

Uses only the stored transcripts, so it needs Claude but no audio, no Groq and no GPU.

Two modes:
    --compare        live per-chunk identification with the filters off vs on
    --conversations  group into windows and run the retrospective resolver, which decides
                     identity after an exchange ends and never touches the transcription

Usage:
    set ANTHROPIC_API_KEY=...
    py replay_sessions.py --captures "D:\\SDR\\...\\captures\\2026-07-28" --conversations
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

# Mutable state lives in the module that owns it, so it must be reset there: clearing a
# re-exported name on `proxy` would leave the real list untouched.
from stt_proxy import conversations  # noqa: E402


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


def run(chunks: list[dict], hints: bool, echo: bool) -> list[dict]:
    """Replay every chunk against a fresh buffer with the given filter settings."""
    proxy.AIS_HINT_FILTER     = hints
    proxy.PROMPT_ECHO_FILTER  = echo
    conversations._vessel_buffer[:] = []

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

    print()
    print("=" * 78)
    print(f"{len(before)} chunks replayed")
    print(f"  identifications changed : {len(changed)}")
    print(f"    vessel removed        : {len(dropped)}   (phantom identifications dropped)")
    print(f"    vessel added          : {len(added)}")
    print(f"    vessel replaced       : {len(swapped)}")
    print(f"  flagged as prompt echo  : {len(echoes)}")
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


def resolve_replay(chunks: list[dict]) -> None:
    """Replay windows through the retrospective resolver and report what it decided.

    Runs the live per-chunk pass first (that is where callsigns come from), then groups the
    results into windows and resolves each, exactly as the reaper does in production.
    """
    conversations._conversation_chunks[:] = []
    journal = []
    for i, c in enumerate(chunks, 1):
        result = proxy.enrich_with_ais(proxy.extract_vessel(c["text"], c["channel"], now=c["time"]))
        journal.append({
            "id": i, "time": c["time"], "channel": c["channel"], "text": c["text"],
            "live_vessel": result.get("vessel"), "live_mmsi": result.get("mmsi"),
            "callsign": result.get("callsign"),
        })
        if i % 25 == 0:
            print(f"    live pass ...{i}/{len(chunks)}", file=sys.stderr, flush=True)

    windows = proxy._split_windows(journal)
    print(f"  {len(windows)} windows; resolving...", file=sys.stderr, flush=True)

    exchanges = []
    for w, window in enumerate(windows, 1):
        for ex in proxy.resolve_conversation(window):
            ex["_turns"] = [c for c in window if c["id"] in ex["chunk_ids"]]
            exchanges.append(ex)
        print(f"    window ...{w}/{len(windows)}", file=sys.stderr, flush=True)

    # Objective anchor: a spelled-out callsign resolves to a vessel by exact lookup, so where
    # one exists the resolver's answer is either consistent with it or it is not. This checks
    # the resolver never contradicts hard evidence -- it is not an independent accuracy score,
    # since the resolver is told to prefer callsign candidates in the first place.
    checked = agreed = 0
    changed = named = 0
    disagreements: list[tuple[dict, dict]] = []
    print()
    print("=" * 78)
    for ex in exchanges:
        turns = ex["_turns"]
        live = {t["live_vessel"] for t in turns if t["live_vessel"]}
        if ex.get("vessel"):
            named += 1
        if live and (len(live) > 1 or ex.get("vessel") not in live):
            changed += 1

        # Collect every callsign in the exchange, not just the first: live extraction is
        # itself fallible, so a lone spurious callsign should not be treated as ground truth.
        refs = {}
        for t in turns:
            cs = (t.get("callsign") or "").strip()
            hit = proxy.match_by_callsign(cs)
            if hit:
                refs[cs] = hit["name"]
        if refs:
            checked += 1
            if ex.get("vessel") in refs.values():
                agreed += 1
            else:
                disagreements.append((ex, refs))

    print(f"{len(chunks)} chunks -> {len(windows)} windows -> {len(exchanges)} exchanges")
    print(f"  exchanges given a vessel        : {named}")
    print(f"  differ from the live per-chunk  : {changed}")
    if checked:
        print(f"  callsign cross-check            : {agreed}/{checked} consistent "
              f"({100*agreed/checked:.0f}%), coverage {checked}/{len(exchanges)} exchanges")
    else:
        print("  callsign cross-check            : no exchange contained a resolvable callsign")
    print("=" * 78)

    if disagreements:
        print()
        print("Callsign disagreements (resolver vs the callsign the live pass extracted).")
        print("Either the resolver is wrong, or the live callsign extraction was:")
        for ex, refs in disagreements:
            print(f"  resolver={ex.get('vessel')!r}  callsign-derived={refs}")
            for t in ex["_turns"]:
                if t.get("callsign"):
                    print(f"     cs={t['callsign']!r} from: {t['text'][:70]!r}")

    print()
    print("No accuracy score: vessel identity has no ground truth. Review the exchanges below.")
    print()

    for ex in exchanges:
        turns = ex["_turns"]
        head = ex.get("vessel") or "(unidentified)"
        via  = " [via callsign]" if ex.get("via_callsign") else ""
        print(f"--- {turns[0]['time'].strftime('%H:%M:%S')}-{turns[-1]['time'].strftime('%H:%M:%S')}"
              f"  {head}{via}  {ex.get('confidence')}  ({len(turns)} turns)")
        if ex.get("evidence"):
            print(f"    evidence: {ex['evidence']}")
        for t in turns:
            live = t["live_vessel"]
            mark = f"   (live: {live})" if live and live != ex.get("vessel") else ""
            print(f"    {t['time'].strftime('%H:%M:%S')}  {t['text'][:88]}{mark}")
        print()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--captures", required=True, help="Capture directory containing index.jsonl")
    ap.add_argument("--limit", type=int, default=None, help="Replay only the first N chunks")
    ap.add_argument("--compare", action="store_true",
                    help="Replay twice (filters off, then on) and diff")
    ap.add_argument("--conversations", action="store_true",
                    help="Group into windows and run the retrospective resolver")
    args = ap.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set")

    proxy._load_cache()
    chunks = load_chunks(Path(args.captures), args.limit)
    print(f"{len(chunks)} chunks with usable transcripts", file=sys.stderr)

    if args.conversations:
        resolve_replay(chunks)
        return 0

    if not args.compare:
        report(run(chunks, False, False), run(chunks, True, True))
        return 0

    print("  replaying with all filters OFF (current behaviour)...", file=sys.stderr)
    before = run(chunks, hints=False, echo=False)
    print("  replaying with all filters ON...", file=sys.stderr)
    after = run(chunks, hints=True, echo=True)
    report(before, after)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
