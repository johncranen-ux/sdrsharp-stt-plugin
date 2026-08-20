"""AIS ship-type codes.

These assert the ITU-R M.1371 allocation directly, code by code, because the table this
replaced was wrong in a way that read as plausible: the whole 60-99 block was shifted by ten,
so 704 cargo ships in the live cache reported as "Tanker" and 753 tankers as "General cargo".
Nothing about that looks wrong on screen. Only checking against the standard catches it.
"""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import ship_types  # noqa: E402


class TestTheBlocksAreNotShifted:
    """The specific failure that made this module necessary."""

    def test_seventies_are_cargo_not_tankers(self):
        for code in range(70, 80):
            assert ship_types.coarse_name(code) == "Cargo", code

    def test_eighties_are_tankers_not_general_cargo(self):
        for code in range(80, 90):
            assert ship_types.coarse_name(code) == "Tanker", code

    def test_nineties_are_other_not_container_ships(self):
        for code in range(90, 100):
            assert ship_types.coarse_name(code) == "Other", code

    def test_sixties_are_passenger(self):
        for code in range(60, 70):
            assert ship_types.coarse_name(code) == "Passenger", code

    def test_the_codes_that_used_to_fall_through_now_resolve(self):
        # 79, 69, 99 and 55 hit no entry in the old table and displayed as "Type 79".
        assert ship_types.coarse_name(79) == "Cargo"
        assert ship_types.coarse_name(69) == "Passenger"
        assert ship_types.coarse_name(99) == "Other"
        assert ship_types.coarse_name(55) == "Law enforcement"

    def test_codes_above_99_are_not_ais_types(self):
        # The old table claimed 100-105 were bulk carriers. No such allocation exists, and no
        # vessel in the cache has ever carried one.
        for code in (100, 101, 105):
            assert ship_types.coarse_name(code) == f"Type {code}"


class TestCoarseName:
    def test_nothing_broadcast_is_none_not_a_guess(self):
        assert ship_types.coarse_name(None) is None

    def test_zero_is_not_available_rather_than_missing(self):
        # 80 vessels in the live cache broadcast 0, which is the AIS default. That is a
        # positive statement -- "the operator never set it" -- not an absent field.
        assert ship_types.coarse_name(0) == "Not available"

    def test_the_service_craft_are_each_themselves(self):
        assert ship_types.coarse_name(50) == "Pilot vessel"
        assert ship_types.coarse_name(51) == "Search & rescue"
        assert ship_types.coarse_name(52) == "Tug"
        assert ship_types.coarse_name(53) == "Port tender"

    def test_thirty_three_is_dredging_not_military(self):
        # GEOSURVEYOR XXII and 68 others; the old table called them military operations.
        assert ship_types.coarse_name(33) == "Dredging/underwater ops"
        assert ship_types.coarse_name(35) == "Military ops"

    def test_forty_is_high_speed_craft_not_a_pilot_vessel(self):
        assert ship_types.coarse_name(40) == "High-speed craft"

    def test_fishing_sailing_and_pleasure_keep_their_meaning(self):
        assert ship_types.coarse_name(30) == "Fishing"
        assert ship_types.coarse_name(36) == "Sailing"
        assert ship_types.coarse_name(37) == "Pleasure craft"

    def test_a_hazard_digit_does_not_change_the_category(self):
        # The whole point of coarse_name: the resolver and the log treat a tanker carrying
        # category B as a tanker.
        assert len({ship_types.coarse_name(c) for c in range(80, 90)}) == 1

    def test_a_string_code_resolves_the_same_as_an_int(self):
        # AISHub's JSON delivers TYPE as a string; aisstream delivers an int.
        assert ship_types.coarse_name("70") == ship_types.coarse_name(70)

    def test_an_uninterpretable_code_says_so_rather_than_crashing(self):
        assert ship_types.coarse_name("not a number") == "Type not a number"


class TestDescribe:
    def test_nothing_broadcast_is_none(self):
        assert ship_types.describe(None) is None

    def test_a_plain_type_reads_as_the_whole_category(self):
        assert ship_types.describe(70) == "Cargo ship — all ships of this type (AIS type 70)"

    def test_the_hazard_categories_are_spelled_out(self):
        assert "category A" in ship_types.describe(81)
        assert "category B" in ship_types.describe(82)
        assert "category C" in ship_types.describe(83)
        assert "category D" in ship_types.describe(84)

    def test_x9_is_no_additional_information_not_a_hazard(self):
        assert "no additional information" in ship_types.describe(79)

    def test_reserved_second_digits_say_reserved(self):
        for code in (75, 76, 77, 78):
            assert "reserved" in ship_types.describe(code).lower(), code

    def test_the_specific_craft_describe_themselves_fully(self):
        assert ship_types.describe(52) == "Tug (AIS type 52)"
        assert ship_types.describe(33) == "Dredging or underwater operations (AIS type 33)"
        assert ship_types.describe(29) == "Search and rescue aircraft (AIS type 29)"

    def test_the_towing_size_qualifier_is_kept(self):
        assert "200 m" in ship_types.describe(32)

    def test_wing_in_ground_and_high_speed_craft_are_named(self):
        assert ship_types.describe(20).startswith("Wing in ground (WIG) craft")
        assert ship_types.describe(40).startswith("High-speed craft (HSC)")

    def test_an_unallocated_code_is_reserved_not_invented(self):
        assert "Reserved" in ship_types.describe(5)

    def test_an_out_of_range_code_is_reported_as_unknown(self):
        assert ship_types.describe(250) == "Unknown AIS ship type code 250"

    def test_every_code_in_range_has_a_description(self):
        for code in range(0, 100):
            assert ship_types.describe(code), code
            assert ship_types.coarse_name(code), code
