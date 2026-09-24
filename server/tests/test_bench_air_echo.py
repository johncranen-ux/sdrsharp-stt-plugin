import bench_air_echo as bench


def r(cs_hex=None, cand=None, echo=None, moved=False, key="unassigned"):
    return {"callsign_clue": {"candidate": cand or (cs_hex and "X"), "hex": cs_hex},
            "echo_clue": {"status": "match", "hex": echo} if echo else {"status": "none"},
            "moved": moved, "effective_key": key}


def test_a_clean_pass():
    rows = [r(cs_hex="a1", echo="a1", key="a1") for _ in range(50)]
    rows += [r(echo="b2") for _ in range(2)] + [r() for _ in range(8)]
    rows += [r(cs_hex="a1", echo="c3", moved=True, key="c3")]
    got = bench.gate(rows)
    assert got["overlap"] == 51 and got["agree"] == 50
    assert got["agreement"] >= 0.95
    assert got["echo_right_on_moves"] == 1 and got["callsign_right_on_moves"] == 0
    assert got["recovery"] == 0.2
    assert got["passes"] is True and got["reasons"] == []


def test_too_few_overlaps_fails_even_at_perfect_agreement():
    rows = [r(cs_hex="a1", echo="a1", key="a1") for _ in range(49)] + [r(echo="b2")] * 5
    got = bench.gate(rows)
    assert got["passes"] is False
    assert any("50" in reason for reason in got["reasons"])


def test_disagreement_fails():
    rows = ([r(cs_hex="a1", echo="a1", key="a1")] * 45 + [r(cs_hex="a1", echo="zz")] * 5
            + [r(echo="b2")] * 5)
    assert bench.gate(rows)["passes"] is False


def test_echo_worse_than_callsign_on_moves_fails():
    rows = [r(cs_hex="a1", echo="a1", key="a1")] * 60 + [r(echo="b2")] * 5
    rows += [r(cs_hex="k1", echo="zz", moved=True, key="k1")]
    got = bench.gate(rows)
    assert got["passes"] is False


def test_no_data_is_not_a_pass():
    got = bench.gate([])
    assert got["passes"] is False and got["agreement"] is None
