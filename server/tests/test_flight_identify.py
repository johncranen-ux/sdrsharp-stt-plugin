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


def test_extract_numeral_flight_number_survives_extraction():
    """Whisper sometimes emits the flight number as literal digits rather than spelling it
    out. Before the whole-branch review's finding 4 fix, the tokenizer was letters-only and
    silently dropped "281" entirely, leaving only the bare airline code."""
    assert flight_identify.extract_callsign_candidate("KLM 281, cleared ILS.") == "KLM281"


def test_extract_continues_past_a_false_anchor_to_a_real_callsign_later_in_the_text():
    """Review finding 3's exact reproduction: "speed" used to fuzzy-anchor on "speedbird"
    (ratio 71.4, over the old threshold of 70) and extraction gave up right there, never
    reaching the real callsign spoken later in the same transmission."""
    assert flight_identify.extract_callsign_candidate(
        "Reduce speed one eight zero, KLM two eight one."
    ) == "KLM281"


def test_extract_speed_word_alone_yields_no_candidate():
    assert flight_identify.extract_callsign_candidate("Speed.") is None


def test_extract_heading_like_speed_phrase_yields_no_candidate():
    """A heading-shaped phrase built from "speed" must not be mistaken for speedbird plus a
    flight number -- there is no airline word here at all once the threshold is fixed."""
    assert flight_identify.extract_callsign_candidate("Speed one eight zero.") is None


def test_extract_genuine_long_name_garbling_still_fuzzy_matches_at_the_raised_threshold():
    """The threshold went from 70 to 85 to kill "speed"/"speedbird" (71.4), but real garbling
    of a longer airline name must still work -- fuzz.ratio("lufthanza", "lufthansa") is 88.9,
    comfortably above 85."""
    assert flight_identify.extract_callsign_candidate(
        "Lufthanza six seven six, descend flight level one one zero."
    ) == "DLH676"


def test_extract_compressed_tens_number_word():
    """Real 2026-09-09 session: "Delta one thirty three" for DAL133 -- ATC/pilots don't
    always read digits individually. Before this fix, "thirty" wasn't decodable at all, so
    extraction stopped after "one" and produced "DAL1" (logged as a no-match 5 times in
    proxy-2026-09-09.log, each time with the real DAL133 sitting in "same-airline nearby")
    even though the very same aircraft got tagged correctly elsewhere in the same session
    when read digit-by-digit ("one three three")."""
    assert flight_identify.extract_callsign_candidate(
        "Star third, Delta one thirty three out of two for six."
    ) == "DAL133"


def test_extract_stops_digit_run_before_an_altitude_thousand_word():
    """Real 2026-09-09 session: "Delta seven four eight thousand for seven thousand" is
    DAL74 climbing/descending through an altitude, not DAL748 -- but the old 4-word lookahead
    window had no stop condition, so it swallowed the "eight" from "eight thousand" into the
    candidate (logged as 'DAL748' with the real DAL74 sitting in "same-airline nearby",
    proxy-2026-09-09.log line 440)."""
    assert flight_identify.extract_callsign_candidate(
        "Shipboat Delta seven four eight thousand for seven thousand, "
        "and we're on the zero eight zero heading."
    ) == "DAL74"


def test_extract_stops_digit_run_before_a_point_fraction_word():
    """Real 2026-09-09 session: "Delta one six one two point two percent" is DAL161, not
    DAL1612 -- the trailing "two" belongs to "point two percent", not the flight number
    (logged as 'DAL1612' with the real DAL161 sitting in "same-airline nearby",
    proxy-2026-09-09.log line 688)."""
    assert flight_identify.extract_callsign_candidate(
        "My departure is Delta one six one two point two percent."
    ) == "DAL161"


def test_extract_oh_decodes_as_zero_in_a_digit_run():
    """Real 2026-09-09 session: "American, two oh three" is AAL203 -- the same aircraft got
    tagged correctly 6 other times in this session using "two zero three", but "oh" wasn't in
    the digit table, so this one phrasing stopped after "two" and produced 'AAL2' (logged with
    the real AAL203 sitting in "same-airline nearby", proxy-2026-09-09.log line 847)."""
    assert flight_identify.extract_callsign_candidate(
        "Good morning American, two oh three, heavy passing two thousand "
        "three hundred four, on flight level six zero."
    ) == "AAL203"


def test_extract_llm_garbled_klm_variant():
    """Real 2026-09-09 session transcript (09:57 UTC-ish, plugin panel): "LLM five nine on
    November" for what context (same conversation as the confirmed KLM23N/KLM1031 traffic)
    makes clear is a garbled "KLM" -- not yet in AIRLINE_TELEPHONY alongside kalm/rklm/klmx."""
    assert flight_identify.extract_callsign_candidate(
        "LLM five nine on November, after SPY we'd like to obtain the heading."
    ) == "KLM59"


def test_extract_lem_garbled_klm_variant():
    """Real 2026-09-09 session transcript: "LEM two three" spoken in the middle of a run of
    confirmed KLM23N transmissions from the same aircraft -- another garbled "KLM" form not
    yet in AIRLINE_TELEPHONY."""
    assert flight_identify.extract_callsign_candidate(
        "ILS one one three zero, okay, LEM two three, no rem."
    ) == "KLM23"


def test_extract_for_decodes_as_four_between_digit_context_words():
    """Real 2026-09-09 23:54 transmission: "Transavia, two for Zulu" -- Whisper transcribed
    "four" as its homophone "for", which isn't a digit word, so extraction stopped after "two"
    and produced 'TRA2' (logged with the real TRA24Z sitting in "same-airline nearby",
    proxy-2026-09-09.log line 191)."""
    assert flight_identify.extract_callsign_candidate(
        "QNH one zero one eight, Transavia, two for Zulu."
    ) == "TRA24Z"


def test_extract_for_before_a_non_digit_word_does_not_produce_a_false_digit():
    """"for" must only be treated as "four" when sandwiched between two digit-context words --
    otherwise ordinary uses ("cleared for ILS", "request for deviation") would corrupt the
    candidate. Here "for" is followed by "ILS" (not decodable), so it must stop the run
    exactly as any other non-decodable word would, leaving DAL13, not a fabricated DAL134."""
    assert flight_identify.extract_callsign_candidate(
        "Delta one three for ILS."
    ) == "DAL13"


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


def test_match_flight_rejects_a_different_flight_with_a_similar_code():
    """Review finding 1's exact reproduction: KLM281 was never in the cache, but
    fuzz.ratio("KLM281", "KLM285") is well over the old whole-string threshold, so the
    unrelated real flight KLM285 got returned as if it were a match. Fuzzing only the code
    and requiring the digit tail to match exactly must reject this."""
    _seed("484443", "KLM285")
    assert flight_identify.match_flight("KLM281") is None


def test_match_flight_bare_code_does_not_match_a_cached_flight_with_a_tail():
    """Review finding 2's exact reproduction: a transmission that named no flight number at
    all ("Hello KLM" -> bare candidate "KLM") must not fabricate an identification against
    whichever cached flight's code happens to fuzzy-match."""
    _seed("484443", "KLM76")
    assert flight_identify.match_flight("KLM") is None


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


def test_near_miss_codes_finds_same_airline_different_tail():
    """Diagnostic aid: given a candidate whose airline code fuzzy-matches a cached flight
    but whose tail doesn't (a real miss), report that flight as a near miss so a session log
    can distinguish "right airline, wrong digits" from "nothing from that airline nearby"."""
    _seed("484443", "KLM285")
    near = flight_identify._near_miss_codes("KLM", adsb.current_aircraft())
    assert near == ["KLM285"]


def test_near_miss_codes_empty_when_no_airline_overlap():
    _seed("484443", "AAL999")
    near = flight_identify._near_miss_codes("KLM", adsb.current_aircraft())
    assert near == []


def test_near_miss_codes_skips_unparseable_flight_strings():
    _seed("484443", "")
    near = flight_identify._near_miss_codes("KLM", adsb.current_aircraft())
    assert near == []


def test_identify_flight_logs_near_misses_on_no_match(capsys):
    _seed("484443", "KLM285")
    flight_identify.identify_flight("Hello KLM two eight one, cleared ILS.")
    err = capsys.readouterr().out
    assert "KLM281" in err
    assert "KLM285" in err


def test_identify_flight_logs_no_near_misses_when_cache_empty_of_that_airline(capsys):
    _seed("484443", "AAL999")
    flight_identify.identify_flight("Hello KLM two eight one, cleared ILS.")
    out = capsys.readouterr().out
    assert "KLM281" in out
    assert "none" in out


def test_identify_flight_does_not_log_when_no_candidate_extracted(capsys):
    """Ordinary chatter (headings, QNH) is the common case -- logging every one of those
    would drown the console the way over-eager print statements have in this project before."""
    flight_identify.identify_flight("One eight zero, over.")
    out = capsys.readouterr().out
    assert out == ""
