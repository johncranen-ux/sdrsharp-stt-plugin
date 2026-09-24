"""Score airband flight identification against a hand-labelled worksheet.

The maritime side has nine bench_*.py scripts and flight identification shipped with none, so
"what is our recall?" could only be answered by hand-diffing a plugin transcript against
`grep flight-id` in the proxy log, and the three proposed improvements (decoder biasing,
session identity register, physical corroboration) were all unfalsifiable. Building any of
them blind is what the maritime record argues against: three matching-layer changes there
measured as nulls, and a fourth was validated 8/8 on its sample and then fired zero times on
real data.

See docs/superpowers/specs/2026-09-10-airband-identification-measurement-design.md.

Usage:
    # score what happened live -- the historical record
    py bench_flight_identify.py --labels flight-labels-2026-09-10.txt

    # re-run extraction and matching over the aircraft that were actually in range
    py bench_flight_identify.py --labels flight-labels-2026-09-10.txt --replay
"""

import argparse
import datetime
import json
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVER_DIR))

# Polls are 15s apart, so 20s admits the snapshot before each transmission and nothing older.
SNAPSHOT_WINDOW_SEC = 20.0


def load_snapshots(path: Path) -> list[dict]:
    """Every aircraft snapshot in a side-car file, oldest first."""
    if not Path(path).exists():
        return []
    rows = [json.loads(line) for line in Path(path).open(encoding="utf-8") if line.strip()]
    return sorted(rows, key=lambda r: r["t"])


def load_transcripts(path: Path, config: str | None = None) -> dict[str, str]:
    """One arm's transcriptions from a bench-results JSON, keyed by clip id.

    `config` names which arm to read when a results file holds more than one; with a single
    arm it can be omitted, which is the common case since each run writes its own file.

    A row whose `error` is set is dropped, so the caller sees it as a MISSING clip. bench_stt
    writes a row for every clip it attempted, and a 429, a timeout or an unparseable body
    leaves `text` empty with `error` filled in. Reading that as an empty transcription would
    score a clip the API refused as a genuine extraction miss and as a row that wrote no QNH:
    an arm that lost ten clips would print several points worse than its control with nothing
    anywhere reporting the loss. Empty text with no error is the opposite case -- silence
    really did decode to nothing -- and is kept.
    """
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    results = payload["results"]
    if config is None:
        if len(results) != 1:
            raise SystemExit(f"{path} holds {sorted(results)} -- name one with --config")
        config = next(iter(results))
    return {row["clip_id"]: row.get("text") or ""
            for row in results[config] if not str(row.get("error") or "").strip()}


def join_snapshot(snapshots: list[dict], timestamp: str,
                  window_sec: float = SNAPSHOT_WINDOW_SEC) -> dict | None:
    """The snapshot in force at `timestamp`, or None if the corpus cannot answer.

    Nearest at-or-before only. A later poll can hold aircraft that had not arrived yet, and
    None must stay distinguishable from an empty snapshot: "we do not know what was in range"
    and "nothing was in range" are different claims and the second one is a scored outcome.
    """
    when = datetime.datetime.fromisoformat(timestamp)
    best = None
    for row in snapshots:
        taken = datetime.datetime.fromisoformat(row["t"])
        if taken > when:
            continue
        if (when - taken).total_seconds() <= window_sec:
            best = row
    return best


# -- the worksheet's two hand-written lines, as the scorer needs them -------------------

LABEL_NONE = "NONE"      # no aircraft is named in this transmission -- ATC chatter, readbacks
LABEL_UNSURE = "UNSURE"  # a callsign is spoken but the labeller could not commit; excluded

_SYSTEM_TAG = "identified "
_SYSTEM_EXTRACTED = "(extracted "


def read_label(raw: str, blank_means: str = LABEL_NONE) -> str:
    """One `aircraft :` line as a scorable verdict.

    A trailing `?` is the labeller hedging (`YZR7939?`), which is UNSURE however confident the
    stem looks -- scoring a hedge as a firm label would put an unearned row in the denominator.

    `blank_means` is the operator's 2026-09-10 call that an empty line means NONE, kept as a
    parameter rather than baked in because seven rows the system tagged were also left blank,
    and that reading turns all seven into wrong matches.
    """
    value = (raw or "").strip().upper()
    if not value:
        return blank_means
    if value.endswith("?"):
        return LABEL_UNSURE
    if value in (LABEL_NONE, LABEL_UNSURE):
        return value
    return value


def parse_system(line: str) -> tuple[str | None, str | None]:
    """(callsign tagged live, candidate extracted but unmatched) from a `system :` line.

    Both are None for a transmission the extractor found no callsign in at all -- which is a
    different miss from one where a candidate was produced and rejected, and the two are
    fixed by different work.
    """
    text = (line or "").strip()
    if text.startswith(_SYSTEM_TAG):
        return text[len(_SYSTEM_TAG):].strip().split("/")[0], None
    start = text.find(_SYSTEM_EXTRACTED)
    if start == -1:
        return None, None
    return None, text[start + len(_SYSTEM_EXTRACTED):].split(",")[0].strip()


# -- the outcome split ------------------------------------------------------------------
#
# Four causes, four different fixes, deliberately not blended into one recall number: a single
# figure hides which of them is binding, which is the only question this arm exists to answer.

CORRECT = "correct"                    # tagged, and it is the labelled aircraft
WRONG_MATCH = "wrong-match"            # tagged, but not the labelled aircraft -- precision
RETRIEVAL_MISS = "retrieval-miss"      # the aircraft was not in range; this is the ceiling
EXTRACTION_MISS = "extraction-miss"    # in range, but no candidate was produced at all
SELECTION_MISS = "selection-miss"      # a candidate was produced and matched nothing
CORRECT_REJECTION = "correct-rejection"  # nothing was named and nothing was tagged
EXCLUDED = "excluded"                  # UNSURE, or a miss the corpus cannot explain


def classify(label: str, tagged: str | None, candidate: str | None,
             in_range: set[str] | None) -> str:
    """Which bucket one labelled transmission falls in.

    `in_range` is None for "no snapshot covers this moment", which is not the same as an empty
    set. It is consulted only where it changes the answer: a tag can be judged against the
    label alone, so a correct identification is never thrown away for want of a snapshot.
    """
    if label == LABEL_UNSURE:
        return EXCLUDED
    if label == LABEL_NONE:
        return WRONG_MATCH if tagged else CORRECT_REJECTION
    if tagged:
        return CORRECT if tagged == label else WRONG_MATCH
    if in_range is None:
        return EXCLUDED
    if label not in in_range:
        return RETRIEVAL_MISS
    return EXTRACTION_MISS if candidate is None else SELECTION_MISS


def candidate_for(text: str) -> str | None:
    """What the live extractor makes of a transmission. Wraps the production function so the
    corpus's standing negative cases (runway designators, ATC chatter) are pinned here."""
    from stt_proxy import flight_identify
    return flight_identify.extract_callsign_candidate(text)


# -- scoring a whole worksheet ----------------------------------------------------------

import collections
import dataclasses
import re

import make_flight_labels
from stt_proxy import adsb, flight_identify


@dataclasses.dataclass
class Scored:
    index: int
    timestamp: str
    label: str
    tagged: str | None
    candidate: str | None
    bucket: str
    text: str          # the transcription this row was actually scored on
    in_range: bool


@dataclasses.dataclass
class Result:
    rows: list[Scored]
    counts: dict[str, int]
    precision: float | None
    recall: float | None
    achievable: int
    ceiling_missed: int
    excluded: int
    missing_transcripts: list[int]


def _timestamp_of(time_line: str) -> str:
    return (time_line or "").split()[0]


def _load_cache(snapshot: dict) -> None:
    """Put a snapshot's aircraft into the live cache, through the real poll path.

    Replay goes via adsb.poll_once with an injected fetch rather than writing the cache dict
    directly, so it exercises the same validate-map-replace code the proxy runs. The snapshot
    already holds mapped records and map_aircraft is idempotent over its own output.
    """
    payload = json.dumps({"aircraft": snapshot["aircraft"]}).encode("utf-8")
    adsb.poll_once(0.0, 0.0, 0.0, fetch=lambda _url: payload, record=False)


def score(worksheet: str, snapshots: list[dict], blank_means: str = LABEL_NONE,
          replay: bool = False, text_source: str = "machine",
          arm: "TailFirst | None" = None,
          transcripts: dict[str, str] | None = None) -> Result:
    """Every labelled transmission in a worksheet, bucketed.

    `replay` re-runs extraction and matching against the aircraft that were actually in range
    instead of reading what happened live; `text_source` picks which transcription it is run
    over, so the gap between "machine" and "heard" sizes what better ASR alone could buy.

    `arm` adds the tail-first fallback on top of the real matcher, which is why it requires
    replay: it is a counterfactual about code that does not exist, not a reading of history.

    `transcripts` scores a different transcription setting's own output for each clip, joined
    by clip id, instead of the worksheet's own `machine`/`heard` text -- also a counterfactual,
    and also requiring replay.
    """
    if arm is not None and not replay:
        raise ValueError("an arm is a counterfactual and needs --replay")
    if transcripts is not None and not replay:
        raise ValueError("scoring an arm's transcripts is a counterfactual and needs --replay")
    rows: list[Scored] = []
    missing: list[int] = []
    for record in make_flight_labels.parse_worksheet(worksheet):
        timestamp = _timestamp_of(record.get("time", ""))
        snapshot = join_snapshot(snapshots, timestamp) if timestamp else None
        in_range = ({a["flight"] for a in snapshot["aircraft"]}
                    if snapshot is not None else None)

        if replay:
            if snapshot is None:
                # Nothing is scored here -- no extraction runs without a snapshot -- but the
                # row still has to record the text this run was reading. Showing the
                # worksheet's own column instead would put text the arm never produced beside
                # an arm's row in --rows, on 17 of the 136 rows of the 2026-09-10 corpus.
                tagged = candidate = None
                text = (record.get(text_source, "") if transcripts is None
                        else transcripts.get(f"{record['index']:04d}", ""))
            else:
                _load_cache(snapshot)
                if transcripts is None:
                    text = record.get(text_source, "")
                else:
                    text = transcripts.get(f"{record['index']:04d}")
                if text is None:
                    missing.append(record["index"])
                    text, candidate, tagged = "", None, None
                    rows.append(Scored(
                        index=record["index"], timestamp=timestamp, label=LABEL_UNSURE,
                        tagged=None, candidate=None, bucket=EXCLUDED, text="",
                        in_range=False))
                    continue
                candidate = flight_identify.extract_callsign_candidate(text)
                matched = flight_identify.match_flight(candidate)
                if matched is None and arm is not None and (
                        candidate is None or arm.on_failed_candidate):
                    matched = match_by_tail(digit_runs(text), snapshot["aircraft"], arm)
                tagged = matched["flight"] if matched else None
        else:
            tagged, candidate = parse_system(record.get("system", ""))
            text = record.get("machine", "")

        label = read_label(record.get("aircraft", ""), blank_means)
        rows.append(Scored(
            index=record["index"], timestamp=timestamp, label=label, tagged=tagged,
            candidate=candidate, bucket=classify(label, tagged, candidate, in_range),
            text=text,
            in_range=bool(in_range and label in in_range)))

    counts = collections.Counter(r.bucket for r in rows if r.bucket != EXCLUDED)
    tags = sum(1 for r in rows if r.bucket in (CORRECT, WRONG_MATCH))
    # Rows that named an aircraft which really was in range -- the only misses anyone can fix.
    achievable = sum(1 for r in rows if r.in_range and r.bucket != EXCLUDED)
    return Result(
        rows=rows,
        counts=dict(counts),
        precision=counts[CORRECT] / tags if tags else None,
        recall=counts[CORRECT] / achievable if achievable else None,
        achievable=achievable,
        ceiling_missed=counts[RETRIEVAL_MISS],
        excluded=sum(1 for r in rows if r.bucket == EXCLUDED),
        missing_transcripts=missing,
    )


# -- arm: tail-first matching ------------------------------------------------------------
#
# ASR destroys the airline word and leaves the digits intact -- "Fox, Roscoe, one two bravo"
# for KLM12B, "ship blue three two" for JBU32. This arm asks whether the digits alone can find
# the aircraft. It is deliberately a bench arm and not a production path: matching on digits
# with no airline anchor is the BERGE TOWNSEND hole reopened, and the 95 NONE rows in the
# corpus -- headings, QNH readbacks, flight levels, frequencies -- are exactly the material
# that would exploit it. Measure the damage before believing the recall.

_RUN_MODES = ("all", "trailing")


@dataclasses.dataclass(frozen=True)
class TailFirst:
    min_tail: int = 2          # a one-character tail matches far too much
    unique: bool = True        # refuse a tail two in-range aircraft share
    include_ground: bool = False   # parked traffic at Schiphol, 36 km away, is not talking
    runs: str = "all"          # "all" maximal digit runs, or only the "trailing" one
    on_failed_candidate: bool = False  # also fire when extraction produced a candidate that
                                       # matched nothing, not only when it produced none


def digit_runs(text: str) -> list[str]:
    """Every maximal run of spoken digits/phonetic letters in a transmission.

    Decoding is flight_identify's own, boundary rules included, so a run here is exactly what
    production extraction would have appended after an airline anchor. Without that the arm
    would be measuring a second, subtly different decoder.
    """
    words = re.findall(r"[A-Za-z0-9]+", (text or "").lower())
    runs, current = [], ""
    for i, word in enumerate(words):
        peek = words[i + 1] if i + 1 < len(words) else None
        char = word if word.isdigit() else flight_identify._decode_digit_word(word, peek)
        if char is None:
            if current:
                runs.append(current)
            current = ""
            continue
        if peek in flight_identify._DIGIT_RUN_BOUNDARY_WORDS:
            # Production stops BEFORE this digit -- it opens an altitude or fraction reading
            # ("three two, two thousand"), so it belongs to neither the callsign nor the run.
            if current:
                runs.append(current)
            current = ""
            continue
        current += char
    if current:
        runs.append(current)
    return runs


def match_by_tail(runs: list[str], aircraft: list[dict], opts: TailFirst) -> dict | None:
    """The one aircraft in range whose tail is one of these runs, or None.

    None whenever the answer is not unique -- two aircraft sharing a tail, or two runs each
    finding a different aircraft. An ambiguous identification is worse than none at all.
    """
    if opts.runs == "trailing":
        runs = runs[-1:]

    fleet = [a for a in aircraft
             if opts.include_ground or a.get("alt_baro") != "ground"]
    hits: list[dict] = []
    for run in runs:
        if len(run) < opts.min_tail:
            continue
        found = [a for a in fleet
                 if (split := flight_identify._split_code_tail(a["flight"] or ""))
                 and split[1] == run]
        if not found:
            continue
        if len(found) > 1 and opts.unique:
            return None
        hits.extend(found)

    if not hits:
        return None
    if len({a["hex"] for a in hits}) > 1:
        return None
    return hits[0]


def integrity_warnings(worksheet: str) -> list[str]:
    """Rows whose `machine` line no longer produces the `system` line stored beside it.

    The generator derived `system` from the capture text, so the pair is a checksum: if they
    disagree, either the machine line was hand-edited (the labeller correcting the wrong line
    -- it happened three times on the first corpus) or the extractor has changed since the
    worksheet was made. Both make a replay-versus-live comparison meaningless, and both are
    invisible without this check.

    Rows the system tagged are skipped: `identified DAL73/A333` is the live record and cannot
    be recomputed from text at all.
    """
    warnings = []
    for record in make_flight_labels.parse_worksheet(worksheet):
        stored = (record.get("system") or "").strip()
        if not stored or stored.startswith(_SYSTEM_TAG):
            continue
        expected = make_flight_labels._system_line(record.get("machine", ""), None)
        if expected != stored:
            warnings.append(
                f"{record['index']:04d}: machine line does not produce its system line"
                f"  stored: {stored}  recomputed: {expected}")
    return warnings


# -- CLI --------------------------------------------------------------------------------

_LOGS = _SERVER_DIR / "logs"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _sweep(worksheet: str, snapshots: list[dict], blank_means: str, text_source: str) -> None:
    """Every tail-first variant, beside the unchanged matcher.

    The column that decides this is `wrong`, not `correct`: the arm matches on digits with no
    airline anchor, and the corpus is full of headings, QNH readbacks and flight levels that
    are digit runs naming no aircraft at all.
    """
    base = score(worksheet, snapshots, blank_means=blank_means, replay=True,
                 text_source=text_source)
    print(f"{'variant':44} {'correct':>7} {'wrong':>6} {'extract':>8} {'precision':>10} {'recall':>7}")

    def row(name: str, result: Result) -> None:
        print(f"  {name:42} {result.counts.get(CORRECT, 0):>7} "
              f"{result.counts.get(WRONG_MATCH, 0):>6} "
              f"{result.counts.get(EXTRACTION_MISS, 0):>8} "
              f"{_pct(result.precision):>10} {_pct(result.recall):>7}")

    row("(no arm -- current matcher)", base)
    for runs in _RUN_MODES:
        for min_tail in (2, 3, 4):
            for unique in (True, False):
                for ground in (False, True):
                    for failed in (False, True):
                        opts = TailFirst(min_tail=min_tail, unique=unique,
                                         include_ground=ground, runs=runs,
                                         on_failed_candidate=failed)
                        name = (f"{runs}/min{min_tail}"
                                f"{'/unique' if unique else '/ambiguous-ok'}"
                                f"{'/+ground' if ground else ''}"
                                f"{'/+failed-cand' if failed else ''}")
                        row(name, score(worksheet, snapshots, blank_means=blank_means,
                                        replay=True, text_source=text_source, arm=opts))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", required=True)
    ap.add_argument("--snapshots", help="default: logs/adsb-snapshots-<the worksheet's date>.jsonl")
    ap.add_argument("--replay", action="store_true",
                    help="re-run extraction and matching against the aircraft in range")
    # No argparse default: "not given" has to stay distinguishable from "given as machine",
    # or --text beside --transcripts cannot be refused.
    ap.add_argument("--text", choices=("machine", "heard"),
                    help="with --replay, which transcription to run over (default: machine)")
    ap.add_argument("--blank", choices=("none", "skip"), default="none",
                    help="what an empty aircraft line means (default: none, the operator's call)")
    ap.add_argument("--rows", action="store_true", help="list every miss, row by row")
    ap.add_argument("--sweep", action="store_true",
                    help="score the tail-first arm over its whole parameter grid and stop")
    ap.add_argument("--transcripts", help="a bench-results JSON to score instead of the "
                                          "worksheet's own machine text (implies --replay)")
    ap.add_argument("--config", help="which arm inside --transcripts to read")
    args = ap.parse_args()

    if args.transcripts and args.text:
        raise SystemExit(
            "--text picks a column of the worksheet and --transcripts replaces that column "
            "with an arm's own transcription -- they cannot both apply. Drop --text.")
    text_source = args.text or "machine"

    worksheet = Path(args.labels).read_text(encoding="utf-8")
    if args.snapshots:
        snap_path = Path(args.snapshots)
    else:
        day = _timestamp_of(
            next(r["time"] for r in make_flight_labels.parse_worksheet(worksheet) if r.get("time"))
        )[:10]
        snap_path = _LOGS / f"adsb-snapshots-{day}.jsonl"
    snapshots = load_snapshots(snap_path)
    if not snapshots:
        raise SystemExit(f"no aircraft snapshots in {snap_path} -- nothing can be bucketed")

    for warning in integrity_warnings(worksheet):
        print(f"CORPUS WARNING {warning}")

    blank_means = LABEL_NONE if args.blank == "none" else LABEL_UNSURE

    if args.sweep:
        _sweep(worksheet, snapshots, blank_means, text_source)
        return

    transcripts = (load_transcripts(Path(args.transcripts), args.config)
                   if args.transcripts else None)

    result = score(worksheet, snapshots, blank_means=blank_means,
                   replay=args.replay or transcripts is not None, text_source=text_source,
                   transcripts=transcripts)

    # The header is how a saved console log is identified months later, so it has to name the
    # source that was actually scored. Keying it off --replay alone printed "live record" for
    # an arm run and made an arm's log indistinguishable from the baseline it is compared to.
    if transcripts is not None:
        mode = f"replay over {Path(args.transcripts).name}"
        if args.config:
            mode += f" [{args.config}]"
    elif args.replay:
        mode = f"replay over the {text_source} text"
    else:
        mode = "live record"
    print(f"{len(result.rows)} transmissions   {mode}   snapshots: {snap_path.name}")
    print(f"blank aircraft line read as {blank_means}\n")

    for bucket in (CORRECT, WRONG_MATCH, EXTRACTION_MISS, SELECTION_MISS,
                   RETRIEVAL_MISS, CORRECT_REJECTION):
        print(f"  {bucket:18} {result.counts.get(bucket, 0):4}")
    print(f"  {'excluded':18} {result.excluded:4}\n")

    print(f"precision {_pct(result.precision)}   "
          f"(correct / every tag applied)")
    print(f"recall    {_pct(result.recall)}   "
          f"(correct / {result.achievable} rows naming an aircraft that was in range)")
    print(f"ceiling   {result.ceiling_missed} rows named an aircraft that was never in range")

    if result.missing_transcripts:
        print(f"\n{len(result.missing_transcripts)} clips missing from the arm and excluded: "
              f"{result.missing_transcripts}")

    if args.rows:
        print()
        for row in result.rows:
            if row.bucket in (CORRECT, CORRECT_REJECTION):
                continue
            print(f"  {row.index:04d} {row.bucket:18} label={row.label:10} "
                  f"tag={str(row.tagged):10} cand={str(row.candidate):10} {row.text[:60]}")


if __name__ == "__main__":
    main()
