import copy
import json
import os
import sys
import urllib.error
import urllib.request

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from stt_proxy import aishub  # noqa: E402


def _envelope(records, error=False):
    return json.dumps([
        {"ERROR": error, "USERNAME": "X", "FORMAT": "HUMAN", "RECORDS": len(records)},
        records,
    ]).encode("utf-8")


SHIP = {
    "MMSI": 244123456, "TIME": "2026-08-12 10:02:58 GMT",
    "LONGITUDE": 3.95477, "LATITUDE": 52.06695, "COG": 51.3, "SOG": 4.9,
    "HEADING": 103, "IMO": 9406714, "NAME": "ORASUND", "CALLSIGN": "PBZL",
    "TYPE": 70, "A": 100, "B": 20, "C": 8, "D": 7, "DRAUGHT": 7.4,
    "DEST": "NLRTM",
}


def test_parse_response_returns_the_ships():
    ships = aishub.parse_response(_envelope([SHIP]))
    assert len(ships) == 1
    assert ships[0]["NAME"] == "ORASUND"


def test_parse_response_treats_the_error_flag_as_no_observation():
    # The rate-limit response: HTTP 200, valid JSON, ERROR true, no ships. Read as an
    # empty box it would mark every cached vessel out of scope.
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(_envelope([], error=True))


def test_parse_response_rejects_a_missing_ships_array():
    payload = json.dumps([{"ERROR": False, "RECORDS": 0}]).encode("utf-8")
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(payload)


def test_parse_response_rejects_malformed_json():
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(b"<html>502 Bad Gateway</html>")


def test_parse_response_allows_a_genuinely_empty_box():
    # Distinct from ERROR: a real observation that found nothing.
    assert aishub.parse_response(_envelope([])) == []


def test_map_ship_maps_every_field_record_understands():
    fields = aishub.map_ship(SHIP)
    assert fields["mmsi"] == "244123456"
    assert fields["name"] == "ORASUND"
    assert fields["callsign"] == "PBZL"
    assert fields["imo"] == 9406714
    assert fields["type"] == 70
    assert fields["length"] == 120      # A + B
    assert fields["beam"] == 15         # C + D
    assert fields["draught"] == 7.4
    assert fields["destination"] == "NLRTM"
    assert fields["latitude"] == 52.06695
    assert fields["longitude"] == 3.95477
    assert fields["sog"] == 4.9
    assert fields["cog"] == 51.3
    assert fields["heading"] == 103


def test_map_ship_drops_a_record_with_no_mmsi():
    assert aishub.map_ship({"NAME": "NO IDENTITY"}) is None


def test_map_ship_survives_absent_optional_fields():
    fields = aishub.map_ship({"MMSI": 1, "NAME": "SPARSE"})
    assert fields["mmsi"] == "1"
    assert fields["length"] is None
    assert fields["draught"] is None


def test_map_ship_strips_ais_destination_padding():
    fields = aishub.map_ship({**SHIP, "DEST": "ROTTERDAM@@@@@@@"})
    assert fields["destination"] == "ROTTERDAM"


def test_map_ship_maps_a_non_numeric_position_to_none():
    """ais.record() -> _apply() -> ais._km_from_maas() now runs INSIDE record()'s
    _cache_lock (reached via _refresh_name_view -> _candidate_sort_key). A non-numeric
    LATITUDE/LONGITUDE reaching that far raises TypeError mid-write, so map_ship must
    neutralise it before it ever gets there -- the same way any other absent AISHub field
    is neutralised to None, not passed through as garbage."""
    fields = aishub.map_ship({**SHIP, "LATITUDE": "not-a-number", "LONGITUDE": None})
    assert fields["latitude"] is None
    assert fields["longitude"] is None


def test_map_ship_coerces_a_numeric_position_to_float():
    fields = aishub.map_ship({**SHIP, "LATITUDE": "52.5", "LONGITUDE": 3})
    assert fields["latitude"] == 52.5
    assert fields["longitude"] == 3.0
    assert isinstance(fields["latitude"], float)
    assert isinstance(fields["longitude"], float)


def test_poll_once_survives_a_non_numeric_position(monkeypatch):
    """Integration-level companion to the map_ship tests above: without the coercion, this
    poll would raise TypeError from inside ais._km_from_maas, reached mid-way through
    poll_once's write loop (via _refresh_name_view -> _candidate_sort_key). With the fix, the
    bad position simply becomes an absent one and the poll completes normally."""
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    bad = {**SHIP, "LATITUDE": "GARBAGE"}
    count = aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([bad]))

    assert count == 1
    # _apply() only ever writes latitude/longitude together, so a coerced-to-None latitude
    # means the position is treated as entirely absent -- no "latitude" key at all, not a
    # key holding None. The point under test is simply that this line is reached without
    # raising.
    assert ais._mmsi_index["244123456"].get("latitude") is None


def test_map_ship_maps_a_non_string_name_callsign_and_dest_to_empty():
    """NAME, CALLSIGN and DEST are unconditionally .strip()ed/.split()ed downstream (in
    map_ship itself and in _clean_destination), which raises AttributeError on anything that
    is not already a string. A malformed or future AISHub response is the plausible source --
    the same defensive posture _coerce_float already takes for LATITUDE/LONGITUDE."""
    fields = aishub.map_ship({**SHIP, "NAME": ["not", "a", "string"], "CALLSIGN": {"x": 1},
                               "DEST": 12345})
    assert fields["name"] == ""
    assert fields["callsign"] == ""
    assert fields["destination"] is None


def test_map_ship_maps_a_non_numeric_type_to_none():
    """ais._get_ship_type_name uses this value as an AIS_SHIP_TYPES dict key, reached from
    _refresh_name_view -> _candidate_sort_key -> _type_plausibility while record() holds
    _cache_lock. An unhashable TYPE (a list, here) would raise TypeError: unhashable type
    there -- the mechanism code review found still open after LATITUDE/LONGITUDE were closed."""
    fields = aishub.map_ship({**SHIP, "TYPE": ["not", "hashable"]})
    assert fields["type"] is None


def test_map_ship_coerces_a_numeric_string_type_to_int():
    fields = aishub.map_ship({**SHIP, "TYPE": "70"})
    assert fields["type"] == 70
    assert isinstance(fields["type"], int)


def test_poll_once_survives_an_unhashable_type(monkeypatch):
    """Integration-level companion, reproducing the exact mechanism the coordinator's finding
    described: without the TYPE coercion, this poll raises TypeError: unhashable type from
    inside ais._get_ship_type_name, reached mid-way through poll_once's write loop via
    _refresh_name_view. With the fix, the bad type simply becomes an absent one."""
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_name_index", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    bad = {**SHIP, "TYPE": ["not", "hashable"]}
    count = aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([bad]))

    assert count == 1
    assert ais._mmsi_index["244123456"]["type"] is None


def test_parse_time_reads_the_gmt_stamp():
    # Absolute epoch, so this assertion is timezone-independent.
    assert aishub.parse_time("2026-08-12 10:02:58 GMT") == 1786528978.0


def test_parse_time_returns_none_on_junk():
    assert aishub.parse_time("not a time") is None
    assert aishub.parse_time("") is None


def test_parse_response_rejects_a_non_dict_envelope():
    # body[0] not a dict used to fall back to envelope = {}, which skips the ERROR check
    # entirely and returns body[1] when it happens to be a list -- feeding ghost ships
    # straight into the cache.
    payload = json.dumps([[1, 2, 3], [{"MMSI": 1, "NAME": "GHOST"}]]).encode("utf-8")
    with pytest.raises(aishub.AisHubError):
        aishub.parse_response(payload)


def test_build_url_carries_the_box_and_asks_for_json():
    url = aishub.build_url("USER", (51.0, 53.2, 2.0, 6.0))
    assert url.startswith(aishub.API_URL + "?")
    assert "username=USER" in url
    assert "output=json" in url
    assert "format=1" in url
    assert "latmin=51.0" in url and "latmax=53.2" in url
    assert "lonmin=2.0" in url and "lonmax=6.0" in url


def test_poll_once_records_every_named_vessel(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    payload = _envelope([SHIP, {**SHIP, "MMSI": 999, "NAME": "SECOND"}])
    count = aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: payload)

    assert count == 2
    assert ais._mmsi_index["244123456"]["name"] == "ORASUND"
    assert ais._mmsi_index["999"]["name"] == "SECOND"


def test_poll_once_uses_the_report_time_as_last_seen(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    import datetime as _dt
    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))

    # SHIP's TIME is 2026-08-12 10:02:58 GMT. Stored as local wall-clock, like every other
    # last_seen in this codebase, so compute rather than hardcode.
    expected = _dt.datetime.fromtimestamp(1786528978.0).strftime("%Y-%m-%d %H:%M:%S")
    assert ais._vessel_cache["ORASUND"]["last_seen"] == expected


def test_poll_once_publishes_the_in_scope_set(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    payload = _envelope([SHIP, {**SHIP, "MMSI": 999, "NAME": "SECOND"}])
    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: payload)

    assert ais.get_in_scope() == {"244123456", "999"}


def test_poll_once_ranks_within_the_same_poll_using_this_polls_scope(monkeypatch):
    """set_in_scope must run BEFORE the write loop, not after: _refresh_name_view (called
    from inside every ais.record() during the loop) ranks candidates against whatever scope
    is CURRENTLY published. If set_in_scope ran after the loop, as it used to, every
    ranking decision made during this poll would use the PREVIOUS poll's scope -- stale by
    exactly one interval, and disagreeing with candidates_for_name(), which always reads
    the current scope via get_in_scope(). Seeds a stale scope of {"111"} left over from a
    hypothetical earlier poll, then polls two ALBATROS in the SAME response: 111 (far from
    Maas Center) and 222 (at Maas Center). Both are in THIS poll's scope, so ranking must
    fall through to proximity and pick 222 -- not 111, which only wins if the stale scope
    from before this poll is still what's being consulted."""
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_name_index", {})
    monkeypatch.setattr(ais, "_in_scope", {"111"})

    far  = {**SHIP, "MMSI": 111, "NAME": "ALBATROS", "LATITUDE": 40.0, "LONGITUDE": 2.0}
    near = {**SHIP, "MMSI": 222, "NAME": "ALBATROS", "LATITUDE": 52.02, "LONGITUDE": 3.88}
    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([far, near]))

    assert ais._vessel_cache["ALBATROS"]["mmsi"] == "222"


def test_a_failed_poll_leaves_the_cache_and_the_scope_alone(monkeypatch):
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))
    before_cache = dict(ais._vessel_cache)
    before_scope = set(ais.get_in_scope())

    with pytest.raises(aishub.AisHubError):
        aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0),
                         fetch=lambda url: _envelope([], error=True))

    assert ais._vessel_cache == before_cache
    assert ais.get_in_scope() == before_scope


def test_the_poll_interval_cannot_be_configured_below_the_rate_limit(monkeypatch):
    monkeypatch.setenv("AISHUB_POLL_SEC", "5")
    assert aishub._resolve_poll_sec() == aishub.MIN_INTERVAL_SEC


def test_the_poll_interval_honours_a_legal_setting(monkeypatch):
    monkeypatch.setenv("AISHUB_POLL_SEC", "900")
    assert aishub._resolve_poll_sec() == 900


def test_a_poll_that_fails_after_parsing_leaves_the_cache_and_the_scope_alone(monkeypatch):
    """Distinct from test_a_failed_poll_leaves_the_cache_and_the_scope_alone above: that one
    raises inside parse_response, BEFORE poll_once has touched any shared state, so it would
    pass even if poll_once wrote records as it went. This uses a body that parses fine but
    carries a malformed ship element, so the failure happens partway through poll_once's own
    loop -- the case that actually exercises "validated and parsed before anything is
    touched." Uses copy.deepcopy for the before-snapshot because _vessel_cache entries are
    shared dict references: a shallow copy would not catch an in-place mutation.
    """
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))
    before_cache = copy.deepcopy(ais._vessel_cache)
    before_scope = set(ais.get_in_scope())

    malformed = _envelope([{**SHIP, "MMSI": 999, "NAME": "SECOND"}, "not-a-dict"])
    with pytest.raises(aishub.AisHubError):
        aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: malformed)

    assert ais._vessel_cache == before_cache
    assert ais.get_in_scope() == before_scope


def test_a_record_failure_mid_write_loop_leaves_the_cache_and_the_scope_alone(monkeypatch):
    """Distinct from both tests above: those fail during VALIDATION (parse_response, or the
    malformed-element check), entirely before poll_once has called ais.record even once. This
    fails INSIDE the write loop itself, on the second of two ships -- the case a code-review
    round found genuinely broken when set_in_scope was (briefly) moved to run BEFORE this
    loop: with that ordering, the scope would already have been published as {'111', '999'}
    -- this poll's FULL scope -- while _mmsi_index held only the first ship, the two
    disagreeing about which poll they describe. ais.record can fail here for real reasons,
    not just this test's simulation: a non-scalar AISHub TYPE reaches
    ais._get_ship_type_name (via _refresh_name_view -> _candidate_sort_key ->
    _type_plausibility) and raises TypeError -- map_ship's coercions do not close every such
    field. With set_in_scope restored to running AFTER the loop, a mid-loop failure must
    leave BOTH the cache and the scope exactly where they were before this poll, the same
    guarantee poll_once's docstring gives the validation-pass failures.
    """
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_name_index", {})
    monkeypatch.setattr(ais, "_in_scope", {"PRE_EXISTING"})

    real_record = ais.record
    calls = {"n": 0}

    def flaky_record(fields, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise TypeError("simulated failure partway through the write loop")
        real_record(fields, **kwargs)

    monkeypatch.setattr(ais, "record", flaky_record)

    payload = _envelope([SHIP, {**SHIP, "MMSI": 999, "NAME": "SECOND"}])
    with pytest.raises(TypeError):
        aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: payload)

    # The write loop itself is not, and was never, atomic across ships: ship 1 is expected to
    # have been written before ship 2's simulated failure, the same way it always would be if
    # a real network drop happened mid-poll. That is accepted, not the regression this test
    # guards. What must hold is that the SCOPE never advances past a poll that didn't finish --
    # under the (fixed and reverted) ordering where set_in_scope ran before the write loop,
    # this would already read {'244123456', '999'}, the full scope of a poll that never
    # actually finished writing.
    assert "244123456" in ais._mmsi_index
    assert ais.get_in_scope() == {"PRE_EXISTING"}, (
        "the scope must not have been published for a poll that never finished")


def test_poll_once_converts_a_urlopen_failure_and_changes_nothing(monkeypatch):
    """Exercises the real `_fetch`, not a stub -- `_fetch`'s conversion of
    urllib.error.URLError (and OSError, which HTTPError/BadGzipFile subclass) into
    AisHubError was previously verified only by inspection.
    """
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))
    before_cache = copy.deepcopy(ais._vessel_cache)
    before_scope = set(ais.get_in_scope())

    def _boom(request, timeout=None):
        raise urllib.error.URLError("no route to host")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)

    with pytest.raises(aishub.AisHubError):
        aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0))

    assert ais._vessel_cache == before_cache
    assert ais.get_in_scope() == before_scope


def test_poll_once_propagates_a_malformed_body_and_changes_nothing(monkeypatch):
    """The unparseable-body path, previously only exercised against parse_response directly
    and never end-to-end through poll_once.
    """
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_vessel_cache", {})
    monkeypatch.setattr(ais, "_callsign_cache", {})
    monkeypatch.setattr(ais, "_mmsi_index", {})
    monkeypatch.setattr(ais, "_pending", {})
    monkeypatch.setattr(ais, "_in_scope", set())

    aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0), fetch=lambda url: _envelope([SHIP]))
    before_cache = copy.deepcopy(ais._vessel_cache)
    before_scope = set(ais.get_in_scope())

    with pytest.raises(aishub.AisHubError):
        aishub.poll_once("U", (51.0, 53.2, 2.0, 6.0),
                         fetch=lambda url: b"<html>502 Bad Gateway</html>")

    assert ais._vessel_cache == before_cache
    assert ais.get_in_scope() == before_scope


def test_get_in_scope_returns_a_copy_of_the_set(monkeypatch):
    """A reviewer mutated get_in_scope to `return _in_scope` with no lock and every existing
    test still passed -- this pins the copy so that regression cannot recur silently.
    """
    from stt_proxy import ais
    monkeypatch.setattr(ais, "_in_scope", {"1", "2"})

    result = ais.get_in_scope()
    result.add("3")

    assert ais.get_in_scope() == {"1", "2"}
