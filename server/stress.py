"""Replay captured VHF clips through whisper-proxy repeatedly to measure how often the
whisper.cpp backend crashes.

This measures *reliability*, not accuracy — use bench.py for word error rate. The output is
a crash rate (failures per 100 requests) plus the wall-clock timestamp of every failure, so
a run can be cross-referenced against Windows event log entries.

Deliberately talks to the proxy (:9000), not the backend (:8080) directly, for two reasons:
the proxy's watchdog restarts a wedged backend so an unattended run completes instead of
stalling on the first hang, and the resulting backend requests are byte-for-byte what
production sends (the proxy strips the client's decoder params and injects its own, so what
matters is that we send the same fields the plugin does — see WhisperClient.BuildMultipartBody).

Close SDR# before running: it shares the GPU, so leaving it open changes the load between
runs and makes before/after comparisons meaningless.

Usage (point --captures at a directory of *_sent.wav clips recorded by the plugin's
"Capture chunks" option; see docs/user-manual.md):
    py stress.py --captures "<SDRSharp>/Plugins/SttPlugin/captures/<date>"
    py stress.py --captures ... --passes 2 --label baseline-rocm-6.1.3
"""

from __future__ import annotations

import argparse
import datetime
import json
import sys
from pathlib import Path

from bench import discover_clips, transcribe


# The exact fields the plugin sends (WhisperClient.BuildMultipartBody). The proxy discards
# the decoder params and substitutes its own, so these mainly need to *parse* as the same
# shape of multipart request production sends.
PLUGIN_FIELDS = {
    "temperature": "0",
    "response_format": "json",
    "language": "en",
}


def classify(error: str | None) -> str:
    """Bucket a failure into a cause.

    'backend_crash' is the signature of the GPU driver fault this harness exists to count:
    the proxy's watchdog kills the wedged whisper-server, which drops the TCP connection
    mid-request and surfaces as a 503 carrying a connection-reset message.
    """
    if error is None:
        return "ok"
    lowered = error.lower()
    if any(s in lowered for s in (
        "remote end closed", "connection reset", "forcibly closed",
        "connectionreset", "remotedisconnected", "incompleteread",
    )):
        return "backend_crash"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    if "refused" in lowered:
        return "backend_down"
    if error.startswith("HTTP "):
        return "http_error"
    return "other"


def run(args: argparse.Namespace) -> int:
    captures = Path(args.captures)
    if not captures.is_dir():
        print(f"error: --captures directory not found: {captures}", file=sys.stderr)
        return 2

    clips = discover_clips(captures)
    if not clips:
        print(f"error: no *_sent.wav clips found under {captures}", file=sys.stderr)
        return 2

    total = len(clips) * args.passes
    print(f"{len(clips)} clips x {args.passes} passes = {total} requests "
          f"-> {args.host}:{args.port}{args.path}")
    print(f"label: {args.label}\n")

    records: list[dict] = []
    counts: dict[str, int] = {}
    started = datetime.datetime.now()

    n = 0
    for pass_idx in range(1, args.passes + 1):
        for clip_id, wav_path in clips:
            n += 1
            audio = wav_path.read_bytes()
            stamp = datetime.datetime.now()
            _text, elapsed, error = transcribe(
                args.host, args.port, args.path, dict(PLUGIN_FIELDS), audio,
                timeout=args.timeout,
            )
            kind = classify(error)
            counts[kind] = counts.get(kind, 0) + 1

            if kind != "ok":
                records.append({
                    "n": n,
                    "pass": pass_idx,
                    "clip": clip_id,
                    "time": stamp.isoformat(timespec="seconds"),
                    "elapsed_s": round(elapsed, 2),
                    "kind": kind,
                    "error": error,
                })
                print(f"  [{stamp:%H:%M:%S}] #{n}/{total} {clip_id}: {kind} — {error}")
            elif n % 25 == 0:
                ok = counts.get("ok", 0)
                print(f"  [{stamp:%H:%M:%S}] #{n}/{total} ok={ok} "
                      f"failures={n - ok}")

    finished = datetime.datetime.now()
    ok = counts.get("ok", 0)
    failures = n - ok
    crashes = counts.get("backend_crash", 0)

    summary = {
        "label": args.label,
        "started": started.isoformat(timespec="seconds"),
        "finished": finished.isoformat(timespec="seconds"),
        "captures": str(captures),
        "clips": len(clips),
        "passes": args.passes,
        "requests": n,
        "ok": ok,
        "failures": failures,
        "counts": counts,
        "crashes_per_100": round(crashes / n * 100, 2) if n else 0.0,
        "failures_per_100": round(failures / n * 100, 2) if n else 0.0,
        "failure_records": records,
    }

    out = Path(args.out_json)
    out.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    duration = (finished - started).total_seconds() / 60
    print(f"\n{'=' * 62}")
    print(f"label            : {args.label}")
    print(f"duration         : {duration:.1f} min")
    print(f"requests         : {n}")
    print(f"ok               : {ok}")
    for kind in sorted(k for k in counts if k != "ok"):
        print(f"{kind:<17}: {counts[kind]}")
    print(f"{'-' * 62}")
    print(f"backend crashes  : {crashes}  ({summary['crashes_per_100']} per 100 requests)")
    print(f"all failures     : {failures}  ({summary['failures_per_100']} per 100 requests)")
    print(f"{'=' * 62}")
    print(f"\nwritten to {out}")
    print("cross-check crash timestamps against Windows Event 10111:")
    print(f'  Get-WinEvent -FilterHashtable @{{LogName="System"; Id=10111; '
          f'StartTime=(Get-Date "{started:yyyy-MM-dd HH:mm:ss}")}} | Select-Object TimeCreated')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--captures", required=True, help="Directory containing *_sent.wav clips")
    parser.add_argument("--passes", type=int, default=2, help="Times to replay the clip set")
    parser.add_argument("--label", default="unlabelled",
                        help="Tag for this run, e.g. baseline-rocm-6.1.3")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=9000, help="Proxy port (not the backend)")
    parser.add_argument("--path", default="/v1/audio/transcriptions")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--out-json", default="stress-results.json")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
