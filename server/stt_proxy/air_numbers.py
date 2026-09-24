"""Altitudes and headings spoken in an airband transmission.

Only numbers that directly follow a trigger word are read. That single rule is what keeps
frequencies ("one eight four zero five" = 118.405), runways ("runway one eight right") and QNH
out: none of them has a trigger, so none is ever consumed. QNH is excluded on purpose -- the
decoder writes "QNH" where "KLM" was spoken (2026-09-11 finding) -- and speed is excluded
because ADS-B carries no selected speed to compare it with.

See docs/superpowers/specs/2026-09-24-airband-conversations-design.md.
"""
from __future__ import annotations

import dataclasses
import re

_DIGITS = {
    "zero": "0", "oh": "0", "one": "1", "two": "2", "three": "3", "tree": "3",
    "four": "4", "five": "5", "fife": "5", "six": "6", "seven": "7", "eight": "8",
    "nine": "9", "niner": "9",
}
_SMALL = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
          "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
          "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
          "nineteen": 19, "twenty": 20, "thirty": 30}

_ALT_VERBS = {"climb", "climbing", "descend", "descending", "maintain", "maintaining"}
_CURRENT = {"passing", "leaving"}          # a report of where they are, not a clearance
_FILLER = {"to", "and", "flight", "level", "altitude", "now", "further"}


@dataclasses.dataclass(frozen=True)
class Number:
    kind: str       # "altitude" (feet) or "heading" (degrees)
    value: int
    text: str


def to_dict(n: Number) -> dict:
    return dataclasses.asdict(n)


def from_dict(d: dict) -> Number:
    return Number(kind=d["kind"], value=int(d["value"]), text=d.get("text", ""))


def _tokens(text: str) -> list[str]:
    """Words, digits split one per token, and punctuation kept as a boundary.

    The comma matters: "descending zero two zero, nine eight two" is FL020 followed by a
    callsign, and without the boundary the two runs merge into one six-digit run.
    """
    out = []
    for raw in re.findall(r"[a-z]+|\d+|[,.;:!?]", (text or "").lower()):
        if raw.isdigit():
            out.extend(raw)             # "70" is read as "seven zero"
        else:
            out.append(raw)
    return out


def _digit(tok: str) -> str | None:
    if tok.isdigit():
        return tok
    return _DIGITS.get(tok)


def _digit_run(toks: list[str], i: int) -> tuple[str, int]:
    """The maximal run of digits starting at i, and the index after it."""
    run = ""
    while i < len(toks):
        d = _digit(toks[i])
        if d is None:
            break
        run += d
        i += 1
    return run, i


_TENS = {"twenty": 20, "thirty": 30}


def _thousands(toks: list[str], i: int) -> tuple[int, int] | None:
    """'two thousand [five hundred]' / 'twenty five hundred' starting at i -> (feet, end).

    Only the word(s) directly before "thousand"/"hundred" count, so "Delta seven three two
    thousand" is 2,000 ft after the callsign, not 732,000 or 12,000.
    """
    def unit(k):
        return _SMALL.get(toks[k]) if k < len(toks) else None

    if (toks[i] in _TENS and unit(i + 1) and unit(i + 1) < 10
            and i + 2 < len(toks) and toks[i + 2] in {"thousand", "hundred"}):
        n, j = _TENS[toks[i]] + unit(i + 1), i + 2
    elif unit(i) and i + 1 < len(toks) and toks[i + 1] in {"thousand", "hundred"}:
        n, j = unit(i), i + 1
    else:
        return None
    if toks[j] == "thousand":
        feet = n * 1000
        j += 1
        if j + 1 < len(toks) and unit(j) and toks[j + 1] == "hundred":
            feet += unit(j) * 100
            j += 2
        return feet, j
    if n >= 10:                              # "twenty five hundred"; "five hundred" is not
        return n * 100, j + 1
    return None


def extract_numbers(text: str) -> list[Number]:
    toks = _tokens(text)
    found: list[Number] = []
    i = 0
    while i < len(toks):
        tok = toks[i]
        prev = toks[i - 1] if i else ""

        if tok == "heading" or (tok == "turn" and prev in {"left", "right"}) or (
                tok in {"left", "right"} and i + 1 < len(toks) and toks[i + 1] == "turn"):
            j = i + 1
            while j < len(toks) and toks[j] in {"turn", "heading", "left", "right"}:
                j += 1
            run, end = _digit_run(toks, j)
            if len(run) == 3 and 1 <= int(run) <= 360:
                found.append(Number("heading", int(run), " ".join(toks[i:end])))
                i = end
                continue

        if tok == "level" or tok in _ALT_VERBS:
            j = i + 1
            while j < len(toks) and toks[j] in _FILLER:
                j += 1
            run, end = _digit_run(toks, j)
            if 2 <= len(run) <= 3 and (
                    end == len(toks) or toks[end] not in {"thousand", "hundred"}):
                found.append(Number("altitude", int(run) * 100, " ".join(toks[i:end])))
                i = end
                continue

        if tok in _SMALL:
            if not any(t in _CURRENT for t in toks[max(0, i - 2):i]) and prev not in {"of"}:
                got = _thousands(toks, i)
                if got is not None:
                    feet, end = got
                    found.append(Number("altitude", feet, " ".join(toks[i:end])))
                    i = end
                    continue
        i += 1

    unique: list[Number] = []
    seen = set()
    for n in found:
        if (n.kind, n.value) not in seen:
            seen.add((n.kind, n.value))
            unique.append(n)
    return unique
