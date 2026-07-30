"""Generate a draft reference (ground-truth) file for a capture directory.

Produces the same format server/references.txt uses -- '#' comments, then
<4-digit id><TAB><transcript> -- pre-filled from the plugin's own recorded
transcriptions so the job is correcting while listening rather than typing from scratch.

Usage:
    py make_references.py --captures "D:\\SDR\\...\\captures\\2026-07-28" \\
        --out references-2026-07-28.txt
"""
import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

# On the Maas Approach channel the plugin displays -- and therefore records -- the
# AIS-enriched string built by format_for_plugin(), e.g.
#     [CALLAO EXPRESS/tanker] (MMSI:218839000) Callao Express, Maas Approach...
# Only the tail is a transcript. Left in, that prefix would become "ground truth" nobody
# ever said and would wreck the WER figures it feeds.
_VESSEL_TAG_RE = re.compile(r"^\s*\[[^\]]*\]\s*")
_MMSI_RE       = re.compile(r"^\s*\(MMSI:[^)]*\)\s*")
_PAREN_RE      = re.compile(r"^\s*\([^)]*\)\s*")


def strip_enrichment(text: str) -> str:
    """Remove a leading vessel tag and identifier added by format_for_plugin()."""
    stripped, had_tag = _VESSEL_TAG_RE.subn("", text, count=1)
    # A bare "(...)" is only the callsign field if a vessel tag preceded it; otherwise
    # leave it alone, since a transcript could legitimately open with a parenthesis.
    if _MMSI_RE.match(stripped):
        stripped = _MMSI_RE.sub("", stripped, count=1)
    elif had_tag:
        stripped = _PAREN_RE.sub("", stripped, count=1)
    return stripped


def build(captures: Path, batch_mark: int) -> tuple[list[str], dict]:
    index_path = captures / "index.jsonl"
    if not index_path.exists():
        sys.exit(f"no index.jsonl in {captures}")

    # The plugin writes index.jsonl with a UTF-8 BOM.
    rows = [json.loads(line) for line
            in index_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
            if line.strip()]
    have_wav = {p.name.split("_")[0] for p in captures.glob("*_sent.wav")}

    total_sec = sum(r.get("durationSec", 0) for r in rows)
    empty = sum(1 for r in rows if not (r.get("text") or "").strip())
    channels = Counter(r.get("channel") for r in rows)
    chan_desc = ", ".join(f"{n} on {c}" for c, n in channels.most_common())

    header = f"""\
# DRAFT ground truth for server/bench.py -- {captures.name}/ ({len(rows)} clips, {total_sec/60:.1f} min).
# Pre-filled from the plugin's own recorded transcriptions, exactly as references.txt was
# for 2026-07-27. This is NOT verified. Play each <id>_sent.wav in {captures.name}/ and fix
# each line to match what was actually said.
#
# Format: <4-digit id><TAB><correct transcript>
#
# !! IMPORTANT -- read before starting !!
# The pre-filled text is what the plugin displayed, i.e. whisper.cpp output *after* the
# proxy's correction pass. Two consequences:
#   1. If you skim and accept it rather than listening, the references end up biased in
#      whisper.cpp's favour, and any backend comparison built on them will flatter it.
#      Listen to every clip. Where the pre-fill is wrong it is often confidently wrong
#      (real example: "GH Nighting ale" for a vessel name).
#   2. Terms like "Callsign", "Maas Approach", "draught" and "buoy" appear already
#      normalised because a correction rule rewrote them. Usually right -- but check them
#      against the audio too, since a rule firing wrongly would bake the error into the
#      ground truth.
#
# On CH01 the plugin records the AIS-enriched display string, e.g.
# "[CALLAO EXPRESS/tanker] (MMSI:218839000) Callao Express, Maas Approach...". That
# prefix is not speech and has been stripped from the pre-fill here. If you spot one that
# survived, delete it -- a reference nobody said would corrupt the WER it feeds.
#
# When you genuinely can't make out what was said:
#   - Best guess but not sure: write it with a "?" right after, e.g. "Fjordstrom?" -- this
#     scores as a normal word, the "?" is just a note to yourself and is ignored by bench.py.
#   - Can't make out a word or phrase at all: write [inaudible] in its place, e.g.
#     "this is [inaudible], calling on channel one" -- bench.py excludes that span from
#     scoring rather than penalising the model for not saying "inaudible".
#   - Nothing intelligible, or you just want to skip the clip: leave the text after the TAB
#     empty. Clips with no usable reference are excluded from every aggregate, so a partly
#     finished file is perfectly usable -- there is no need to complete all {len(rows)}.
#
# Working through the first {batch_mark} in order already gives a solid set; references.txt
# has only 49 and has carried the project so far.
#
# {empty} clips had no transcript at all and are pre-filled empty -- likely noise or a clipped
# transmission. Listen before deciding; if there is speech, transcribe it.
#
# Channels: {chan_desc}.
#
# When done:
#   py bench.py --captures "{captures}" \\
#       --references <this file> --matrix groq_prompt \\
#       --host localhost --port 9000 --path /v1/audio/transcriptions
#"""

    lines = header.split("\n")
    missing = []
    for row in rows:
        clip_id = f"{row['index']:04d}"
        if clip_id not in have_wav:
            missing.append(clip_id)
            continue
        text = strip_enrichment(row.get("text") or "")
        for ch in ("\r", "\n", "\t"):
            text = text.replace(ch, " ")
        lines.append(f"{clip_id}\t{text.strip()}")
        if row["index"] + 1 == batch_mark:
            lines.append(f"# ---- {batch_mark} clips above: already a usable set. "
                         f"Everything below is optional. ----")

    stats = {"clips": len(rows), "written": sum(1 for l in lines if "\t" in l),
             "empty": empty, "missing_wav": missing}
    return lines, stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--captures", required=True, help="Capture directory containing index.jsonl")
    ap.add_argument("--out", required=True, help="Reference file to write")
    ap.add_argument("--batch-mark", type=int, default=100,
                    help="Insert a 'good stopping point' marker after this many clips")
    args = ap.parse_args()

    out = Path(args.out)
    if out.exists():
        sys.exit(f"{out} already exists -- refusing to overwrite hand-written references")

    lines, stats = build(Path(args.captures), args.batch_mark)
    # CRLF throughout, matching the existing references.txt.
    out.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8", newline="")

    print(f"wrote {out}")
    print(f"  clips in index : {stats['clips']}")
    print(f"  lines written  : {stats['written']}")
    print(f"  pre-filled empty: {stats['empty']}")
    if stats["missing_wav"]:
        print(f"  WARNING no _sent.wav for: {stats['missing_wav']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
