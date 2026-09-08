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
}

# Only words this long or longer are tried against the table with fuzzy matching -- see the
# module docstring's note on why short tokens (KLM's 3 characters) are handled by explicit
# variants instead.
_FUZZY_MIN_WORD_LEN = 5
_FUZZY_THRESHOLD = 70  # a starting point, not yet measured against a labelled corpus


def _find_airline_anchor(word: str) -> str | None:
    """The ICAO code if `word` names a known airline (exactly, or a garbled long name), else None."""
    exact = AIRLINE_TELEPHONY.get(word)
    if exact:
        return exact
    if len(word) < _FUZZY_MIN_WORD_LEN:
        return None
    best_code, best_score = None, 0
    for spoken, code in AIRLINE_TELEPHONY.items():
        if len(spoken) < _FUZZY_MIN_WORD_LEN:
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
    words = re.findall(r"[A-Za-z]+", (text or "").lower())
    for i, word in enumerate(words):
        code = _find_airline_anchor(word)
        if code is None:
            continue
        digits = ""
        for follow in words[i + 1:i + 5]:
            # Not just spoken digits: a real callsign suffix mixes digits and a single
            # phonetic letter ("six november" -> "6N"), which _decode_spoken_word already
            # handles by checking both tables -- using _SPOKEN_DIGITS alone here was tried
            # and empirically failed ("Transavia six november" produced "TRA6", dropping the
            # N) before this plan was finalised.
            char = _decode_spoken_word(follow)
            if char is None:
                break
            digits += char
        if digits:
            return f"{code}{digits}"
        return code if code == word.upper() else None
    return None
