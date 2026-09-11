"""Airband flight identification: match a heard callsign against the live adsb.fi cache.

Mirrors identify.py's role for vessels, deliberately smaller -- regex/dictionary extraction
and rapidfuzz matching, no Claude call, no conversation-level resolution. See
docs/superpowers/specs/2026-09-08-airband-flight-identification-design.md.
"""

import re

from rapidfuzz import fuzz as rf_fuzz

from stt_proxy import adsb
from stt_proxy.corrections import _decode_spoken_word

# Spoken/heard word -> ICAO 3-letter airline code. Seeded from carriers actually observed
# during the 2026-09-08 feasibility check (both the live log and the live adsb.fi traffic).
# Grow this table from real transmissions, the same policy corrections.py's phonetic table
# already uses.
#
# KLM's known garbled forms (kalm, rklm, klmx) are listed explicitly rather than relying on
# fuzzy matching -- "KLM" is 3 characters, in the same danger zone corrections.py's own
# comments document for its phonetic-letter table: at any fuzzy threshold loose enough to
# catch real garbling of a 3-character token, unrelated short words start scoring just as
# high. Longer airline names (5+ characters) are safe for fuzzy matching; see
# _find_airline_anchor below.
AIRLINE_TELEPHONY: dict[str, str] = {
    "klm": "KLM", "kalm": "KLM", "rklm": "KLM", "klmx": "KLM",
    "llm": "KLM", "lem": "KLM",
    "transavia": "TRA",
    "american": "AAL",
    "united": "UAL",
    "delta": "DAL",
    "envoy": "ENY",
    "qatar": "QTR", "qatari": "QTR",
    "ryanair": "RYR",
    "speedbird": "BAW",
    "lufthansa": "DLH",
    "easyjet": "EZY",
    "shamrock": "EIN", "shemarck": "EIN",
    "canada": "ACA",
    # Both spellings were observed in the 2026-09-10 corpus; "jet blue" is joined into one
    # token before tokenizing (see _TELEPHONY_PHRASES) rather than anchoring on "blue", which
    # would fabricate JBU1 out of "the blue one in sight".
    "jetblue": "JBU",
    # ORANGE is TUI fly Netherlands' telephony designator. Held back deliberately until the
    # identification baseline had been taken, so it would register as a real extraction miss
    # rather than being quietly fixed before the number was published -- it did, twice.
    "orange": "TFL",
    # "KL one two bravo" for KLM12B. Two letters, so it never reaches the fuzzy path below and
    # must match exactly, which is what makes an anchor this short safe.
    "kl": "KLM",
}

# Telephony designators that are two spoken words. The tokenizer is single-word, and the
# second word on its own is not safe to anchor on -- "blue" alone turns "the blue one in
# sight" into JBU1 -- so the pair is joined into the single token the table already knows.
# This is the general form of the workaround "canada" uses for Air Canada.
_TELEPHONY_PHRASES: dict[str, str] = {
    "jet blue": "jetblue",
}

# Spoken forms close enough to an ordinary word that fuzzy anchoring on them does more harm
# than good, so they must match exactly. fuzz.ratio("range", "orange") is 90.9, over the
# threshold, and "radar contact, range one two zero miles" would otherwise extract TFL120 --
# the same failure the speed/speedbird case produced (whole-branch review, finding 3). "range"
# appears zero times in the 238 real airband transmissions captured so far, which is why the
# entry is still worth having; it is an ordinary enough word not to gamble on.
_NEVER_FUZZY = frozenset({"orange"})

# Extra digit words _decode_spoken_word (corrections.py) doesn't cover, scoped locally rather
# than added to that shared table since they're specific to how flight numbers get read aloud
# rather than general spelled-out decoding (vessel names, etc.). Grown from real transmissions
# the same way AIRLINE_TELEPHONY is: "one thirty three" for DAL133 and "two oh three" for
# AAL203, both observed 2026-09-09 with the correctly-tagged flight sitting right there in the
# session under a different phrasing. A tens word contributes only its tens digit (dropping
# the implicit trailing zero) because it's always immediately followed by a units word in the
# observed data ("thirty" + "three" -> "33"); a bare tens word with no units word after it
# (e.g. "Delta sixty" for DAL60) isn't handled -- no evidence yet it's needed.
_SUPPLEMENTAL_DIGIT_WORDS: dict[str, str] = {
    "twenty": "2", "thirty": "3", "forty": "4", "fifty": "5",
    "sixty": "6", "seventy": "7", "eighty": "8", "ninety": "9",
    "oh": "0",
}

# A digit word immediately followed by one of these means the digit belongs to an altitude or
# fraction reading, not the callsign -- stop the run before it. Evidence: "Delta seven four
# eight thousand for seven thousand" is DAL74, not DAL748 (proxy-2026-09-09.log line 440); "one
# six one two point two percent" is DAL161, not DAL1612 (line 688). Both real transmissions
# from the same session, not a hypothetical -- add more boundary words only against similar
# evidence.
_DIGIT_RUN_BOUNDARY_WORDS = {"thousand", "point"}


def _decode_digit_word(word: str, next_word: str | None) -> str | None:
    """Like _decode_spoken_word, plus the tens/oh table above and one deliberately narrow
    homophone case.

    "for"/"four" (real 2026-09-09 23:54 transmission: "Transavia, two for Zulu" -> should be
    TRA24Z) is NOT added as a blanket alias the way "oh" was -- "for" is one of the most common
    words in English ("cleared for ILS", "request for deviation"), so decoding it unconditionally
    would corrupt those. It's only treated as "4" when the word right after it is itself
    digit-context (another digit or a phonetic letter), which real prepositional uses of "for"
    essentially never are.
    """
    char = _decode_spoken_word(word) or _SUPPLEMENTAL_DIGIT_WORDS.get(word)
    if char is not None:
        return char
    if word == "for" and next_word is not None and _decode_spoken_word(next_word) is not None:
        return "4"
    return None


# Only words this long or longer are tried against the table with fuzzy matching -- see the
# module docstring's note on why short tokens (KLM's 3 characters) are handled by explicit
# variants instead.
_FUZZY_MIN_WORD_LEN = 5
# 70 was a starting point, not measured against a labelled corpus, and it was too loose:
# fuzz.ratio("speed", "speedbird") is 71.4, so "reduce speed one eight zero" false-anchored on
# "speedbird" (see the whole-branch review, finding 3). 85 still passes every explicit-variant
# and genuine-garbling case the test suite covers -- those matches score in the 90s-100 range
# -- while putting "speed"/"speedbird" (and similarly short overlaps) below threshold.
_FUZZY_THRESHOLD = 85


def _find_airline_anchor(word: str) -> str | None:
    """The ICAO code if `word` names a known airline (exactly, or a garbled long name), else None."""
    exact = AIRLINE_TELEPHONY.get(word)
    if exact:
        return exact
    if len(word) < _FUZZY_MIN_WORD_LEN:
        return None
    best_code, best_score = None, 0
    for spoken, code in AIRLINE_TELEPHONY.items():
        if len(spoken) < _FUZZY_MIN_WORD_LEN or spoken in _NEVER_FUZZY:
            continue
        score = rf_fuzz.ratio(word, spoken)
        if score > best_score:
            best_code, best_score = code, score
    return best_code if best_score >= _FUZZY_THRESHOLD else None


def extract_callsign_candidate(text: str) -> str | None:
    """A candidate flight designator (e.g. "TRA6N") for match_flight, or None.

    Deliberately permissive on the airline word -- an airline name can be misheard the same
    way a vessel name can. Deliberately strict on requiring digits to follow it: bare digits
    with no airline anchor are ambiguous (a heading, a QNH, a flight level) and must not be
    treated as a callsign.
    """
    # [A-Za-z0-9]+ (not letters-only): Whisper sometimes transcribes a flight number as
    # literal digits ("KLM 281") rather than spelling it out ("KLM two eight one"). A
    # letters-only tokenizer silently dropped those digit tokens entirely, which then fed the
    # bare-code fabrication problem this whole function exists to avoid (review finding 4).
    lowered = (text or "").lower()
    for phrase, joined in _TELEPHONY_PHRASES.items():
        lowered = lowered.replace(phrase, joined)
    words = re.findall(r"[A-Za-z0-9]+", lowered)
    for i, word in enumerate(words):
        code = _find_airline_anchor(word)
        if code is None:
            continue
        digits = ""
        window_end = min(i + 5, len(words))
        for j in range(i + 1, window_end):
            follow = words[j]
            peek = words[j + 1] if j + 1 < len(words) else None
            if follow.isdigit():
                # A numeral token ("281") already stands for its whole run of characters --
                # unlike a single spelled-out word, which _decode_spoken_word turns into
                # exactly one character. Keep scanning afterwards in case a phonetic-letter
                # suffix follows the number ("281 november" -> "281N").
                char = follow
            else:
                # Not just spoken digits: a real callsign suffix mixes digits and a single
                # phonetic letter ("six november" -> "6N"), which _decode_spoken_word already
                # handles by checking both tables -- using _SPOKEN_DIGITS alone here was tried
                # and empirically failed ("Transavia six november" produced "TRA6", dropping
                # the N) before this plan was finalised. _decode_digit_word adds tens words
                # ("thirty") and "oh" on top, scoped to this loop -- see
                # _SUPPLEMENTAL_DIGIT_WORDS above.
                char = _decode_digit_word(follow, peek)
            if char is None:
                break
            if peek in _DIGIT_RUN_BOUNDARY_WORDS:
                # This digit is the start of an altitude/fraction reading ("eight thousand",
                # "two point two"), not part of the callsign -- stop before including it.
                break
            digits += char
        if digits:
            return f"{code}{digits}"
        if code == word.upper():
            return code
        # This anchor matched (exactly or fuzzily) but produced nothing usable -- e.g. a
        # fuzzy false-anchor like "speed"/"speedbird" with no callsign actually spoken here.
        # Keep scanning: a real callsign may still appear later in the same transmission
        # (review finding 3 -- "reduce speed one eight zero, KLM two eight one" must not die
        # on "speed" before ever reaching "KLM").
    return None


# Airline codes are typically 2-4 letters (KLM, BAW, RYR, EZY...); everything after that is
# the flight's digit/suffix tail (e.g. "281", "6N").
_CODE_TAIL_RE = re.compile(r"^([A-Za-z]{2,4})(.*)$")


def _split_code_tail(flight: str) -> tuple[str, str] | None:
    """(code, tail) for a flight designator, or None if it doesn't even start with a code.

    Used by match_flight to fuzz only the airline-code portion and require the tail to match
    exactly -- see the module-level note on why fuzzing the whole string was wrong.
    """
    if not flight:
        return None
    m = _CODE_TAIL_RE.match(flight)
    if not m:
        return None
    return m.group(1).upper(), m.group(2).strip().upper()


def match_flight(candidate: str | None) -> dict | None:
    """The live aircraft `candidate` most likely refers to, or None.

    Exact match first (the common case once extraction has already normalised known garbled
    airline forms). Falls back to a fuzzy match, but ONLY on the airline-code portion of the
    string -- fuzzing the whole candidate ("KLM281" vs "KLM285") let two different real
    flights score well over threshold against each other, and let a bare code with no digits
    ("KLM") fuzzy-match any cached flight whose code was similar (review findings 1 and 2).
    The digit/suffix tail must match exactly: both empty (a bare code, matched only against
    another bare-code cache entry -- which in practice does not happen for airline traffic,
    so a bare code effectively never fuzzy-matches) or both present and identical.
    """
    if not candidate:
        return None

    for ac in adsb.current_aircraft():
        if ac["flight"] == candidate:
            return ac

    cand_split = _split_code_tail(candidate)
    if cand_split is None:
        return None
    cand_code, cand_tail = cand_split

    best_ac, best_score = None, 0
    for ac in adsb.current_aircraft():
        ac_split = _split_code_tail(ac["flight"])
        if ac_split is None:
            continue
        ac_code, ac_tail = ac_split
        if cand_tail != ac_tail:
            continue
        score = rf_fuzz.ratio(cand_code, ac_code)
        if score > best_score:
            best_ac, best_score = ac, score
    return best_ac if best_score >= _FUZZY_THRESHOLD else None


def format_flight_for_plugin(result: dict, text: str) -> str:
    flight = result.get("flight") or ""
    aircraft_type = result.get("t")
    tag = f"[{flight}/{aircraft_type}]" if aircraft_type else f"[{flight}]"
    return f"{tag} {text}"


def _near_miss_codes(cand_code: str, cache: list[dict]) -> list[str]:
    """Flight designators in `cache` whose airline code fuzzy-matches `cand_code`, regardless
    of tail. Diagnostic only -- lets a real session's log distinguish "the right airline was
    nearby, just with a different flight number" (evidence the digit tail is getting
    misheard, the same way airline names are) from "nothing from that airline was in range at
    all" (evidence of a poll-timing/radius miss instead). See the review's own note: measure
    before tuning the matching thresholds again.
    """
    near = []
    for ac in cache:
        split = _split_code_tail(ac["flight"])
        if split is None:
            continue
        code, _tail = split
        if rf_fuzz.ratio(cand_code, code) >= _FUZZY_THRESHOLD:
            near.append(ac["flight"])
    return near


def _log_unmatched(candidate: str) -> None:
    cache = adsb.current_aircraft()
    split = _split_code_tail(candidate)
    if split is None:
        print(f"[flight-id] no match for {candidate!r} -- {len(cache)} aircraft in range, "
              f"candidate has no parseable airline code", flush=True)
        return
    cand_code, _cand_tail = split
    near = _near_miss_codes(cand_code, cache)
    print(f"[flight-id] no match for {candidate!r} -- {len(cache)} aircraft in range, "
          f"same-airline nearby: {near or 'none'}", flush=True)


def identify_flight(text: str) -> str:
    """The one call site Task 3 needs: identify and prefix, or return text unchanged.

    Only logs on a genuine extraction-but-no-match -- ordinary chatter (headings, QNH) never
    yields a candidate at all and must not print anything, or the console would drown in
    noise the way over-eager logging elsewhere in this project has before.
    """
    candidate = extract_callsign_candidate(text)
    if candidate is None:
        return text
    result = match_flight(candidate)
    if result is None:
        _log_unmatched(candidate)
        return text
    return format_flight_for_plugin(result, text)
