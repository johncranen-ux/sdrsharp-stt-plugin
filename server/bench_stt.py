#!/usr/bin/env python3
"""Transcribe a captures directory into a bench-results JSON, using the production backend.

bench.py talks to a host/port over plain HTTP, which cannot reach Groq, and routing through
the proxy is not an option either: the proxy applies the regex correction pass to every
response (whisper-proxy.py, the maritime branch), so a correction bake-off fed from it would
be scoring corrections applied twice.

This calls stt_proxy.backends.transcribe directly -- the same function the proxy calls, with
the same server-owned decoder params -- and captures the raw text before any post-processing.
Output is shaped exactly like bench.py's results JSON so bench_correct.py can consume it.

Usage:
    py bench_stt.py --captures "D:\\SDR\\SdrSharp\\Plugins\\SttPlugin\\captures\\2026-07-28" \
                    --references references-2026-07-28.txt --out bench-results-0728.json
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench  # noqa: E402
from stt_proxy import backends  # noqa: E402

HERE = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--captures", required=True)
    parser.add_argument("--references", default=None)
    parser.add_argument("--out", required=True)
    parser.add_argument("--config", default="groq_prompt",
                        help="key to store results under, matching bench.py's CONFIGS")
    parser.add_argument("--only-with-reference", action="store_true",
                        help="skip clips that have no reference text (saves API quota)")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    captures = Path(args.captures)
    clips = bench.discover_clips(captures)
    if not clips:
        print(f"error: no *_sent.wav under {captures}", file=sys.stderr)
        return 1

    refs = bench.load_references(Path(args.references)) if args.references else {}
    if args.only_with_reference:
        clips = [(cid, p) for cid, p in clips if (refs.get(cid) or "").strip()]
    if args.limit:
        clips = clips[:args.limit]

    print(f"{len(clips)} clips, backend={backends.STT_BACKEND}, model={backends.GROQ_MODEL}")

    rows = []
    for index, (clip_id, path) in enumerate(clips, start=1):
        file_info = {
            "field": "file",
            "filename": path.name,
            "content_type": "audio/wav",
            "data": path.read_bytes(),
        }
        started = time.monotonic()
        status, body, _headers = backends.transcribe(file_info, language="en",
                                                     prompt=bench.MARITIME_PROMPT)
        elapsed = time.monotonic() - started

        text, error = "", None
        if status == 200:
            try:
                text = (json.loads(body.decode("utf-8")).get("text") or "").strip()
            except Exception as exc:  # noqa: BLE001
                error = f"bad JSON: {exc}"
        else:
            error = f"HTTP {status}: {body[:160].decode('utf-8', 'replace')}"

        rows.append({
            "clip_id": clip_id,
            "text": text,
            "elapsed": elapsed,
            "wer": bench.word_error_rate(refs.get(clip_id, ""), text) if refs.get(clip_id) else None,
            "error": error,
            "reference": refs.get(clip_id, ""),
        })
        flag = "!" if error else " "
        print(f"  [{index:>3}/{len(clips)}]{flag} {clip_id}  {elapsed:5.2f}s  {text[:70]}", flush=True)

    out = Path(args.out)
    out.write_text(json.dumps({
        "model_label": f"{backends.STT_BACKEND}-{backends.GROQ_MODEL}",
        "results": {args.config: rows},
    }, ensure_ascii=False, indent=1), encoding="utf-8")

    scored = [r for r in rows if r["wer"] is not None]
    if scored:
        edits = words = 0
        for row in rows:
            counts = bench.word_error_counts(row["reference"], row["text"])
            if counts:
                edits += counts[0]
                words += counts[1]
        print(f"\npooled WER over {len(scored)} scored clips: {edits / words:.1%}")
    print(f"wrote {out}  ({sum(1 for r in rows if r['error'])} errors)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
