"""Tests for stt_proxy/flight_identify.py: callsign extraction and matching against the
live adsb.fi cache."""

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from stt_proxy import adsb, flight_identify  # noqa: E402


def test_extract_clean_airline_name_and_digits():
    assert flight_identify.extract_callsign_candidate(
        "Transavia, six november, deviating heading zero two zero."
    ) == "TRA6N"


def test_extract_klm_said_plainly():
    assert flight_identify.extract_callsign_candidate(
        "Hello KLM, Greenland runway two thousand one one, ILS, cleared ILS."
    ) == "KLM"


def test_extract_known_garbled_klm_variants():
    """Real garbled forms observed in the 2026-09-08 session log -- the same policy
    corrections.py already uses: variants are added only where a real transmission
    produced them."""
    assert flight_identify.extract_callsign_candidate("KALM six seven six") == "KLM676"
    assert flight_identify.extract_callsign_candidate("RKLM seven four four") == "KLM744"


def test_extract_american_two_two_one():
    assert flight_identify.extract_callsign_candidate(
        "American two two one on bird two Sierra, passing two thousand four hundred."
    ) == "AAL221"


def test_extract_bare_digits_without_airline_anchor_returns_none():
    """A heading, QNH, or flight level is just digits -- without an airline word, treating
    it as a callsign would be a guess wearing evidence's clothes."""
    assert flight_identify.extract_callsign_candidate("One eight zero, over.") is None


def test_extract_airline_word_with_no_following_digits_returns_none():
    assert flight_identify.extract_callsign_candidate("Delta, say again please.") is None


def test_extract_returns_none_for_empty_text():
    assert flight_identify.extract_callsign_candidate("") is None
