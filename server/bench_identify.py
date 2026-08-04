"""Score vessel identification against hand-labelled conversations.

Transcription has had bench.py and a pooled WER figure since the beginning, and every change
to it was argued with numbers. Identification had nothing: the AIS matcher, the hint filter,
the resolver and its prompt were all changed on the strength of one-off scripts written to
chase whatever had just gone wrong, and thrown away afterwards. Two bugs found by hand on
2026-08-04 are what this exists to have caught -- PECHORA STAR losing an exact spelled-out
callsign to a 76.9 name match, and one THULELAND conversation resolving as three different
ships.

Scored per transmission, not per stored exchange. Over-segmentation is one of the failures
being measured: the THULELAND conversation produced three exchanges naming three ships, and
a per-exchange score would call that "one right, two wrong" while hiding that all five turns
belonged to one vessel.

Usage:
    # 1. bootstrap a labels file from what the resolver currently believes, then correct it
    py bench_identify.py --make-labels --out identification-labels.txt

    # 2. score what is on disk (free, no API calls) -- the historical record
    py bench_identify.py --labels identification-labels.txt

    # 3. re-run the resolver over the same conversations and score that (costs API calls)
    py bench_identify.py --labels identification-labels.txt --resolve

Mode 3 is the one that makes a resolver or prompt change measurable: run it, change
something, run it again, compare. Mode 2 only ever reports what already happened.
"""

import argparse
import datetime
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVER_DIR))

DEFAULT_CONVERSATIONS = _SERVER_DIR / "stt_proxy" / "conversations.json"
# Where the plugin writes its captures. Only days that still have audio can be labelled by
# ear, which is the whole reason to prefer them over correcting the resolver's own guesses.
DEFAULT_CAPTURES = os.environ.get(
    "STT_CAPTURES_DIR", r"D:\SDR\SdrSharp\Plugins\SttPlugin\captures")
_TS_FMT = "%Y-%m-%d %H:%M:%S"


# ---------------------------------------------------------------------------
# Labels
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Label:
    start: datetime.datetime
    end: datetime.datetime
    mmsi: str | None          # None means nobody was identifiable from the audio
    note: str = ""
    channel: str = "160,650"

    @property
    def identifiable(self) -> bool:
        return self.mmsi is not None


def _resolve_expected(value: str, lookup: dict | None, where: str) -> str | None:
    """Turn the third field into an MMSI. Accepts an MMSI or a vessel name.

    A name is what you hear on the recording; an MMSI is a number you would have to go and
    look up for every line. The name is resolved by EXACT cache key and never fuzzily -- a
    fuzzy resolution here would build the matcher being measured into its own ground truth.
    """
    if value in ("-", ""):
        return None
    if value.isdigit():
        return value
    if lookup is None:
        raise ValueError(f"{where}: {value!r} is a vessel name, which needs the AIS cache to "
                         f"resolve -- run bench_identify.py rather than calling parse_labels bare")
    mmsi = lookup.get(value.upper())
    if not mmsi:
        raise ValueError(f"{where}: {value!r} is not in the AIS cache. Use its MMSI directly, "
                         f"or check the spelling against /api/ais-cache")
    return mmsi


def parse_labels(path, lookup: dict | None = None) -> list[Label]:
    """Read a ground-truth file: '#' comments, then <start> TAB <end> TAB <mmsi|name|-> [TAB note].

    A malformed line raises rather than being skipped. Dropping one silently would shrink
    the corpus and flatter every score computed from it, which is the failure mode a
    benchmark can least afford.
    """
    labels: list[Label] = []
    for lineno, raw in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            raise ValueError(f"{path}:{lineno}: expected at least 3 tab-separated fields, "
                             f"got {len(parts)}: {raw!r}")
        start_s, end_s, expected_s = (p.strip() for p in parts[:3])
        note = parts[3].strip() if len(parts) > 3 else ""
        try:
            start = datetime.datetime.strptime(start_s, _TS_FMT)
            end   = datetime.datetime.strptime(end_s, _TS_FMT)
        except ValueError as exc:
            raise ValueError(f"{path}:{lineno}: {exc}") from exc
        mmsi = _resolve_expected(expected_s, lookup, f"{path}:{lineno}")
        labels.append(Label(start, end, mmsi, note))
    return labels


def load_capture_index(captures_dir, day: str) -> list[tuple[str, str, str]]:
    """(timestamp, clip id, channel) for one capture day, so a label can name its audio.

    The plugin writes index.jsonl with a UTF-8 BOM, hence utf-8-sig.
    """
    path = Path(captures_dir) / day / "index.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
            stamp = entry["timestamp"][:19].replace("T", " ")
        except (json.JSONDecodeError, KeyError):
            continue
        out.append((stamp, f"{int(entry.get('index', 0)):04d}", entry.get("channel", "")))
    return out


def make_labels(exchanges: list[dict], days: set | None = None,
                clips: list[tuple[str, str, str]] | None = None) -> list[str]:
    """A draft labels file built from what the resolver currently believes.

    Same principle as make_references.py: correcting a draft while listening beats typing
    one from scratch. Every line still has to be checked -- a draft that is simply accepted
    scores the resolver against itself and reports 100%.

    The vessel is drafted as a NAME rather than an MMSI so a wrong line is corrected by
    typing what you hear, and `clips` names the wav files covering each conversation so
    there is something to hear.
    """
    lines = [
        "# Identification ground truth. One line per REAL conversation, not per stored",
        "# exchange -- if the resolver split one conversation in three, that is ONE line here",
        "# and the split is what gets measured.",
        "#",
        "# <start>\t<end>\t<vessel name, MMSI, or - >\t<note>",
        "#",
        "# HOW TO CORRECT A LINE",
        "#   1. Play the wav files named at the end of the line, in order.",
        "#   2. Decide who was actually speaking across the WHOLE conversation.",
        "#   3. Put that in field 3: the vessel name as AIS spells it, or its MMSI.",
        "#      Use '-' when nobody could be identified from the audio -- that is a real",
        "#      answer, and it asserts that naming anyone at all is wrong.",
        "#   4. Delete any line you are not sure about. An unlabelled conversation is simply",
        "#      not scored; a guessed one corrupts every number computed from this file.",
        "#",
        "# Field 3 is drafted from the resolver's own verdict, so CHECK EVERY LINE: a draft",
        "# accepted unread scores the resolver against itself and reports 100%.",
    ]
    for ex in sorted(exchanges, key=lambda e: e.get("start", "")):
        start, end = ex.get("start", ""), ex.get("end", "")
        if days and start[:10] not in days:
            continue
        turns = ex.get("turns") or []
        first = (turns[0].get("text") or "")[:55].replace("\t", " ") if turns else ""
        wavs  = ""
        if clips:
            ids = [cid for stamp, cid, _ch in clips if start <= stamp <= end]
            if ids:
                wavs = "  [" + " ".join(f"{i}_sent.wav" for i in ids) + "]"
        note = f"{ex.get('vessel') or 'unidentified'} | {first}{wavs}"
        lines.append(f"{start}\t{end}\t{ex.get('vessel') or '-'}\t{note}")
    return lines


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def _turn_times(exchange: dict) -> list[datetime.datetime]:
    """Absolute timestamps for an exchange's turns, which store only HH:MM:SS.

    The date comes from the exchange's own start. An exchange running over midnight would
    need more than this, but a window closes after 60s of quiet, so one cannot.
    """
    try:
        day = datetime.datetime.strptime(exchange.get("start", ""), _TS_FMT).date()
    except ValueError:
        return []
    out = []
    for turn in exchange.get("turns") or []:
        try:
            clock = datetime.datetime.strptime(turn.get("time", ""), "%H:%M:%S").time()
        except ValueError:
            continue
        out.append(datetime.datetime.combine(day, clock))
    return out


def score(labels: list[Label], exchanges: list[dict]) -> dict:
    """Per-transmission identification accuracy, plus how badly conversations were split."""
    correct = wrong = missed = correct_null = 0
    fragments = 0
    labels_with_no_turns = 0
    exchange_counts: list[int] = []

    for label in labels:
        covering: dict[int, dict] = {}
        turns_here = 0
        for idx, ex in enumerate(exchanges):
            if ex.get("channel", "160,650") != label.channel:
                continue
            for when in _turn_times(ex):
                if label.start <= when <= label.end:
                    turns_here += 1
                    covering[idx] = ex
                    predicted = ex.get("mmsi")
                    if label.identifiable:
                        if predicted is None:
                            missed += 1
                        elif str(predicted) == label.mmsi:
                            correct += 1
                        else:
                            wrong += 1
                    else:
                        # Nobody was identifiable, so naming anyone at all is an error.
                        if predicted is None:
                            correct_null += 1
                        else:
                            wrong += 1

        if turns_here == 0:
            labels_with_no_turns += 1
            continue
        exchange_counts.append(len(covering))
        if len(covering) > 1:
            fragments += 1

    named = correct + wrong
    identifiable_turns = correct + wrong + missed
    return {
        "conversations": len(labels),
        "scored_turns": correct + wrong + missed + correct_null,
        "correct": correct,
        "wrong": wrong,
        "missed": missed,
        "correct_null": correct_null,
        "precision": (correct / named) if named else None,
        "recall": (correct / identifiable_turns) if identifiable_turns else None,
        "fragments": fragments,
        "exchanges_per_conversation": (
            sum(exchange_counts) / len(exchange_counts)) if exchange_counts else None,
        "labels_with_no_turns": labels_with_no_turns,
    }


# ---------------------------------------------------------------------------
# Re-resolving, so a resolver change can actually be measured
# ---------------------------------------------------------------------------

def _rebuild_window(exchange: dict, seq: int) -> list[dict]:
    """Journal chunks equivalent to what the reaper handed the resolver for this exchange.

    `live_mmsi` is recovered by name because it was never stored per turn -- the live vessel
    name already is a cache key, so this is a lookup rather than a re-match. Callsigns need
    no recovery: the resolver decodes those from the transmission text itself.
    """
    from stt_proxy import ais

    day = datetime.datetime.strptime(exchange["start"], _TS_FMT).date()
    chunks = []
    for turn in exchange.get("turns") or []:
        clock = datetime.datetime.strptime(turn["time"], "%H:%M:%S").time()
        live  = turn.get("live_vessel")
        entry = ais._vessel_cache.get(live.upper()) if live else None
        seq += 1
        chunks.append({
            "id": seq,
            "time": datetime.datetime.combine(day, clock),
            "channel": exchange.get("channel", "160,650"),
            # Raw, as the reaper journals it -- corrections mask the evidence the resolver needs.
            "text": turn.get("raw") or turn.get("text", ""),
            "corrected": turn.get("text", ""),
            "live_vessel": live,
            "live_mmsi": (entry or {}).get("mmsi"),
            "callsign": None,
        })
    return chunks


def _resolve_again(exchanges: list[dict], labels: list[Label]) -> list[dict]:
    """Re-run the resolver over the transmissions each labelled conversation covers.

    Grouped by label rather than by stored exchange: a conversation the resolver previously
    split must be handed back to it whole, or the rerun would inherit the very segmentation
    it is being measured on.
    """
    from stt_proxy.conversations import resolve_conversation

    out: list[dict] = []
    seq = 0
    for label in labels:
        window: list[dict] = []
        for ex in exchanges:
            if ex.get("channel", "160,650") != label.channel:
                continue
            if any(label.start <= w <= label.end for w in _turn_times(ex)):
                window.extend(_rebuild_window(ex, seq))
                seq += len(ex.get("turns") or [])
        if not window:
            continue
        window.sort(key=lambda c: c["time"])
        by_id = {c["id"]: c for c in window}
        for ex in resolve_conversation(window):
            turns = [by_id[i] for i in ex["chunk_ids"] if i in by_id]
            if not turns:
                continue
            out.append({
                "vessel": ex.get("vessel"), "mmsi": ex.get("mmsi"),
                "channel": turns[0]["channel"],
                "start": turns[0]["time"].strftime(_TS_FMT),
                "end": turns[-1]["time"].strftime(_TS_FMT),
                "turns": [{"time": t["time"].strftime("%H:%M:%S"),
                           "text": t.get("corrected") or t["text"]} for t in turns],
            })
    return out


# ---------------------------------------------------------------------------

def _pct(value) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def print_report(result: dict, label: str) -> None:
    print(f"\n{label}")
    print(f"  conversations          {result['conversations']}")
    print(f"  transmissions scored   {result['scored_turns']}")
    print(f"    correct              {result['correct']}")
    print(f"    wrong                {result['wrong']}")
    print(f"    missed               {result['missed']}   (identifiable, named nobody)")
    print(f"    correctly unnamed    {result['correct_null']}")
    print(f"  precision              {_pct(result['precision'])}   (of transmissions given a name)")
    print(f"  recall                 {_pct(result['recall'])}   (of identifiable transmissions)")
    epc = result["exchanges_per_conversation"]
    print(f"  exchanges/conversation {'n/a' if epc is None else f'{epc:.2f}'}"
          f"   ({result['fragments']} conversation(s) split across more than one)")
    if result["labels_with_no_turns"]:
        print(f"  !! {result['labels_with_no_turns']} label(s) matched no stored transmission")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversations", default=str(DEFAULT_CONVERSATIONS),
                    help="resolved conversation store (default: stt_proxy/conversations.json)")
    ap.add_argument("--labels", help="ground-truth file from --make-labels, corrected by hand")
    ap.add_argument("--make-labels", action="store_true",
                    help="write a draft labels file from the current verdicts and exit")
    ap.add_argument("--out", help="where --make-labels writes (default: stdout)")
    ap.add_argument("--day", action="append", metavar="YYYY-MM-DD",
                    help="only draft conversations from this day; repeatable. Worth using: "
                         "only days whose capture audio still exists can be labelled by ear")
    ap.add_argument("--captures", default=DEFAULT_CAPTURES,
                    help="capture root, so each drafted line names the wav files to play "
                         f"(default: {DEFAULT_CAPTURES})")
    ap.add_argument("--resolve", action="store_true",
                    help="re-run the resolver and score that instead of the stored verdicts "
                         "(makes API calls)")
    ap.add_argument("--out-json", help="write the scores as JSON, for comparing two runs")
    args = ap.parse_args()

    try:
        exchanges = json.loads(Path(args.conversations).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _fail(f"no conversation store at {args.conversations}")

    if args.make_labels:
        days = set(args.day) if args.day else None
        clips: list[tuple[str, str, str]] = []
        for day in sorted(days or {e.get("start", "")[:10] for e in exchanges if e.get("start")}):
            clips.extend(load_capture_index(args.captures, day))
        lines = make_labels(exchanges, days=days, clips=clips)
        text = "\n".join(lines) + "\n"
        drafted = sum(1 for ln in lines if not ln.startswith("#"))
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"wrote {drafted} draft labels to {args.out} "
                  f"({len(clips)} clips indexed) -- correct them before use", file=sys.stderr)
        else:
            print(text, end="")
        return 0

    if not args.labels:
        return _fail("--labels is required (build one with --make-labels first)")
    # Labels may name a vessel rather than an MMSI, which needs the cache to resolve.
    from stt_proxy import ais
    ais._load_cache()
    lookup = {name: entry.get("mmsi") for name, entry in ais._vessel_cache.items()}
    labels = parse_labels(args.labels, lookup=lookup)
    if not labels:
        return _fail(f"no labels in {args.labels}")

    result = score(labels, exchanges)
    print_report(result, f"STORED VERDICTS  ({args.conversations})")

    if args.resolve:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            return _fail("--resolve needs ANTHROPIC_API_KEY")
        from stt_proxy import ais
        ais._load_cache()
        rerun = score(labels, _resolve_again(exchanges, labels))
        print_report(rerun, "RE-RESOLVED NOW")
        result = {"stored": result, "resolved": rerun}

    if args.out_json:
        Path(args.out_json).write_text(json.dumps(result, indent=1), encoding="utf-8")
    return 0


def _fail(message: str) -> int:
    print(f"error: {message}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
