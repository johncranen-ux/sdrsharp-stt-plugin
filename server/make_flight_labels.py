"""Build a hand-labelling worksheet for airband flight identification.

Flight identification shipped with no way to score it -- nine bench_*.py scripts exist for the
maritime side and none for flights -- so every proposed improvement is unfalsifiable. This
produces the corpus half of the fix: one block per airband transmission, pre-filled from the
plugin's capture index, with the clip filename so every row is checkable against what was
actually said rather than against what the decoder claimed.

See docs/superpowers/specs/2026-09-10-airband-identification-measurement-design.md.

Usage:
    # generate a blank worksheet (refuses to write over an existing file)
    py make_flight_labels.py --captures "D:\\SDR\\...\\captures\\2026-09-10" \\
                             --out flight-labels-2026-09-10.txt

    # AFTER labelling it by hand: export the `heard` lines as a WER reference file
    py make_flight_labels.py --from-worksheet flight-labels-2026-09-10.txt \\
                             --references references-airband-2026-09-10.txt
"""

import argparse
import json
import re
from pathlib import Path

from stt_proxy import flight_identify

# [KLM281] or [DAL73/A333] -- anything else at the start of a line is transcript text and must
# survive verbatim. Stripping a non-tag bracket would silently corrupt a label.
_TAG_RE = re.compile(r"^\[([A-Z0-9]+(?:/[A-Z0-9]+)?)\]\s*")

# Airband is 118.000-136.975 MHz; CH01 maritime is 160.650. Deciding by frequency range rather
# than by importing APPROACH_TOWER_CHANNELS keeps this script standalone, and correctly keeps
# a mixed-band capture day from putting vessels in an aircraft worksheet.
_AIRBAND_LO, _AIRBAND_HI = 118.0, 137.0

_FIELD_RE = re.compile(r"^(\w+)\s*:\s?(.*)$")
_BLOCK_RE = re.compile(r"^--- (\d+) -+$")


def is_airband(channel: str) -> bool:
    try:
        megahertz = float((channel or "").replace(",", "."))
    except ValueError:
        return False
    return _AIRBAND_LO <= megahertz < _AIRBAND_HI


def strip_tag(text: str) -> tuple[str, str | None]:
    """(text without the identification tag, the tag) -- the tag is absent for most rows."""
    match = _TAG_RE.match(text or "")
    if not match:
        return text, None
    return text[match.end():], match.group(1)


def airband_rows(rows: list[dict]) -> list[dict]:
    return [r for r in rows if is_airband(r.get("channel", ""))]


def _system_line(text: str, tag: str | None) -> str:
    """What the system actually did with this transmission.

    The tag is authoritative -- it is what the plugin displayed at the time. The extracted
    candidate is recomputed here rather than read from the proxy log, which only prints the
    near-miss diagnostic to the console and never to a file the worksheet could read.
    """
    if tag:
        return f"identified {tag}"
    candidate = flight_identify.extract_callsign_candidate(text)
    if candidate is None:
        return "not identified  (no callsign extracted)"
    return f"not identified  (extracted {candidate}, no match)"


def render_block(row: dict) -> str:
    text, tag = strip_tag(row.get("text", ""))
    index = int(row["index"])
    return (
        f"--- {index:04d} " + "-" * 55 + "\n"
        f"audio    : {index:04d}_sent.wav   (raw: {index:04d}_raw.wav)\n"
        f"time     : {row.get('timestamp', '')}   "
        f"channel: {row.get('channel', '')}   {row.get('durationSec', 0):.1f}s\n"
        f"machine  : {text}\n"
        f"system   : {_system_line(text, tag)}\n"
        "\n"
        f"heard    : {text}\n"
        "aircraft :\n"
    )


HEADER = """\
=======================================================================
AIRBAND FLIGHT IDENTIFICATION -- LABELLING WORKSHEET
=======================================================================

Fill in two lines per block. Everything else is context, leave it alone.

  heard    : correct this against the audio clip named on the `audio` line.
             It is PRE-FILLED with the machine transcription, which BIASES you
             toward accepting the machine's wording -- that bias is the price of
             not transcribing an hour of radio by hand. Please actually listen.

  aircraft : the aircraft you believe this transmission is, one of
               <CALLSIGN>  e.g. DAL162, KLM1576, EIN611
               NONE        no aircraft is named here (ATC chatter, bare readbacks)
               UNSURE      a callsign is spoken but you cannot make it out
             Leave BLANK if you have not judged this row yet. Blank and NONE mean
             different things and are scored differently -- blank is "not labelled",
             NONE is "nothing to identify".

To check what was actually in range at a given moment:
    py adsb_find.py 162 --at 11:23:14

Do not renumber, reorder or delete blocks.
=======================================================================

"""


def render_worksheet(rows: list[dict]) -> str:
    return HEADER + "\n".join(render_block(r) for r in rows)


def parse_worksheet(text: str) -> list[dict]:
    """The filled-in worksheet as records. Round-trips with render_worksheet."""
    records: list[dict] = []
    current: dict | None = None
    for line in text.splitlines():
        block = _BLOCK_RE.match(line)
        if block:
            current = {"index": int(block.group(1)), "heard": "", "aircraft": ""}
            records.append(current)
            continue
        if current is None:
            continue
        field = _FIELD_RE.match(line)
        if field and field.group(1) in ("machine", "system", "heard", "aircraft", "audio", "time"):
            current[field.group(1)] = field.group(2).strip()
    return records


def to_references(worksheet: str) -> str:
    """The worksheet's corrected `heard` lines as a bench.load_references file.

    A `?` the labeller wrote is an unintelligible word, not a word they transcribed as "?".
    It is emitted as `[inaudible]`, which bench._normalize already strips, so it costs no WER
    against any arm rather than counting as one wrong word against all of them.

    A row with an empty `heard` line emits nothing: bench treats a missing reference as
    "excluded from aggregates", which is what an unlabelled clip deserves.

    CAVEAT -- THE OUTPUT IS NOT AN INDEPENDENT REFERENCE SET. The `heard` lines are exported
    to the worksheet PRE-FILLED with the machine transcription and the operator corrects only
    what they hear differently: on the 2026-09-10 corpus 51 of the 126 exported references are
    byte-identical to the decoder's own output. A WER computed against this file therefore
    partly scores the decoder against itself, and it flatters whichever arm produced the
    pre-fill above all others. This is exactly why bench_word_production.py exists and is the
    prompt measurement's primary metric: counting word production needs no reference at all.
    Use these references for eyeballing individual rows, not for a headline WER.
    """
    lines = []
    for record in parse_worksheet(worksheet):
        heard = (record.get("heard") or "").strip()
        if not heard:
            continue
        text = heard.replace("?", "[inaudible]").replace("\t", " ")
        lines.append(f"{record['index']:04d}\t{text}")
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--captures", help="a dated capture directory (generate mode)")
    ap.add_argument("--out", help="where to write the new worksheet (generate mode)")
    ap.add_argument("--from-worksheet", dest="from_worksheet",
                    help="an EXISTING labelled worksheet to export references from; "
                         "generates nothing and writes nothing but --references")
    ap.add_argument("--references", help="with --from-worksheet, where to write the WER "
                                         "reference file built from its `heard` lines")
    args = ap.parse_args()

    # Export mode. Separated from generation because combining them is what produced
    # self-referential references: the old --references regenerated the worksheet first, so
    # every exported `heard` line was the decoder's own output scored against itself.
    if args.from_worksheet:
        if not args.references:
            raise SystemExit("--from-worksheet needs --references <path> to write to")
        if args.captures or args.out:
            raise SystemExit(
                "--from-worksheet only reads an existing worksheet; drop --captures/--out "
                "(nothing is generated in this mode)")
        text = to_references(Path(args.from_worksheet).read_text(encoding="utf-8"))
        Path(args.references).write_text(text + "\n", encoding="utf-8")
        print(f"-> {args.references} ({len(text.splitlines())} references "
              f"from {args.from_worksheet})")
        return

    if not args.captures or not args.out:
        raise SystemExit(
            "generating a worksheet needs both --captures and --out (to export references "
            "from a worksheet you already have, use --from-worksheet <path> --references "
            "<path>)")
    if args.references:
        raise SystemExit(
            "--references cannot be combined with generation: the worksheet it would export "
            "from is the one just generated, so every reference would be the decoder's own "
            "output scored against itself. Generate first, label by hand, then run "
            "--from-worksheet <the labelled file> --references <path>")
    if Path(args.out).exists():
        raise SystemExit(
            f"refusing to overwrite {args.out}: it already exists, and if it is a labelled "
            f"worksheet this would destroy the hand labelling. Write to a new path, or "
            f"delete that file yourself if you are certain it holds nothing.")

    index_path = Path(args.captures) / "index.jsonl"
    # utf-8-sig: the plugin writes a BOM, which json.loads will not accept on the first line.
    rows = [json.loads(line) for line in index_path.open(encoding="utf-8-sig") if line.strip()]
    kept = airband_rows(rows)

    Path(args.out).write_text(render_worksheet(kept), encoding="utf-8")
    tagged = sum(1 for r in kept if strip_tag(r.get("text", ""))[1])
    print(f"{len(kept)} airband transmissions ({len(rows) - len(kept)} skipped as non-airband)")
    print(f"{tagged} already identified by the system; {len(kept) - tagged} to judge")
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
