import pytest

from stt_proxy.air_numbers import Number, extract_numbers, from_dict, to_dict


def alt(v):
    return ("altitude", v)


def hdg(v):
    return ("heading", v)


def kinds(text):
    return [(n.kind, n.value) for n in extract_numbers(text)]


@pytest.mark.parametrize("text, expected", [
    ("Descend flight level seven zero, QNH one two bravo.", [alt(7000)]),
    ("Maintain flight level six zero, Delta seven three.", [alt(6000)]),
    ("Climb one three zero, delta seven three.", [alt(13000)]),
    ("Flight level one three zero, American two zero two.", [alt(13000)]),
    ("Level four zero, Foxtrot ILS runway right, QNH one six.", [alt(4000)]),
    ("Maintain level six zero, United nine four seven.", [alt(6000)]),
    ("Descending zero two zero, nine eight two.", [alt(2000)]),
    ("Coteo, present heading, descend flight level seven zero, QNH one nine zero four four.",
     [alt(7000)]),
    ("Target two zero three, and two thousand feet, one airport, ILS six zero.", [alt(2000)]),
    ("One two three seven zero five, uh, nine thousand feet, take a break.", [alt(9000)]),
    ("Delta five seven with you, twenty five hundred, six.", [alt(2500)]),
    ("ILS, heading zero seven zero, ILS one eight three four five, QNH one eight two", [hdg(70)]),
    ("Turn left heading zero six zero, contact, drive on one one eight four zero five",
     [hdg(60)]),
    ("Over, right heading three six zero, KLM six two seven.", [hdg(360)]),
    ("Right turn three six zero, ship blue three two.", [hdg(360)]),
    ("Right turn three six zero, up to level one three zero, United nine four seven.",
     [hdg(360), alt(13000)]),
    ("Heading two seven zero, NRA one one eight four zero five, QNH one three zero.",
     [hdg(270)]),
    ("Two seven zero degrees, two four three zero alpha.", []),   # no trigger word
])
def test_positive_and_mixed(text, expected):
    assert kinds(text) == expected


@pytest.mark.parametrize("text", [
    # frequencies -- 118.405 and 123.705 spoken in full or abbreviated
    "One eight four zero five, this is the inbound aircraft.",
    "One two three seven zero five for Delta, seven three, good day.",
    "Contact arrival one one eight, decimal four zero five, QNH one one eight, bye.",
    "Tower one eight four zero five, six one three zero, goodbye.",
    # runways
    "ILS approach runway one eight center, over.",
    "Center for four zero, for the ILS runway right.",
    "ILS runway one eight right, roger",
    # QNH is never a trigger
    "Pulse, good morning, QNH one nine four four.",
    "Cleared runway one three zero, QNH one six two seven.",
    # a flight-level run that is not 2-3 digits is ambiguous, not guessed
    "Super four three zero, Alpha reducing two fifty, flight level seven zero eight zero",
    # current altitude, not a clearance
    "Port Cremoros three six seven heavy, passing two thousand six hundred, focus NISO.",
    # heading out of range
    "heading four five zero",
    "",
])
def test_negatives_yield_nothing(text):
    assert extract_numbers(text) == []


def test_numerals_are_read_like_spoken_digits():
    assert kinds("descend flight level 70") == [alt(7000)]
    assert kinds("heading 270") == [hdg(270)]


def test_a_number_repeated_in_one_transmission_is_reported_once():
    assert kinds("descend flight level seven zero, flight level seven zero") == [alt(7000)]


def test_round_trip_through_a_dict():
    n = Number("altitude", 7000, "flight level seven zero")
    assert from_dict(to_dict(n)) == n
