"""Replay captured VHF clips against the whisper.cpp server across a parameter matrix
and report word error rate + decode time per configuration.

Talks to whisper.cpp's /inference endpoint directly (bypasses whisper-proxy.py) so
results reflect the decoder alone, not the proxy's post-processing.

Usage:
    py bench.py --captures ../captures/2026-07-27 --references references.txt
    py bench.py --captures ../captures/2026-07-27 --references references.txt --matrix full
    py bench.py --captures ../captures/2026-07-27 --matrix full --model-label large-v3

Clip discovery: every "<id>_sent.wav" file under --captures (as written by the plugin's
ChunkRecorder). References file: one "<id>\t<transcript>" line per clip; clips without a
reference are still transcribed (shown in the report) but excluded from WER aggregates.
"""

from __future__ import annotations

import argparse
import html
import http.client
import json
import os
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from stt_proxy import backends  # noqa: E402  (path set above)


# ---------------------------------------------------------------------------
# Configuration matrix
#
# Each entry is a set of whisper.cpp /inference form fields. "current" mirrors what the
# plugin sends today (see WhisperClient.cs) so it always serves as the baseline row.
# Edit/add entries here as new parameters are worth comparing.
# ---------------------------------------------------------------------------

# The prompt production actually sends, imported rather than restated. bench.py carried its
# own copy until 2026-08-06, and the two had drifted: every WER figure on record (including
# the "~9-10 points, largest single lever" claim in docs/design-notes.md) was measured
# against a prompt the proxy has never sent. A copy cannot be kept honest by discipline, so
# there is now only one.
MARITIME_PROMPT = backends.DEFAULT_MARITIME_PROMPT

# That drifted copy. Measured against the shipped prompt over 244 clips on 2026-08-06 (see
# docs/design-notes.md): worse by 2.6 points pooled, and worse on 74 clips against 50 better
# (sign test p=0.038) -- so the shipped prompt wins, but the pooled-WER interval grazes zero.
# Kept because that is a direction without a magnitude: a future, larger reference set should
# be able to settle it, and it cannot re-run the comparison if this text is gone.
LEGACY_BENCH_PROMPT = (
    "Maas Approach, this is Motortanker Neptune, over. "
    "Roger, standing by on channel one six. "
    "Rotterdam VTS, Pilot Rotterdam, Botlek Traffic, over, out, wilco."
)

# Candidate v2, arm 1: the shipped prompt with the invented vessel name and callsign taken
# out and NOTHING else changed, so a difference is attributable to that alone. The names earn
# removal on three independent counts (docs/design-notes.md): they manufactured four false
# references, they are still echoed into live output on ~1.6% of clips, and the spelled
# callsign coincides with letter-by-letter spelling failures. The "this is <vessel>" shape is
# kept, since that structure may be what the prompt is really teaching.
NO_NAMES_PROMPT = (
    "Maas Approach, this is the inbound motortanker, requesting permission "
    "to enter the Botlek, over. "
    "Motortanker, Maas Approach, roger, proceed to VHF channel six one, out. "
    "Rotterdam VTS, be advised we are standing by on channel one six, over."
)

# Arm 2: the same removal, plus vocabulary actually observed in these captures -- station and
# place names, and procedural phrasing. Deliberately contains no vessel name at all: the
# lesson of the false references is that any name in the prompt can be echoed out and matched
# against AIS. Uses the ~160 tokens of Groq's 224-token budget the shipped prompt leaves idle.
VOCAB_PROMPT = (
    "Maas Approach, Maas Aanloop, this is the inbound motortanker, requesting "
    "permission to enter the Botlek, over. "
    "Maas Approach, roger, proceed to VHF channel six one, out. "
    "Rotterdam VTS, be advised we are standing by on channel one six, over. "
    "Pilot Maas, we are outbound from Europoort past the Maasvlakte, our draught "
    "is eleven metres twenty, pilot ladder portside, over. "
    "Maas Approach, my intention is to proceed for East Anchorage, crossing the "
    "Deepwater route, ETA at the Maas Center buoy one four four five, over. "
    "Understood, shall we change to channel seven seven, over."
)

# Selectable by name from the command line (bench.py --prompt, bench_stt.py --prompt).
PROMPTS: dict[str, str] = {
    "shipped": MARITIME_PROMPT,
    "legacy": LEGACY_BENCH_PROMPT,
    "no_names": NO_NAMES_PROMPT,
    "vocab": VOCAB_PROMPT,
}

CONFIGS: dict[str, dict[str, Any]] = {
    "current": {
        "temperature": "0",
    },
    "prompt": {
        "temperature": "0",
        "prompt": MARITIME_PROMPT,
        "carry_initial_prompt": "true",
    },
    "beam5": {
        "temperature": "0",
        "beam_size": "5",
        "best_of": "5",
    },
    "beam5_prompt": {
        "temperature": "0",
        "beam_size": "5",
        "best_of": "5",
        "prompt": MARITIME_PROMPT,
        "carry_initial_prompt": "true",
    },
    "beam1_prompt": {
        "temperature": "0",
        "beam_size": "1",
        "best_of": "1",
        "prompt": MARITIME_PROMPT,
        "carry_initial_prompt": "true",
    },
    # Groq exposes no decoder controls, so this is beam5_prompt minus the knobs that
    # have no equivalent there. Run it through the proxy (--port 9000 --path
    # /v1/audio/transcriptions) with STT_BACKEND=groq; the proxy drops client-supplied
    # decoder params either way, so what actually differs from beam5_prompt is the
    # backend, not these fields.
    "groq_prompt": {
        "temperature": "0",
        "prompt": MARITIME_PROMPT,
        "carry_initial_prompt": "true",
    },
    "vad": {
        "temperature": "0",
        "vad": "true",
        "vad_threshold": "0.5",
        "vad_min_speech_duration_ms": "250",
        "vad_speech_pad_ms": "100",
    },
    "full": {
        "temperature": "0",
        "beam_size": "5",
        "best_of": "5",
        "suppress_nst": "true",
        "prompt": MARITIME_PROMPT,
        "carry_initial_prompt": "true",
        "vad": "true",
        "vad_threshold": "0.5",
        "vad_min_speech_duration_ms": "250",
        "vad_speech_pad_ms": "100",
    },
}

MATRIX_PRESETS: dict[str, list[str]] = {
    "quick": ["current", "full"],
    "full": list(CONFIGS.keys()),
}


# ---------------------------------------------------------------------------
# WER
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9']+")
_BRACKET_RE = re.compile(r"\[[^\]]*\]")  # e.g. "[inaudible]" markers in hand-written references


def _normalize(text: str) -> list[str]:
    # Bracketed annotations ([inaudible], [static], etc.) mark audio the transcriber
    # couldn't make out — strip them so that stretch contributes neither a word Whisper
    # must match nor a penalty for whatever it actually produced there. A trailing "?" on
    # an uncertain-but-guessed word needs no special handling: the regex below already
    # drops all punctuation when extracting word tokens.
    text = _BRACKET_RE.sub(" ", text)
    return _WORD_RE.findall(text.lower())


def word_error_counts(reference: str, hypothesis: str) -> tuple[int, int] | None:
    """Returns (edit_distance, ref_word_count), or None if there's no usable reference.
    Exposed separately from word_error_rate so callers can pool edits/words across many
    clips into a single corpus-level WER, rather than only averaging per-clip ratios (which
    lets short references -- a 1-word clip is either 0% or 100% WER, nothing in between --
    dominate the average out of proportion to how many words they actually contain).
    """
    ref = _normalize(reference)
    hyp = _normalize(hypothesis)
    if not ref:
        return None  # no usable reference; excluded from aggregates
    if not hyp:
        return len(ref), len(ref)

    n, m = len(ref), len(hyp)
    prev_row = list(range(m + 1))
    for i in range(1, n + 1):
        cur_row = [i] + [0] * m
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            cur_row[j] = min(
                prev_row[j] + 1,      # deletion
                cur_row[j - 1] + 1,   # insertion
                prev_row[j - 1] + cost,  # substitution
            )
        prev_row = cur_row
    return prev_row[m], n


def word_alignment_ops(reference: str, hypothesis: str) -> list[tuple[str, str | None, str | None]] | None:
    """Returns the Levenshtein alignment between reference and hypothesis as a list of
    (op, ref_word, hyp_word) tuples, op in {"match", "sub", "ins", "del"}. Unlike
    word_error_counts (which only returns the edit count), this exposes the actual
    substitution pairs so real recurring mis-transcription patterns can be found instead
    of just measured. Same normalization and no-reference behavior as word_error_counts.
    """
    ref = _normalize(reference)
    hyp = _normalize(hypothesis)
    if not ref:
        return None

    n, m = len(ref), len(hyp)
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n + 1):
        dp[i][0] = i
    for j in range(m + 1):
        dp[0][j] = j
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = 0 if ref[i - 1] == hyp[j - 1] else 1
            dp[i][j] = min(
                dp[i - 1][j] + 1,
                dp[i][j - 1] + 1,
                dp[i - 1][j - 1] + cost,
            )

    ops: list[tuple[str, str | None, str | None]] = []
    i, j = n, m
    while i > 0 or j > 0:
        if i > 0 and j > 0 and ref[i - 1] == hyp[j - 1] and dp[i][j] == dp[i - 1][j - 1]:
            ops.append(("match", ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif i > 0 and j > 0 and dp[i][j] == dp[i - 1][j - 1] + 1:
            ops.append(("sub", ref[i - 1], hyp[j - 1]))
            i, j = i - 1, j - 1
        elif j > 0 and dp[i][j] == dp[i][j - 1] + 1:
            ops.append(("ins", None, hyp[j - 1]))
            j -= 1
        else:
            ops.append(("del", ref[i - 1], None))
            i -= 1
    ops.reverse()
    return ops


def word_error_rate(reference: str, hypothesis: str) -> float | None:
    counts = word_error_counts(reference, hypothesis)
    if counts is None:
        return None
    edits, ref_len = counts
    return edits / ref_len


# ---------------------------------------------------------------------------
# whisper.cpp client
# ---------------------------------------------------------------------------

def build_multipart(fields: dict[str, str], file_bytes: bytes) -> tuple[str, bytes]:
    boundary = "----Bench" + os.urandom(12).hex()
    parts: list[bytes] = []
    for name, value in fields.items():
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="audio.wav"\r\n'
        f'Content-Type: audio/wav\r\n\r\n'.encode("utf-8")
        + file_bytes
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(parts)


def transcribe(host: str, port: int, path: str, fields: dict[str, str],
                file_bytes: bytes, timeout: float = 90.0) -> tuple[str, float, str | None]:
    boundary, body = build_multipart(fields, file_bytes)
    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    conn = http.client.HTTPConnection(host, port, timeout=timeout)
    t0 = time.monotonic()
    try:
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        elapsed = time.monotonic() - t0
        if resp.status != 200:
            preview = raw[:200].decode("utf-8", errors="replace")
            return "", elapsed, f"HTTP {resp.status}: {preview}"
        data = json.loads(raw)
        return (data.get("text") or "").strip(), elapsed, None
    except Exception as exc:  # noqa: BLE001 - report, don't crash the sweep
        return "", time.monotonic() - t0, f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Clip discovery + references
# ---------------------------------------------------------------------------

def discover_clips(captures_dir: Path) -> list[tuple[str, Path]]:
    clips = []
    for wav in sorted(captures_dir.glob("*_sent.wav")):
        clip_id = wav.name[: -len("_sent.wav")]
        clips.append((clip_id, wav))
    return clips


def load_references(path: Path | None) -> dict[str, str]:
    if path is None or not path.exists():
        return {}
    refs: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "\t" in line:
            clip_id, text = line.split("\t", 1)
        elif ":" in line:
            clip_id, text = line.split(":", 1)
        else:
            continue
        refs[clip_id.strip()] = text.strip()
    return refs


# ---------------------------------------------------------------------------
# Bench run
# ---------------------------------------------------------------------------

def run(args: argparse.Namespace) -> int:
    captures_dir = Path(args.captures)
    if not captures_dir.is_dir():
        print(f"error: captures dir not found: {captures_dir}", file=sys.stderr)
        return 1

    clips = discover_clips(captures_dir)
    if not clips:
        print(f"error: no *_sent.wav clips found under {captures_dir}", file=sys.stderr)
        return 1

    refs = load_references(Path(args.references) if args.references else None)
    config_names = MATRIX_PRESETS.get(args.matrix, [args.matrix])
    missing = [c for c in config_names if c not in CONFIGS]
    if missing:
        print(f"error: unknown config(s) {missing}; available: {list(CONFIGS)}", file=sys.stderr)
        return 1

    print(f"clips: {len(clips)}   references: {len(refs)}   configs: {config_names}")
    print(f"prompt: {args.prompt} ({len(PROMPTS[args.prompt].split())} words)")
    print(f"backend: {args.host}:{args.port}{args.path}   model-label: {args.model_label or '(unset)'}\n")

    # results[config_name] = list of dict(clip_id, text, elapsed, wer, error)
    results: dict[str, list[dict[str, Any]]] = {name: [] for name in config_names}

    for config_name in config_names:
        fields = CONFIGS[config_name]
        # Swap in the selected prompt without mutating CONFIGS -- configs with no "prompt"
        # key are unprompted on purpose ("current", "beam5") and must stay that way.
        if "prompt" in fields:
            fields = {**fields, "prompt": PROMPTS[args.prompt]}
        print(f"-- {config_name} --")
        for clip_id, wav_path in clips:
            file_bytes = wav_path.read_bytes()
            text, elapsed, error = transcribe(
                args.host, args.port, args.path, fields, file_bytes, timeout=args.timeout
            )
            reference = refs.get(clip_id)
            wer = word_error_rate(reference, text) if reference is not None else None
            results[config_name].append({
                "clip_id": clip_id,
                "text": text,
                "elapsed": elapsed,
                "wer": wer,
                "error": error,
                "reference": reference,
            })
            status = f"WER={wer:.2f}" if wer is not None else "(no ref)"
            err_note = f"  [{error}]" if error else ""
            print(f"  {clip_id}  {elapsed:5.2f}s  {status:12s}  {text[:70]!r}{err_note}")

        n = len(results[config_name])
        empty = sum(1 for r in results[config_name] if not r["text"])
        if n > 0 and empty / n > 0.5:
            print(f"  WARNING: {empty}/{n} clips returned empty text for '{config_name}' — "
                  f"check server connectivity/params before trusting this config's numbers.")
        print()

    print_summary(results)
    write_json(results, args.out_json, args.model_label)
    write_html_report(results, args.out_html, args.model_label)
    print(f"\nwrote {args.out_json} and {args.out_html}")
    return 0


def _pooled_wer(rows: list[dict[str, Any]]) -> float | None:
    """Corpus-level WER: total edits / total reference words across all clips, rather than
    the average of each clip's own ratio. Standard WER definition; doesn't let a 1-2 word
    clip swing the result as much as a 20-word one just because both count as "one clip".
    """
    total_edits = total_words = 0
    for r in rows:
        if r["reference"] is None:
            continue
        counts = word_error_counts(r["reference"], r["text"])
        if counts is None:
            continue
        edits, n = counts
        total_edits += edits
        total_words += n
    return total_edits / total_words if total_words else None


def print_summary(results: dict[str, list[dict[str, Any]]]) -> None:
    print("=== summary ===")
    header = f"{'config':<16}{'n':>4}{'macro WER':>10}{'pooled WER':>11}{'median s':>10}{'p95 s':>8}"
    print(header)
    print("-" * len(header))
    for name, rows in results.items():
        wers = [r["wer"] for r in rows if r["wer"] is not None]
        times = sorted(r["elapsed"] for r in rows)
        mean_wer = f"{statistics.mean(wers):.3f}" if wers else "n/a"
        pooled = _pooled_wer(rows)
        pooled_str = f"{pooled:.3f}" if pooled is not None else "n/a"
        median_t = f"{statistics.median(times):.2f}" if times else "n/a"
        p95_t = f"{times[int(0.95 * (len(times) - 1))]:.2f}" if times else "n/a"
        print(f"{name:<16}{len(rows):>4}{mean_wer:>10}{pooled_str:>11}{median_t:>10}{p95_t:>8}")


def write_json(results: dict[str, list[dict[str, Any]]], out_path: str, model_label: str | None) -> None:
    payload = {"model_label": model_label, "results": results}
    Path(out_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_html_report(results: dict[str, list[dict[str, Any]]], out_path: str, model_label: str | None) -> None:
    config_names = list(results.keys())
    clip_ids = [r["clip_id"] for r in next(iter(results.values()), [])]

    rows_html = []
    for clip_id in clip_ids:
        cells = [f"<td class='clip'>{html.escape(clip_id)}</td>"]
        for name in config_names:
            row = next(r for r in results[name] if r["clip_id"] == clip_id)
            wer_str = f"{row['wer']:.2f}" if row["wer"] is not None else "&mdash;"
            err = f"<div class='err'>{html.escape(row['error'])}</div>" if row["error"] else ""
            cells.append(
                f"<td><div class='meta'>{row['elapsed']:.2f}s &middot; WER {wer_str}</div>"
                f"<div class='text'>{html.escape(row['text'])}</div>{err}</td>"
            )
        rows_html.append("<tr>" + "".join(cells) + "</tr>")

    summary_rows = []
    for name, rows in results.items():
        wers = [r["wer"] for r in rows if r["wer"] is not None]
        times = sorted(r["elapsed"] for r in rows)
        mean_wer = f"{statistics.mean(wers):.3f}" if wers else "n/a"
        pooled = _pooled_wer(rows)
        pooled_str = f"{pooled:.3f}" if pooled is not None else "n/a"
        median_t = f"{statistics.median(times):.2f}s" if times else "n/a"
        summary_rows.append(f"<tr><td>{html.escape(name)}</td><td>{len(rows)}</td>"
                             f"<td>{mean_wer}</td><td>{pooled_str}</td><td>{median_t}</td></tr>")

    doc = f"""<!doctype html>
<html><head><meta charset="utf-8"><title>whisper bench{' - ' + html.escape(model_label) if model_label else ''}</title>
<style>
body {{ font-family: -apple-system, Segoe UI, sans-serif; margin: 24px; background:#111; color:#eee; }}
h1 {{ font-size: 18px; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 24px; }}
th, td {{ border: 1px solid #333; padding: 6px 10px; text-align: left; vertical-align: top; font-size: 13px; }}
th {{ background: #1c1c1c; position: sticky; top: 0; }}
.clip {{ font-family: monospace; white-space: nowrap; }}
.meta {{ color: #888; font-size: 11px; margin-bottom: 2px; }}
.text {{ white-space: pre-wrap; }}
.err {{ color: #e66; font-size: 11px; margin-top: 2px; }}
</style></head><body>
<h1>Whisper bench{' &mdash; ' + html.escape(model_label) if model_label else ''}</h1>
<table><tr><th>config</th><th>n</th><th>macro WER</th><th>pooled WER</th><th>median time</th></tr>
{''.join(summary_rows)}
</table>
<table>
<tr><th>clip</th>{''.join(f'<th>{html.escape(n)}</th>' for n in config_names)}</tr>
{''.join(rows_html)}
</table>
</body></html>"""

    Path(out_path).write_text(doc, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--captures", required=True, help="Directory containing *_sent.wav clips")
    parser.add_argument("--references", help="Path to references.txt (id<TAB>transcript per line)")
    parser.add_argument("--matrix", default="quick", help="quick | full | <single config name>")
    parser.add_argument("--prompt", default="shipped", choices=sorted(PROMPTS),
                        help="which prompt the prompt-bearing configs use (default: shipped, "
                             "i.e. whatever the proxy currently sends)")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--path", default="/inference")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--model-label", default=None, help="Tag for this run, e.g. large-v3-turbo")
    parser.add_argument("--out-json", default="bench-results.json")
    parser.add_argument("--out-html", default="bench-report.html")
    args = parser.parse_args()
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
