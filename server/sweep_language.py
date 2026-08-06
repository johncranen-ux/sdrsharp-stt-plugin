#!/usr/bin/env python3
"""Decode a capture set with no prompt, and report what comes back.

**Which hand-written references are the prompt talking?** `make_references.py` drafts
references from the plugin's own *prompted* output for hand-correction, so an echo the
labeller missed becomes ground truth. A reference cannot be checked against more prompted
output -- but an unprompted decode has no way to know the prompt's vessel name or callsign
exists, so where it diverges sharply the reference is worth re-listening to.

Deliberately does not go through backends.transcribe: that function always sends a prompt,
and the point here is to remove exactly that one variable while changing nothing else.

`--language ""` additionally asks Whisper to detect the language, which was the original
motivation for this script -- how much of the traffic is not English. That turned out not to
work: on a 4-clip smoke test it labelled two plainly English transmissions "Modern Greek" and
transcribed them into Greek script. Language ID on a few seconds of noisy VHF is not
trustworthy, so the option is kept for the record but the default forces English, as
production does. See docs/design-notes.md.

Usage:
    py sweep_language.py --captures "D:\\SDR\\...\\captures\\2026-07-28" \\
        --references references-2026-07-28.txt --out sweep-language-0728.json --sleep 3.2
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import bench                                            # noqa: E402
from stt_proxy import backends, corrections             # noqa: E402


def transcribe_unprompted(wav: Path, timeout: float, language: str) -> tuple[str, str, str | None]:
    """Returns (text, detected_language, error). Never sends a prompt; verbose_json.

    `language=""` lets Whisper detect. Measured on real captures that detection is not
    trustworthy at these clip lengths -- see the note on --language in main().
    """
    fields = {
        "model": backends.GROQ_MODEL,
        "temperature": "0",
        # verbose_json is the only response format that carries the detected language.
        "response_format": "verbose_json",
    }
    if language:
        fields["language"] = language
    file_info = {"field": "file", "filename": wav.name,
                 "content_type": "audio/wav", "data": wav.read_bytes()}
    boundary, body = backends._build_multipart(fields, file_info)
    headers = {
        "Authorization": f"Bearer {backends.GROQ_API_KEY}",
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Content-Length": str(len(body)),
    }
    try:
        conn = http.client.HTTPSConnection(backends.GROQ_HOST, timeout=timeout)
        conn.request("POST", backends.GROQ_PATH, body=body, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
    except Exception as exc:  # noqa: BLE001 - report, don't abort the sweep
        return "", "", f"{type(exc).__name__}: {exc}"
    if resp.status != 200:
        return "", "", f"HTTP {resp.status}: {raw[:160].decode('utf-8', 'replace')}"
    data = json.loads(raw.decode("utf-8"))
    return (data.get("text") or "").strip(), (data.get("language") or "").strip(), None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--captures", required=True)
    ap.add_argument("--references", default=None)
    ap.add_argument("--out", required=True)
    ap.add_argument("--sleep", type=float, default=3.2,
                    help="seconds between requests (Groq free tier is 20/min)")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only-with-reference", action="store_true")
    # Defaults to forcing English, which is what production does. Auto-detection ("") was
    # tried and is not usable here: on a 4-clip smoke test Whisper labelled two plainly
    # English transmissions "Modern Greek" and transcribed them into Greek script. Language
    # ID on a few seconds of noisy VHF is unreliable, which also justifies the pinned
    # language in backends.py -- unforced decoding would mangle English, and English is the
    # overwhelming majority of this traffic.
    ap.add_argument("--language", default="en",
                    help='language to force; "" to let Whisper detect (unreliable, see source)')
    args = ap.parse_args()

    # This sweep exists to surface non-English text, so a cp1252 console must not be able to
    # kill it partway through a paid run.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass

    if not backends.GROQ_API_KEY:
        print("error: GROQ_API_KEY not set", file=sys.stderr)
        return 1

    clips = bench.discover_clips(Path(args.captures))
    refs = bench.load_references(Path(args.references)) if args.references else {}
    if args.only_with_reference:
        clips = [(c, p) for c, p in clips if (refs.get(c) or "").strip()]
    if args.limit:
        clips = clips[:args.limit]
    if not clips:
        print("error: no clips found", file=sys.stderr)
        return 1

    print(f"{len(clips)} clips, unprompted, "
          f"language={args.language or 'auto-detected (unreliable)'}\n")
    rows = []
    for index, (clip_id, path) in enumerate(clips, start=1):
        text = lang = ""
        error = None
        for attempt in range(1, args.retries + 1):
            if args.sleep and (index > 1 or attempt > 1):
                time.sleep(args.sleep)
            text, lang, error = transcribe_unprompted(path, args.timeout, args.language)
            if error is None:
                break
            if attempt < args.retries:
                print(f"      retry {attempt}: {error[:70]}", flush=True)
        reference = refs.get(clip_id, "")
        rows.append({
            "clip_id": clip_id, "language": lang, "text": text,
            "reference": reference, "error": error,
            # How far the unprompted decode is from the reference. Not proof of anything on
            # its own -- an unprompted decode of noisy VHF is itself poor -- but it ranks
            # which references are worth a human listening to.
            "divergence": bench.word_error_rate(reference, text) if reference else None,
        })
        flag = "!" if error else " "
        print(f"  [{index:>3}/{len(clips)}]{flag} {clip_id}  [{lang or '??':<10}] {text[:64]}",
              flush=True)

    Path(args.out).write_text(json.dumps({"results": rows}, ensure_ascii=False, indent=1),
                              encoding="utf-8")
    report(rows, forced_language=args.language)
    print(f"\nwrote {args.out}")
    return 0


def report(rows: list[dict], forced_language: str = "") -> None:
    if forced_language:
        # The API echoes back whatever language was forced, so a census here would read
        # "100% English" no matter what the audio actually is. Printing it would invite
        # exactly that misreading.
        print(f"\n(no language census: --language {forced_language!r} was forced, so the "
              f"reported language is an echo of the request, not a detection)")
    else:
        counts: dict[str, int] = {}
        for row in rows:
            counts[row["language"] or "(error)"] = counts.get(row["language"] or "(error)", 0) + 1
        print("\n=== detected language ===")
        for lang, n in sorted(counts.items(), key=lambda kv: -kv[1]):
            print(f"  {lang:<12} {n:>4}  ({n / len(rows):.1%})")

    non_english = [r for r in rows if not forced_language and r["language"]
                   and r["language"].strip().lower() not in ("en", "english")]
    if non_english:
        print(f"\n=== {len(non_english)} clip(s) not detected as English ===")
        for row in non_english:
            print(f"  {row['clip_id']}  [{row['language']}]")
            print(f"      reference   {row['reference'][:96]}")
            print(f"      unprompted  {row['text'][:96]}")

    scored = [r for r in rows if r["divergence"] is not None and not r["error"]]
    scored.sort(key=lambda r: -r["divergence"])
    print(f"\n=== references most divergent from an unprompted decode (top 20 of {len(scored)}) ===")
    print("    high divergence alone is not contamination -- it also flags genuinely hard audio")
    _, distinctive = corrections._prompt_echo_tokens(backends.DEFAULT_MARITIME_PROMPT)
    for row in scored[:20]:
        words = set(corrections._WORD_TOKEN_RE.findall(row["reference"].lower()))
        tag = "  <-- reference contains prompt-distinctive word(s): " + \
              "/".join(sorted(words & distinctive)) if words & distinctive else ""
        print(f"  {row['clip_id']}  divergence {row['divergence']:.0%}{tag}")
        print(f"      reference   {row['reference'][:96]}")
        print(f"      unprompted  {row['text'][:96]}")


if __name__ == "__main__":
    raise SystemExit(main())
