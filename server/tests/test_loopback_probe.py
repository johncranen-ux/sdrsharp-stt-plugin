"""Tests for the loopback probe.

The verdict logic is what these mostly cover, because the verdict is the whole point: the
probe exists to tell a fixed-timeout signature apart from ordinary scatter, and getting that
call wrong would send the next session chasing the fault it already refuted.

The one end-to-end test uses a small payload deliberately. 380 B measured 0/200 failures on
this machine, so a small transfer is the reliable end of the curve -- a test that sent 1.8 MB
would inherit the very intermittency it is meant to be independent of.
"""
import json
import socket

import loopback_probe as lp


def _trial(index=0, elapsed=0.04, received=1000, expected=1000, outcome="ok"):
    return lp.Trial(index=index, elapsed=elapsed, received=received, expected=expected,
                    outcome=outcome)


def _ok(index=0, elapsed=0.04):
    return _trial(index=index, elapsed=elapsed)


def _reset(index=0, elapsed=18.95, received=967_000, expected=1_000_000):
    return _trial(index=index, elapsed=elapsed, received=received, expected=expected,
                  outcome="reset")


class TestBuildPayload:
    def test_is_exactly_the_requested_size(self):
        for size in (1, 63, 64, 1000, 100_000):
            assert len(lp.build_payload(size)) == size

    def test_default_payload_is_ascii_text(self):
        payload = lp.build_payload(500)
        assert payload.decode("ascii")

    def test_random_payload_is_the_requested_size_too(self):
        assert len(lp.build_payload(4096, random_bytes=True)) == 4096

    def test_random_payload_differs_from_the_text_one(self):
        assert lp.build_payload(4096, random_bytes=True) != lp.build_payload(4096)


class TestTrial:
    def test_ok_is_not_a_failure(self):
        assert _ok().failed is False

    def test_every_other_outcome_is_a_failure(self):
        for outcome in ("reset", "short", "timeout", "error"):
            assert _trial(outcome=outcome).failed is True

    def test_shortfall_is_what_never_arrived(self):
        assert _reset(received=967_000, expected=1_000_000).shortfall == 33_000


class TestSummariseVerdict:
    def test_no_trials_says_so_rather_than_dividing_by_zero(self):
        summary = lp.summarise([])
        assert summary.verdict == "NO DATA"
        assert summary.runs == 0
        assert summary.failure_rate == 0.0

    def test_all_successes_is_clean(self):
        summary = lp.summarise([_ok(i) for i in range(24)])
        assert summary.failures == 0
        assert "CLEAN" in summary.verdict

    def test_failures_in_a_tight_band_are_the_fixed_timeout_signature(self):
        trials = [_ok(0), _reset(1, 18.93), _ok(2), _reset(3, 18.97), _reset(4, 19.00)]
        summary = lp.summarise(trials)
        assert "FAULT REPRODUCED" in summary.verdict
        assert "18.97" in summary.verdict

    def test_scattered_failures_are_reported_as_a_different_fault(self):
        trials = [_reset(0, 2.0), _reset(1, 11.0), _reset(2, 25.0)]
        summary = lp.summarise(trials)
        assert "SCATTERED" in summary.verdict
        assert "FAULT REPRODUCED" not in summary.verdict

    def test_a_lone_failure_refuses_to_call_a_signature(self):
        # One point is trivially "within 1.0s of itself", so without this branch a single
        # failure would read as a confirmed signature.
        summary = lp.summarise([_ok(0), _reset(1, 18.95), _ok(2)])
        assert "too few" in summary.verdict
        assert "FAULT REPRODUCED" not in summary.verdict

    def test_the_boundary_spread_still_counts_as_one_timeout(self):
        trials = [_reset(0, 18.00), _reset(1, 19.00)]
        assert "FAULT REPRODUCED" in lp.summarise(trials).verdict

    def test_just_past_the_boundary_does_not(self):
        trials = [_reset(0, 18.00), _reset(1, 19.01)]
        assert "SCATTERED" in lp.summarise(trials).verdict


class TestSummariseNumbers:
    def test_counts_and_rate(self):
        summary = lp.summarise([_ok(0), _ok(1), _reset(2), _reset(3)])
        assert summary.runs == 4
        assert summary.failures == 2
        assert summary.failure_rate == 0.5

    def test_the_two_clusters_are_kept_apart(self):
        trials = [_ok(0, 0.03), _ok(1, 0.05), _reset(2, 18.93), _reset(3, 18.99)]
        summary = lp.summarise(trials)
        assert summary.ok_elapsed == {"n": 2, "min": 0.03, "median": 0.04, "max": 0.05}
        assert summary.fail_elapsed["n"] == 2
        assert summary.fail_elapsed["min"] == 18.93

    def test_shortfall_is_measured_over_failures_only(self):
        trials = [_ok(0), _reset(1, received=967_000, expected=1_000_000)]
        assert lp.summarise(trials).shortfall == {"n": 1, "min": 33_000.0,
                                                  "median": 33_000.0, "max": 33_000.0}

    def test_a_failure_that_lost_nothing_is_left_out_of_the_shortfall(self):
        # A timeout with the whole payload already received has no bytes missing, and
        # averaging a zero into the shortfall would understate what the fault costs.
        trials = [_trial(0, elapsed=60.0, received=1000, expected=1000, outcome="timeout")]
        assert lp.summarise(trials).shortfall == {}

    def test_outcomes_are_tallied_by_kind(self):
        trials = [_ok(0), _ok(1), _reset(2), _trial(3, outcome="short")]
        assert lp.summarise(trials).outcomes == {"ok": 2, "reset": 1, "short": 1}

    def test_size_comes_from_the_trials(self):
        assert lp.summarise([_reset(0, expected=1_800_000)]).size == 1_800_000

    def test_label_is_carried_through(self):
        assert lp.summarise([_ok()], "before").label == "before"


class TestFormatSummary:
    def test_reports_the_headline_counts(self):
        text = lp.format_summary(lp.summarise([_ok(0), _reset(1)], "before"))
        assert "[before]" in text
        assert "1/2 transfers failed" in text
        assert "50%" in text

    def test_omits_empty_clusters_rather_than_printing_blanks(self):
        text = lp.format_summary(lp.summarise([_ok(0), _ok(1)]))
        assert "succeeded" in text
        assert "failed    (" not in text
        assert "bytes lost" not in text

    def test_always_ends_with_the_verdict(self):
        text = lp.format_summary(lp.summarise([_ok()]))
        assert text.splitlines()[-1].startswith("  VERDICT:")


class TestCompare:
    def test_a_clean_baseline_is_inconclusive_not_a_success(self):
        # The failure rate is intermittent, so "before was clean" proves nothing about the
        # change -- and reading it as a win is the easiest mistake this tool could invite.
        before = lp.summarise([_ok(i) for i in range(24)], "before")
        after = lp.summarise([_ok(i) for i in range(24)], "after")
        assert "INCONCLUSIVE" in lp.compare(before, after)

    def test_failures_disappearing_implicates_the_change_and_asks_for_a_revert(self):
        before = lp.summarise([_reset(0), _reset(1), _ok(2)], "before")
        after = lp.summarise([_ok(i) for i in range(24)], "after")
        text = lp.compare(before, after)
        assert "CHANGE HELPED" in text
        assert "reverting" in text

    def test_an_unchanged_rate_rules_the_change_out(self):
        before = lp.summarise([_reset(0), _ok(1)], "before")
        after = lp.summarise([_reset(0), _ok(1)], "after")
        assert "NO EFFECT" in lp.compare(before, after)

    def test_a_halved_rate_is_only_partial(self):
        before = lp.summarise([_reset(0), _reset(1), _reset(2), _ok(3)], "before")
        after = lp.summarise([_reset(0), _ok(1), _ok(2), _ok(3),
                              _ok(4), _ok(5), _ok(6), _ok(7)], "after")
        assert "PARTIAL" in lp.compare(before, after)

    def test_both_summaries_are_shown_in_full(self):
        before = lp.summarise([_reset(0)], "before")
        after = lp.summarise([_ok(0)], "after")
        text = lp.compare(before, after)
        assert "[before]" in text and "[after]" in text


class TestEndToEnd:
    def test_small_transfers_complete_and_are_measured(self):
        trials = lp.probe(size=1000, runs=3, timeout=30.0)
        assert len(trials) == 3
        assert [t.outcome for t in trials] == ["ok", "ok", "ok"]
        assert all(t.received == 1000 for t in trials)
        assert all(t.elapsed >= 0 for t in trials)

    def test_progress_is_called_once_per_trial_as_it_happens(self):
        seen = []
        lp.probe(size=500, runs=3, timeout=30.0, progress=seen.append)
        assert [t.index for t in seen] == [0, 1, 2]

    def test_main_writes_a_reloadable_summary(self, tmp_path, capsys):
        out = tmp_path / "run.json"
        assert lp.main(["--size", "500", "--runs", "2", "--label", "before",
                        "--out", str(out)]) == 0
        payload = json.loads(out.read_text(encoding="utf-8"))
        assert payload["summary"]["runs"] == 2
        assert len(payload["trials"]) == 2
        # compare() reads these files back, so the round trip has to hold.
        assert lp._load(str(out)).label == "before"

    def test_compare_reads_two_written_files(self, tmp_path, capsys):
        before, after = tmp_path / "b.json", tmp_path / "a.json"
        lp.main(["--size", "500", "--runs", "2", "--out", str(before)])
        lp.main(["--size", "500", "--runs", "2", "--out", str(after)])
        capsys.readouterr()
        assert lp.main(["--compare", str(before), str(after)]) == 0
        assert "INCONCLUSIVE" in capsys.readouterr().out


class TestPortSelection:
    def test_a_named_port_is_actually_used(self):
        # The whole point of --port is testing a rule attached to a listening service, so a
        # silently-ignored port would make a firewall/IDS result meaningless.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
            probe_socket.bind(("127.0.0.1", 0))
            free_port = probe_socket.getsockname()[1]
        trials = lp.probe(size=500, runs=1, timeout=30.0, port=free_port)
        assert trials[0].outcome == "ok"

    def test_a_wildcard_bound_port_is_refused_too(self, capsys):
        # The one that actually bit, 2026-08-20. The live proxy listens on 0.0.0.0:9000, and
        # on Windows a wildcard bind does NOT exclude a later 127.0.0.1 bind on the same port
        # -- the more specific bind simply wins for incoming connections. So this probe
        # silently HIJACKED the port the SDR# plugin posts audio to, while the proxy was
        # carrying live radio traffic. Binding must fail here, not succeed quietly.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("0.0.0.0", 0))
            held.listen(1)
            busy_port = held.getsockname()[1]
            assert lp.main(["--size", "500", "--runs", "1", "--port", str(busy_port)]) == 3
        assert "must not share a port" in capsys.readouterr().err

    def test_a_busy_port_is_refused_rather_than_shared(self, capsys):
        # Binding must fail loudly: this project has twice had a second listener quietly join
        # a port that was already carrying live traffic.
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
            held.bind(("127.0.0.1", 0))
            held.listen(1)
            busy_port = held.getsockname()[1]
            assert lp.main(["--size", "500", "--runs", "1", "--port", str(busy_port)]) == 3
        assert "must not share a port" in capsys.readouterr().err


class TestTimeoutGuard:
    def test_a_timeout_under_the_give_up_point_is_refused(self, capsys):
        # A 10s receive timeout would abort at 10s and be recorded as "timeout", hiding the
        # 18.9s arithmetic that is the entire diagnostic value of the run.
        assert lp.main(["--timeout", "10"]) == 2
        assert "18.9s give-up point" in capsys.readouterr().err

    def test_exactly_nineteen_is_still_refused(self):
        assert lp.main(["--timeout", "19"]) == 2
