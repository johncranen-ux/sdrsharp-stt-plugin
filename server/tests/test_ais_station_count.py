"""What the counter reports about its own last look at the station.

`polls` and `poll_failures` are running totals, and a total cannot answer the question the
control panel asks: did the MOST RECENT poll work? A station that failed once an hour ago and
has been fine since carries the same non-zero `poll_failures` as one that is down right now.
The dashboard lamp needs the last outcome, so `Coverage` records it.
"""
import io
import json
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import ais_station_count as counter  # noqa: E402


@contextmanager
def _answering(payload):
    yield io.BytesIO(json.dumps(payload).encode("utf-8"))


def _coverage():
    return counter.Coverage("192.0.2.1:8100", counter.RX_LAT, counter.RX_LON)


def test_a_fresh_coverage_has_not_polled_yet(monkeypatch):
    """None, not False. "We have never asked" and "we asked and it failed" are different
    facts, and a lamp that shows the second when it means the first is lying."""
    snapshot = _coverage().snapshot()
    assert snapshot["last_poll_ok"] is None
    assert snapshot["last_poll_at"] is None


def test_a_poll_that_answers_is_recorded_as_the_last_outcome(monkeypatch):
    monkeypatch.setattr(counter.urllib.request, "urlopen",
                        lambda url, timeout=None: _answering({"ships": []}))
    coverage = _coverage()
    assert coverage.poll() is None

    snapshot = coverage.snapshot()
    assert snapshot["last_poll_ok"] is True
    assert snapshot["last_poll_at"] is not None


def test_a_poll_that_fails_is_recorded_as_the_last_outcome(monkeypatch):
    def _refuse(url, timeout=None):
        raise ConnectionRefusedError("nobody home")

    monkeypatch.setattr(counter.urllib.request, "urlopen", _refuse)
    coverage = _coverage()
    assert coverage.poll() is not None

    snapshot = coverage.snapshot()
    assert snapshot["last_poll_ok"] is False
    # Timestamped even though it failed: "we last tried at 10:04 and it did not answer" is
    # what tells the watchkeeper how long the station has been unreachable.
    assert snapshot["last_poll_at"] is not None


def test_recovery_clears_the_last_outcome_while_the_running_total_remembers(monkeypatch):
    """The lamp goes green again; the audit trail does not forget the failure."""
    responses = iter([ConnectionRefusedError("nobody home"), {"ships": []}])

    def _next(url, timeout=None):
        answer = next(responses)
        if isinstance(answer, Exception):
            raise answer
        return _answering(answer)

    monkeypatch.setattr(counter.urllib.request, "urlopen", _next)
    coverage = _coverage()
    coverage.poll()
    coverage.poll()

    snapshot = coverage.snapshot()
    assert snapshot["last_poll_ok"] is True
    assert snapshot["poll_failures"] == 1


def test_the_timestamp_is_utc_and_parses_back(monkeypatch):
    """health.py reads this string out of the heartbeat log and subtracts it from now, so it
    must round-trip through fromisoformat and carry a zone."""
    monkeypatch.setattr(counter.urllib.request, "urlopen",
                        lambda url, timeout=None: _answering({"ships": []}))
    coverage = _coverage()
    coverage.poll()

    parsed = counter.datetime.fromisoformat(coverage.snapshot()["last_poll_at"])
    assert parsed.tzinfo is not None
    assert parsed.utcoffset().total_seconds() == 0
