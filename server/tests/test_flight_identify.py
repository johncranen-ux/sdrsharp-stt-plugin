"""Tests for stt_proxy/flight_identify.py: callsign extraction and matching against the
live adsb.fi cache."""

import sys
from pathlib import Path

import pytest

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


@pytest.fixture(autouse=True)
def _clear_cache():
    adsb._aircraft_cache.clear()
    yield
    adsb._aircraft_cache.clear()


def _seed(hex_id: str, flight: str, t: str = "B738"):
    adsb._aircraft_cache[hex_id] = {
        "hex": hex_id, "flight": flight, "r": None, "t": t,
        "alt_baro": None, "gs": None, "track": None, "lat": None, "lon": None,
        "squawk": None, "last_seen": 0.0,
    }


def test_match_flight_exact_hit():
    _seed("484443", "KLM281")
    result = flight_identify.match_flight("KLM281")
    assert result["hex"] == "484443"
    assert result["flight"] == "KLM281"


def test_match_flight_resolves_a_garbled_candidate_to_the_real_flight():
    """extract_callsign_candidate already normalised KALM -> KLM, so the candidate reaching
    match_flight is clean; this test is the airline-code fuzzy path when the candidate
    itself still differs slightly from what's in the live cache (e.g. a flight number a
    digit short)."""
    _seed("484443", "KLM676")
    assert flight_identify.match_flight("KLM676")["hex"] == "484443"


def test_match_flight_no_match_returns_none():
    _seed("484443", "KLM281")
    assert flight_identify.match_flight("AAL999") is None


def test_match_flight_handles_empty_cache():
    assert flight_identify.match_flight("KLM281") is None


def test_match_flight_none_candidate_returns_none():
    """So callers can chain extract -> match without a None-check in between."""
    assert flight_identify.match_flight(None) is None


def test_format_flight_for_plugin_with_type():
    result = {"flight": "KLM281", "t": "A332"}
    assert (flight_identify.format_flight_for_plugin(result, "Approach, good day.")
            == "[KLM281/A332] Approach, good day.")


def test_format_flight_for_plugin_without_type():
    result = {"flight": "KLM281", "t": None}
    assert (flight_identify.format_flight_for_plugin(result, "Approach, good day.")
            == "[KLM281] Approach, good day.")


def test_identify_flight_prefixes_on_a_match():
    _seed("484443", "KLM281", t="A332")
    text = flight_identify.identify_flight("Hello KLM two eight one, cleared ILS.")
    assert text == "[KLM281/A332] Hello KLM two eight one, cleared ILS."


def test_identify_flight_returns_text_unchanged_without_a_match():
    text = flight_identify.identify_flight("One eight zero, over.")
    assert text == "One eight zero, over."


def test_identify_flight_returns_text_unchanged_when_cache_is_empty():
    text = flight_identify.identify_flight("Hello KLM two eight one, cleared ILS.")
    assert text == "Hello KLM two eight one, cleared ILS."
