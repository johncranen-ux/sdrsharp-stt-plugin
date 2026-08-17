"""Flag resolver picks that cannot physically draw the draught spoken in the same exchange.

A merchant hull's length-to-draught ratio runs roughly 15-30. When a vessel states
"maximum draught six decimal three" and the resolver has named a 20 m pleasure craft, the
ratio is 3.2 and one of the two is false -- either the vessel is wrong or the draught was
mis-transcribed.

On the 2026-08-12 sample (25 conversations with a named vessel, a spoken draught and a known
hull length) the two populations did not overlap: 8 cases fell at 7.9 or below, 17 at 20.0 or
above, and the span between was empty. So the threshold is not a tuned parameter -- anything
from 8 to 19 gives the same answer. The user then labelled all 8 by ear and by callsign, and
every one was a WRONG VESSEL; none was a mis-heard draught.

USE LENGTH, NOT CACHED DRAUGHT. Cached draught is master-entered and frequently stale or 0.0
("not reported"). Length is physical and stable. Draught-mismatch alone would be a bad rule:
SUAPE EXPRESS spoke 6.9 against a cached 9.0 on a 293 m tanker and was correct.

This script exists because the original analysis did not. The 8-of-25 figure was produced by a
one-off that was never saved, which left the measurement unreproducible on any new corpus --
exactly the pattern bench_identify.py was written to end.

--- ON NOT CONTAMINATING GROUND TRUTH ---------------------------------------------------
Default output is aggregate only. The per-conversation detail names vessels the check
believes are wrong, and reading that before labelling a corpus by ear would bias the labels
it is meant to be judged against. Label first, then run with --detail.

Usage:
    py draught_check.py --conversations conversations-2026-08-13_14.json \
        --day 2026-08-13 --day 2026-08-14
    py draught_check.py ... --detail --out draught-flags-2026-08-13_14.txt
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Ratio below which a hull cannot draw the stated draught. The measured gap runs 7.9 to 20.0,
# so this sits in empty space rather than on a slope.
IMPLAUSIBLE_LT = 8.0
# A stated draught outside this range is not a draught -- it is an ETA, a channel or a
# position that happened to follow the keyword.
MIN_DRAUGHT_M, MAX_DRAUGHT_M = 0.5, 30.0
# How many tokens after the keyword may still hold the number.
_WINDOW = 10

_UNITS = {"zero": 0, "oh": 0, "one": 1, "two": 2, "three": 3, "four": 4,
          "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9}
_TEENS = {"ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
          "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19}
_TENS = {"twenty": 20, "thirty": 30}
_SEP = {"point", "decimal", "comma"}

# "draft" is the spelling whisper produces; corrections.py rewrites it to "draught", so a
# corpus holds both and matching only one silently halves the sample -- which is exactly what
# happened on the first pass (8 hits instead of 30). "drop" is the channel's common garble,
# as in "Maxim drop ten decimal eight", and is only trusted next to a max/maximum.
_KEYWORD_RE = re.compile(r"\b(dra[uf]ght|draft|(?<=max )drop|(?<=maximum )drop)\b", re.I)
_TOKEN_RE = re.compile(r"\d+(?:[.,]\d+)?|[a-z]+", re.I)


def _spoken_to_number(tokens: list[str]) -> float | None:
    """Read a spoken number: 'seven point nine', 'one seven decimal seven two', '5.85'."""
    whole: list[int] = []
    frac: list[int] = []
    seen_sep = False
    consumed = False

    for i, tok in enumerate(tokens):
        low = tok.lower()
        nxt = tokens[i + 1].lower() if i + 1 < len(tokens) else ""
        if seen_sep and low in _UNITS and nxt in _SEP:
            # This digit is the whole part of a repeat, not another decimal place:
            # "five decimal three, FIVE decimal three". Stop before swallowing it.
            break
        if re.fullmatch(r"\d+(?:[.,]\d+)?", tok):
            # A written number ends the read immediately -- it is already complete.
            return float(tok.replace(",", "."))
        if low in _SEP:
            if not consumed:      # a separator with no digits before it is not our number
                return None
            if seen_sep:
                # A second separator means the speaker has started the figure again --
                # "five decimal three, five decimal three" is one draught read twice, the
                # standard readback on this channel. Without this the run continues across
                # the repeat and yields 5.353, inflating the draught and pushing L/T DOWN,
                # which is the direction that invents false flags.
                break
            seen_sep = True
            continue
        target = frac if seen_sep else whole
        if low in _UNITS:
            target.append(_UNITS[low])
            consumed = True
        elif low in _TEENS and not seen_sep and not whole:
            # "eleven point six" -- a teen only makes sense as the whole part.
            whole.append(_TEENS[low])
            consumed = True
        elif low in _TENS and not seen_sep and not whole:
            whole.append(_TENS[low])
            consumed = True
        elif consumed:
            break                 # a non-number word ends the run
        else:
            continue              # still scanning towards the number

    if not whole:
        return None
    # Digit words spoken separately are concatenated, not summed: "one seven" is 17, and
    # "one seven decimal seven two" is 17.72. A single teen/tens word stands as its value.
    value = float(whole[0]) if len(whole) == 1 else float("".join(str(d) for d in whole))
    if frac:
        value += float("0." + "".join(str(d) for d in frac))
    return value


def spoken_draughts(text: str) -> list[float]:
    """Every plausible draught stated in this text, in order."""
    out = []
    for m in _KEYWORD_RE.finditer(text):
        tail = _TOKEN_RE.findall(text[m.end():])[:_WINDOW]
        value = _spoken_to_number(tail)
        if value is not None and MIN_DRAUGHT_M <= value <= MAX_DRAUGHT_M:
            out.append(value)
    return out


def check(exchanges: list[dict], days: set[str] | None = None) -> dict:
    """Partition a conversation store by whether the physical check can be applied."""
    rows, no_draught, no_length, unnamed = [], 0, 0, 0
    for e in exchanges:
        start = str(e.get("start") or "")
        if days and start[:10] not in days:
            continue
        text = " ".join(t.get("text") or t.get("raw") or "" for t in e.get("turns") or [])
        said = spoken_draughts(text)
        if not said:
            no_draught += 1
            continue
        if not e.get("vessel"):
            unnamed += 1
            continue
        length = e.get("length")
        if not length:
            no_length += 1
            continue
        draught = max(said)
        rows.append({
            "start": start, "end": e.get("end"), "vessel": e.get("vessel"),
            "mmsi": e.get("mmsi"), "callsign": e.get("callsign"), "type": e.get("type"),
            "length": length, "cached_draught": e.get("draught"),
            "said": draught, "lt": length / draught,
            "confidence": e.get("confidence"), "evidence": e.get("evidence"),
        })
    rows.sort(key=lambda r: r["lt"])
    return {"scored": rows,
            "flagged": [r for r in rows if r["lt"] < IMPLAUSIBLE_LT],
            "no_draught": no_draught, "no_length": no_length, "unnamed": unnamed}


def _summary(res: dict) -> str:
    rows, flagged = res["scored"], res["flagged"]
    total = len(rows) + res["no_draught"] + res["no_length"] + res["unnamed"]
    out = [
        f"conversations considered      : {total}",
        f"  no draught spoken           : {res['no_draught']}",
        f"  draught spoken, no vessel   : {res['unnamed']}",
        f"  draught + vessel, no length : {res['no_length']}",
        f"  SCORABLE                    : {len(rows)}",
        "",
        f"flagged as physically impossible (L/T < {IMPLAUSIBLE_LT}): {len(flagged)}"
        + (f" of {len(rows)}" if rows else ""),
    ]
    if rows:
        below = [r["lt"] for r in rows if r["lt"] < IMPLAUSIBLE_LT]
        above = [r["lt"] for r in rows if r["lt"] >= IMPLAUSIBLE_LT]
        out.append("")
        out.append("  L/T distribution -- the gap between the two groups is what makes the")
        out.append("  threshold safe. A populated middle would mean it is now a tuned knob.")
        if below:
            out.append(f"    impossible : {len(below):3}   max {max(below):5.1f}")
        if above:
            out.append(f"    normal     : {len(above):3}   min {min(above):5.1f}")
        if below and above:
            out.append(f"    GAP        : {max(below):.1f} .. {min(above):.1f}")
        hi = sum(1 for r in flagged if r.get("confidence") == "high")
        if flagged:
            out.append(f"    of the flagged, {hi} are HIGH confidence")
    return "\n".join(out)


def _detail(res: dict) -> str:
    out = ["", "=" * 78,
           "PER-CONVERSATION DETAIL -- do not read before labelling this corpus by ear.",
           "=" * 78]
    for r in res["flagged"]:
        out += [
            "",
            f"{r['start']} - {r['end']}",
            f"  PICKED   : {r['vessel']}  [{r['confidence']} confidence]",
            f"             MMSI {r['mmsi']}  callsign {r['callsign']}  {r['type']}",
            f"             {r['length']} m, cached draught {r['cached_draught']} m",
            f"  SAID     : draught {r['said']} m      L/T = {r['lt']:.1f}",
            f"  RESOLVER : {(r['evidence'] or '')[:150]}",
        ]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conversations", default="stt_proxy/conversations.json")
    ap.add_argument("--day", action="append", metavar="YYYY-MM-DD")
    ap.add_argument("--detail", action="store_true",
                    help="include per-conversation detail; biases ear-labelling, so run it "
                         "only after the corpus is labelled")
    ap.add_argument("--out", help="write to this file instead of stdout")
    args = ap.parse_args(argv)

    path = Path(args.conversations)
    if not path.exists():
        print(f"no conversation store at {path}", file=sys.stderr)
        return 2
    exchanges = json.loads(path.read_text(encoding="utf-8"))
    res = check(exchanges, set(args.day) if args.day else None)

    text = _summary(res) + (_detail(res) if args.detail else "")
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
