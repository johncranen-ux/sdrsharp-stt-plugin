import re

import pytest

import air_archive
from stt_proxy import adsb, flight_attribution as fa
from stt_proxy.air_numbers import Number

# A real 2026 instant: on Windows, astimezone() raises OSError for epochs near 1970.
T = 1_790_000_000.0


def ac(hex_, flight, mcp=None, hdg=None, alt=9000):
    return {"hex": hex_, "flight": flight, "t": "B738", "r": "PH-X", "alt_baro": alt,
            "nav_altitude_mcp": mcp, "nav_heading": hdg}


def snaps(*pairs):
    return [{"t": t, "aircraft": list(a)} for t, a in pairs]


ALT7000 = [Number("altitude", 7000, "level seven zero")]


class TestEchoClue:
    def test_a_single_change_to_the_spoken_altitude_matches(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("484161", "KLM12B", mcp=11008), ac("4bb299", "DAL73", mcp=6016)]),
            (T + 8, [ac("484161", "KLM12B", mcp=7008), ac("4bb299", "DAL73", mcp=6016)])))
        assert got["status"] == "match" and got["hex"] == "484161"
        assert got["delay_s"] == pytest.approx(8.0)

    def test_already_set_does_not_count(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("484161", "KLM12B", mcp=7008)]),
            (T + 8, [ac("484161", "KLM12B", mcp=7008)])))
        assert got["status"] == "none"

    def test_two_aircraft_changing_is_ambiguous(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("a1", "KLM1", mcp=9000), ac("a2", "KLM2", mcp=9000)]),
            (T + 20, [ac("a1", "KLM1", mcp=7008), ac("a2", "KLM2", mcp=6992)])))
        assert got["status"] == "ambiguous" and sorted(got["candidates"]) == ["a1", "a2"]

    def test_a_change_after_the_window_does_not_count(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("484161", "KLM12B", mcp=11008)]),
            (T + 45, [ac("484161", "KLM12B", mcp=11008)]),
            (T + 75, [ac("484161", "KLM12B", mcp=7008)])))
        assert got["status"] == "none"

    def test_an_aircraft_absent_before_the_window_is_not_a_candidate(self):
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, []), (T + 8, [ac("484161", "KLM12B", mcp=7008)])))
        assert got["status"] == "none"

    def test_a_missing_baseline_value_is_not_a_candidate(self):
        # present at/before t-10, but with no known value to have "changed" from
        got = fa.echo_clue(ALT7000, T, snaps(
            (T - 12, [ac("484161", "KLM12B", mcp=None)]),
            (T + 8, [ac("484161", "KLM12B", mcp=7008)])))
        assert got["status"] == "none"

    def test_heading_within_five_degrees_matches_across_north(self):
        got = fa.echo_clue([Number("heading", 360, "heading three six zero")], T, snaps(
            (T - 12, [ac("a1", "UAL947", hdg=250.0)]),
            (T + 10, [ac("a1", "UAL947", hdg=2.5)])))
        assert got["status"] == "match" and got["hex"] == "a1"

    def test_no_numbers_and_no_snapshots_are_distinct_from_none(self):
        assert fa.echo_clue([], T, snaps((T - 12, []), (T + 8, [])))["status"] == "no_numbers"
        assert fa.echo_clue(ALT7000, T, [])["status"] == "no_snapshots"
        # nothing at or before t-10: we cannot know what was "already set"
        assert fa.echo_clue(ALT7000, T, snaps((T + 8, []))) ["status"] == "no_snapshots"


CS_X = {"candidate": "KLM12B", "hex": "484161", "flight": "KLM12B", "type": "B738", "reg": "PH"}
CS_HEARD = {"candidate": "KLM1406", "hex": None, "flight": None, "type": None, "reg": None}
CS_NONE = {"candidate": None, "hex": None, "flight": None, "type": None, "reg": None}
E_X = {"status": "match", "hex": "484161"}
E_Y = {"status": "match", "hex": "4bb299"}
E_NONE = {"status": "none", "hex": None}


@pytest.mark.parametrize("cs, echo, enabled, expected", [
    (CS_X, E_X, True, ("flight", "484161", "confirmed")),
    (CS_X, E_NONE, True, ("flight", "484161", "callsign")),
    (CS_X, None, True, ("flight", "484161", "callsign")),
    (CS_NONE, E_Y, True, ("flight", "4bb299", "echo")),
    (CS_X, E_Y, True, ("review", "review", "conflict")),
    (CS_HEARD, E_Y, True, ("unknown", "heard:KLM1406", "unknown")),
    (CS_NONE, E_NONE, True, ("unassigned", "unassigned", "none")),
    (CS_NONE, {"status": "ambiguous", "hex": None}, True, ("unassigned", "unassigned", "none")),
    # shadow mode: the echo clue never changes the outcome
    (CS_X, E_X, False, ("flight", "484161", "callsign")),
    (CS_X, E_Y, False, ("flight", "484161", "callsign")),
    (CS_NONE, E_Y, False, ("unassigned", "unassigned", "none")),
])
def test_combine(cs, echo, enabled, expected):
    got = fa.combine(cs, echo, echo_enabled=enabled)
    assert (got["kind"], got["key"], got["badge"]) == expected


def test_combine_upper_cases_and_trims_the_heard_key():
    got = fa.combine({**CS_NONE, "candidate": "klm 14"}, None, echo_enabled=False)
    assert got["key"] == "heard:KLM14"


class TestStages:
    def test_stage_one_stores_the_callsign_outcome_immediately(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_X)
        monkeypatch.setattr(fa.adsb, "current_aircraft",
                            lambda: [ac("484161", "KLM12B", mcp=11008)])
        db = tmp_path / "c.db"
        tid = fa.record_transmission("Descend flight level seven zero, KLM one two bravo",
                                     "121,200", now=T, db_path=db)
        with air_archive.open_db(db) as conn:
            row = air_archive.get_transmission(conn, tid)
        assert row["outcome_key"] == "484161" and row["badge"] == "callsign"
        assert row["echo_clue"] is None
        assert row["numbers"] == [{"kind": "altitude", "value": 7000,
                                   "text": "descend flight level seven zero"}]
        assert row["state"]["flight"] == "KLM12B"
        assert re.search(r"[+-]\d\d:\d\d$", row["t"]), row["t"]   # offset kept

    def test_stage_one_never_raises(self, tmp_path, monkeypatch):
        def boom(_t):
            raise RuntimeError("matcher exploded")
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", boom)
        assert fa.record_transmission("x", "121.200", now=T, db_path=tmp_path / "c.db") is None

    def test_stage_two_waits_sixty_seconds_then_stores_the_echo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        monkeypatch.setattr(fa, "AIR_ECHO_ENABLED", False)
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero", "121.200",
                                     now=T, db_path=db)
        history = snaps((T - 12, [ac("484161", "KLM12B", mcp=11008)]),
                        (T + 8, [ac("484161", "KLM12B", mcp=7008)]))
        source = lambda t0, t1: [s for s in history if t0 <= s["t"] <= t1]   # noqa: E731
        assert fa.recheck_pending(now=T + 30, db_path=db, snapshots_between=source) == 0
        assert fa.recheck_pending(now=T + 61, db_path=db, snapshots_between=source) == 1
        with air_archive.open_db(db) as conn:
            row = air_archive.get_transmission(conn, tid)
        assert row["echo_clue"]["status"] == "match"
        # shadow mode: recorded, but the displayed outcome is still the callsign's
        assert row["outcome_key"] == "unassigned"

    def test_stage_two_with_echo_enabled_assigns_by_echo(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        monkeypatch.setattr(fa, "AIR_ECHO_ENABLED", True)
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero", "121.200",
                                     now=T, db_path=db)
        history = snaps((T - 12, [ac("484161", "KLM12B", mcp=11008)]),
                        (T + 8, [ac("484161", "KLM12B", mcp=7008)]))
        fa.recheck_pending(now=T + 61, db_path=db,
                           snapshots_between=lambda a, b: [s for s in history
                                                           if a <= s["t"] <= b])
        with air_archive.open_db(db) as conn:
            row = air_archive.get_transmission(conn, tid)
        assert row["outcome_key"] == "484161" and row["badge"] == "echo"
        # Spec Section 5: state is the aircraft's state AT THAT MOMENT (t), not the end of the
        # lookback window -- the value at T-12 (11008), not the post-clearance value at T+8.
        assert row["state"]["nav_altitude_mcp"] == 11008

    def test_stage_two_keeps_stage_one_state_when_the_outcome_key_is_unchanged(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_X)
        monkeypatch.setattr(fa.adsb, "current_aircraft",
                            lambda: [ac("484161", "KLM12B", mcp=11008)])
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero, KLM one two bravo",
                                     "121.200", now=T, db_path=db)
        history = snaps((T - 12, [ac("484161", "KLM12B", mcp=11008)]),
                        (T + 8, [ac("484161", "KLM12B", mcp=7008)]))
        fa.recheck_pending(now=T + 61, db_path=db,
                           snapshots_between=lambda a, b: [s for s in history
                                                           if a <= s["t"] <= b])
        with air_archive.open_db(db) as conn:
            row = air_archive.get_transmission(conn, tid)
        # The callsign already resolved this row to 484161 at stage 1; stage 2 confirms the
        # same key (with or without echo), so stage 1's state must be left untouched rather
        # than overwritten with whatever aircraft state exists later in the window.
        assert row["outcome_key"] == "484161"
        assert row["state"]["nav_altitude_mcp"] == 11008

    def test_stage_two_isolates_a_failing_row_and_still_completes_the_rest(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        db = tmp_path / "c.db"
        tid1 = fa.record_transmission("descend flight level seven zero", "121.200",
                                      now=T, db_path=db)
        tid2 = fa.record_transmission("descend flight level seven zero", "121.200",
                                      now=T + 1, db_path=db)
        history = snaps((T - 12, [ac("484161", "KLM12B", mcp=11008)]),
                        (T + 8, [ac("484161", "KLM12B", mcp=7008)]))
        calls = {"n": 0}

        def flaky(a, b):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return [s for s in history if a <= s["t"] <= b]

        done = fa.recheck_pending(now=T + 61, db_path=db, snapshots_between=flaky)
        assert done == 1
        with air_archive.open_db(db) as conn:
            row1 = air_archive.get_transmission(conn, tid1)
            row2 = air_archive.get_transmission(conn, tid2)
        assert row1["echo_clue"] is None           # the row whose source call raised
        assert row2["echo_clue"]["status"] == "match"   # later row still completed

    def test_a_restart_with_no_snapshot_evidence_is_no_snapshots_not_none(
            self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero", "121.200",
                                     now=T, db_path=db)
        fa.recheck_pending(now=T + 3600, db_path=db, snapshots_between=lambda a, b: [])
        with air_archive.open_db(db) as conn:
            assert air_archive.get_transmission(conn, tid)["echo_clue"]["status"] == \
                "no_snapshots"

    def test_a_restart_recovers_evidence_from_the_daily_log(self, tmp_path, monkeypatch):
        """Review Focus 3: the default snapshot source is adsb.snapshots_between, whose log
        fallback survives a restart that emptied the ring."""
        import datetime
        import json
        now = datetime.datetime(2026, 9, 24, 11, 0, 0).astimezone().timestamp()
        before = {"aircraft": [ac("484161", "KLM12B", mcp=11008)]}
        after = {"aircraft": [ac("484161", "KLM12B", mcp=7008)]}
        adsb.poll_once(0, 0, 0, fetch=lambda _u: json.dumps(before).encode(), now=now - 12)
        adsb.poll_once(0, 0, 0, fetch=lambda _u: json.dumps(after).encode(), now=now + 8)
        adsb.reset_snapshot_ring()
        monkeypatch.setattr(fa.flight_identify, "callsign_clue", lambda _t: CS_NONE)
        monkeypatch.setattr(fa.adsb, "current_aircraft", lambda: [])
        db = tmp_path / "c.db"
        tid = fa.record_transmission("descend flight level seven zero", "121.200",
                                     now=now, db_path=db)
        fa.recheck_pending(now=now + 3600, db_path=db)
        with air_archive.open_db(db) as conn:
            assert air_archive.get_transmission(conn, tid)["echo_clue"]["status"] == "match"


def test_the_channel_list_covers_both_separators():
    assert {"121.200", "121,200", "121.205", "121,205"} == set(fa.AIR_CONVERSATION_CHANNELS)
