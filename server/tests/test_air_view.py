import pytest

from webapp import air_view

NOW = 100_000.0


def row(tid, epoch, key, kind="flight", badge="callsign", moved=False, text="x",
        clue=None, echo=None, state=None):
    return {"id": tid, "t": f"2026-09-24T11:00:{tid:02d}+02:00", "epoch": epoch, "text": text,
            "effective_key": key, "outcome_kind": kind, "outcome_key": key, "badge": badge,
            "moved": moved, "numbers": [],
            "callsign_clue": clue or {"candidate": None, "hex": None, "flight": None,
                                      "type": None, "reg": None},
            "echo_clue": echo,
            "state": state}


KLM = {"flight": "KLM12B", "t": "B738", "r": "PH-BXH", "airline": "KLM",
       "alt_baro": 9725, "nav_altitude_mcp": 7008, "nav_heading": 51.3}
DAL = {"flight": "DAL73", "t": "B763", "r": "N183DN", "airline": "Delta",
       "alt_baro": 7200, "nav_altitude_mcp": 6016, "nav_heading": 245.0}


def test_strips_group_by_effective_key_and_sort_flights_first():
    rows = [row(1, NOW - 300, "484161", state=KLM), row(2, NOW - 100, "4bb299", state=DAL),
            row(3, NOW - 50, "unassigned", kind="unassigned", badge="none"),
            row(4, NOW - 40, "review", kind="review", badge="conflict"),
            row(5, NOW - 30, "heard:KLM1406", kind="unknown", badge="unknown"),
            row(6, NOW - 20, "484161", state=KLM)]
    got = air_view.strips(rows, NOW, live=True)
    assert [s["key"] for s in got] == ["484161", "4bb299", "review", "heard:KLM1406",
                                       "unassigned"]
    klm = got[0]
    assert klm["label"] == "KLM12B" and klm["count"] == 2 and klm["type"] == "B738"
    assert klm["state"]["nav_altitude_mcp"] == 7008
    assert got[3]["label"] == "KLM1406"


def test_a_moved_row_counts_where_it_was_moved_to():
    rows = [row(1, NOW - 10, "4bb299", moved=True, state=None),
            row(2, NOW - 20, "4bb299", state=DAL)]
    got = air_view.strips(rows, NOW, live=True)
    assert got[0]["count"] == 2 and got[0]["label"] == "DAL73"


def test_a_moved_row_never_lends_its_old_aircraft_to_its_new_strip():
    # A KLM12B row moved to bbbbbb still carries KLM12B's stage-1 state.
    rows = [row(1, NOW - 30, "bbbbbb", state=DAL),
            row(2, NOW - 10, "bbbbbb", moved=True, state=KLM)]
    got = air_view.strips(rows, NOW, live=True)
    assert got[0]["label"] == "DAL73" and got[0]["airline"] == "Delta"
    assert got[0]["type"] == "B763" and got[0]["state"] == DAL


def test_a_strip_with_only_moved_rows_falls_back_to_its_key():
    rows = [row(1, NOW - 10, "bbbbbb", moved=True, state=KLM)]
    got = air_view.strips(rows, NOW, live=True)
    assert got[0]["label"] == "BBBBBB" and got[0]["state"] is None
    assert got[0]["airline"] is None and got[0]["type"] is None and got[0]["reg"] is None


def test_a_row_moved_to_unassigned_lends_it_nothing():
    rows = [row(1, NOW - 10, "unassigned", kind="unassigned", badge="none", moved=True,
                state=KLM)]
    got = air_view.strips(rows, NOW, live=True)
    assert got[0]["label"] == "Unassigned" and got[0]["state"] is None
    assert got[0]["airline"] is None and got[0]["type"] is None and got[0]["reg"] is None


def test_move_targets_ignore_a_moved_rows_state():
    rows = [row(1, NOW - 30, "bbbbbb", state=DAL),
            row(2, NOW - 10, "bbbbbb", moved=True, state=KLM),
            row(3, NOW - 5, "cccccc", moved=True, state=KLM)]
    got = {t["key"]: t["label"] for t in air_view.move_targets(rows, NOW)}
    assert got["bbbbbb"] == "DAL73"
    assert got["cccccc"] == "CCCCCC"


def test_live_drops_flights_silent_for_fifteen_minutes_history_keeps_them():
    rows = [row(1, NOW - 901, "484161", state=KLM), row(2, NOW - 10, "4bb299", state=DAL)]
    assert [s["key"] for s in air_view.strips(rows, NOW, live=True)] == ["4bb299"]
    assert len(air_view.strips(rows, NOW, live=False)) == 2


def test_thread_is_oldest_first_and_explains_itself():
    rows = [row(2, NOW - 10, "484161", badge="confirmed", state=KLM,
                clue={"candidate": "KLM12B", "hex": "484161", "flight": "KLM12B",
                      "type": "B738", "reg": "PH"},
                echo={"status": "match", "hex": "484161", "flight": "KLM12B",
                      "number": {"kind": "altitude", "value": 7000, "text": "l"},
                      "delay_s": 8.0}),
            row(1, NOW - 60, "484161", state=KLM),
            row(3, NOW - 5, "4bb299", state=DAL)]
    got = air_view.thread(rows, "484161")
    assert [r["id"] for r in got] == [1, 2]
    assert "KLM12B heard" in got[1]["evidence"]
    assert "selected 7000 ft 8 s later" in got[1]["evidence"]


def test_evidence_for_a_moved_row_says_so():
    assert "moved by hand" in air_view.evidence(row(1, NOW, "484161", moved=True))


def test_move_targets_are_nearby_flights_plus_unassigned():
    rows = [row(1, NOW - 700, "aaaaaa", state={**KLM, "flight": "OLD1"}),
            row(2, NOW - 100, "484161", state=KLM),
            row(3, NOW + 100, "4bb299", state=DAL),
            row(4, NOW, "review", kind="review", badge="conflict")]
    keys = [t["key"] for t in air_view.move_targets(rows, NOW)]
    assert keys == ["484161", "4bb299", "unassigned"]


def test_parse_range():
    assert air_view.parse_range(None, None, NOW) == (NOW - 900, NOW, True)
    frm, to, live = air_view.parse_range("2026-09-24T11:00:00+02:00",
                                         "2026-09-24T12:00:00+02:00", NOW)
    assert to - frm == 3600 and live is False
    with pytest.raises(ValueError):
        air_view.parse_range("yesterday", None, NOW)
    with pytest.raises(ValueError):
        air_view.parse_range("2026-09-24T12:00:00+02:00", "2026-09-24T11:00:00+02:00", NOW)


def test_evidence_says_when_a_callsign_was_read_from_qnh():
    r = row(1, NOW, "484abc", clue={"candidate": "KLM604", "hex": "484abc", "flight": "KLM604",
                                    "type": "E190", "reg": None, "repaired_from": "QNH"})
    assert 'KLM604 read from "QNH"' in air_view.evidence(r)


KLM67F = {"flight": "KLM67F", "t": "E295", "r": "PH-NXN", "airline": "KLM", "alt_baro": 10150}


def test_a_hand_made_flight_takes_its_name_from_the_move():
    moved = row(1, NOW - 10, "4864eb", moved=True, state=KLM)
    moved["moved_state"] = KLM67F
    got = air_view.strips([moved], NOW, live=True)
    assert got[0]["label"] == "KLM67F" and got[0]["type"] == "E295"
    assert got[0]["reg"] == "PH-NXN" and got[0]["airline"] == "KLM"


def test_a_hand_made_flight_is_offered_by_name_as_a_move_target():
    moved = row(1, NOW - 10, "4864eb", moved=True, state=KLM)
    moved["moved_state"] = KLM67F
    got = {t["key"]: t["label"] for t in air_view.move_targets([moved], NOW)}
    assert got["4864eb"] == "KLM67F"
