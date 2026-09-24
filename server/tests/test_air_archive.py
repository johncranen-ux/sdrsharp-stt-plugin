import air_archive


def _row(epoch, key="484161", kind="flight", badge="callsign", **extra):
    base = {"t": f"2026-09-24T11:{int(epoch) // 60 % 60:02d}:00+02:00", "epoch": float(epoch),
            "channel": "121.200", "text": "Descend flight level seven zero",
            "numbers": [{"kind": "altitude", "value": 7000, "text": "level seven zero"}],
            "callsign_clue": {"candidate": "KLM12B", "hex": "484161", "flight": "KLM12B",
                              "type": "B738", "reg": "PH-BXH"},
            "state": {"flight": "KLM12B"}, "outcome_kind": kind, "outcome_key": key,
            "badge": badge}
    base.update(extra)
    return base


def test_ids_are_stable_across_reopening(tmp_path):
    db = tmp_path / "conversations.db"
    with air_archive.open_db(db) as conn:
        first = air_archive.insert_transmission(conn, _row(100))
    with air_archive.open_db(db) as conn:
        second = air_archive.insert_transmission(conn, _row(200))
        assert second == first + 1
        assert air_archive.get_transmission(conn, first)["numbers"][0]["value"] == 7000


def test_pending_echo_lists_only_rows_old_enough_and_unchecked(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        old = air_archive.insert_transmission(conn, _row(100))
        air_archive.insert_transmission(conn, _row(500))
        done = air_archive.insert_transmission(conn, _row(90))
        air_archive.set_echo(conn, done, {"status": "none"},
                             {"kind": "flight", "key": "484161", "badge": "callsign"}, None)
        assert [r["id"] for r in air_archive.pending_echo(conn, 200.0)] == [old]


def test_set_echo_updates_clue_outcome_and_state(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        tid = air_archive.insert_transmission(conn, _row(100))
        air_archive.set_echo(conn, tid, {"status": "match", "hex": "484161"},
                             {"kind": "flight", "key": "484161", "badge": "confirmed"},
                             {"flight": "KLM12B", "nav_altitude_mcp": 7008})
        got = air_archive.get_transmission(conn, tid)
        assert got["echo_clue"]["status"] == "match"
        assert got["badge"] == "confirmed"
        assert got["state"]["nav_altitude_mcp"] == 7008


def test_the_latest_move_wins_and_moves_are_kept(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        tid = air_archive.insert_transmission(conn, _row(100))
        first = air_archive.add_move(conn, tid, "unassigned", now="2026-09-24T12:00:00")
        second = air_archive.add_move(conn, tid, "4bb299", now="2026-09-24T12:01:00")
        assert first["from_key"] == "484161"
        assert second["from_key"] == "unassigned"
        row = air_archive.transmissions(conn, 0, 1_000)[0]
        assert row["effective_key"] == "4bb299" and row["moved"] is True
        count = conn.execute("SELECT COUNT(*) FROM air_moves").fetchone()[0]
        assert count == 2


def test_moving_a_missing_transmission_writes_nothing(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        assert air_archive.add_move(conn, 999, "unassigned") is None
        assert conn.execute("SELECT COUNT(*) FROM air_moves").fetchone()[0] == 0


def test_range_query_is_inclusive_and_ordered(tmp_path):
    with air_archive.open_db(tmp_path / "c.db") as conn:
        for e in (300, 100, 200, 400):
            air_archive.insert_transmission(conn, _row(e))
        assert [r["epoch"] for r in air_archive.transmissions(conn, 100, 300)] == [100, 200, 300]


def test_valid_key():
    for good in ("unassigned", "review", "heard:KLM1406", "484161", "4BB299"):
        assert air_archive.valid_key(good), good
    for bad in ("", "heard:", "heard:" + "X" * 13, "48416", "zzzzzz", "flight", "heard:a b"):
        assert not air_archive.valid_key(bad), bad


def test_the_real_archive_is_unreachable_from_tests():
    import pytest
    import conversation_archive
    from pathlib import Path
    real = conversation_archive.default_db_path(Path(air_archive.__file__).parent)
    with pytest.raises(AssertionError):
        with air_archive.open_db(real):
            pass
