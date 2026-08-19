# server/tests/test_proxy_data.py
"""Getting the proxy's collections without letting the proxy's problems become ours.

Two rules shape this: a browser must never be handed 1.8 MB, and a proxy that is slow or gone
must degrade the screen rather than hang it. The panel therefore holds the last good copy and
says how old it is.
"""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import proxy_data  # noqa: E402


class _Clock:
    def __init__(self): self.now = 1000.0
    def __call__(self): return self.now


def _data(answers, clock=None):
    """answers: dict of url-substring -> payload or Exception, consulted per call."""
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        for key, answer in answers.items():
            if key in url:
                if isinstance(answer, Exception):
                    raise answer
                return answer(url) if callable(answer) else answer
        raise AssertionError(f"unexpected url {url}")

    data = proxy_data.ProxyData(lambda: {"PROXY_PORT": "9000"}, fetch=fetch,
                                clock=clock or _Clock())
    return data, calls


def test_a_first_call_fetches_and_reports_a_fresh_snapshot():
    data, calls = _data({"conversations": [{"vessel": "PASHA"}]})
    records, snap = data.conversations()
    assert records == [{"vessel": "PASHA"}]
    assert snap.stale is False and snap.error is None and snap.count == 1
    assert len(calls) == 1


def test_a_second_call_inside_the_ttl_does_not_hit_the_proxy_again():
    clock = _Clock()
    data, calls = _data({"conversations": [{"vessel": "PASHA"}]}, clock)
    data.conversations()
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC - 1
    data.conversations()
    assert len(calls) == 1


def test_the_ttl_expiring_refetches():
    clock = _Clock()
    data, calls = _data({"conversations": [{"vessel": "PASHA"}]}, clock)
    data.conversations()
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC + 1
    data.conversations()
    assert len(calls) == 2


def test_a_failed_fetch_serves_the_last_good_copy_marked_stale():
    """The screen keeps showing what it last knew, labelled. An empty table would read as
    'there are no conversations', which is a different and false claim."""
    clock = _Clock()
    answers = {"conversations": [{"vessel": "PASHA"}]}
    data, _ = _data(answers, clock)
    data.conversations()

    answers["conversations"] = ConnectionRefusedError("nobody home")
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC + 1
    records, snap = data.conversations()

    assert records == [{"vessel": "PASHA"}]
    assert snap.stale is True
    assert "ConnectionRefusedError" in snap.error
    assert snap.age_sec == pytest.approx(proxy_data.CONVERSATIONS_TTL_SEC + 1)


def test_a_failure_with_nothing_cached_yet_is_an_empty_result_that_says_why():
    data, _ = _data({"conversations": ConnectionRefusedError("nobody home")})
    records, snap = data.conversations()
    assert records == []
    assert snap.stale is True and snap.error is not None and snap.count == 0
    # Distinct from the "last copy, stale" case below: there has never been a successful
    # fetch, so age_sec being 0.0 must not be read as "the copy is 0s old" -- has_data says
    # plainly that there is no copy at all.
    assert snap.has_data is False


def test_a_failed_fetch_that_did_succeed_before_still_reports_it_has_data():
    clock = _Clock()
    answers = {"conversations": [{"vessel": "PASHA"}]}
    data, _ = _data(answers, clock)
    data.conversations()

    answers["conversations"] = ConnectionRefusedError("nobody home")
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC + 1
    _, snap = data.conversations()
    assert snap.has_data is True


def test_a_payload_that_is_not_a_list_is_refused_rather_than_rendered():
    data, _ = _data({"conversations": {"unexpected": "shape"}})
    records, snap = data.conversations()
    assert records == []
    # "shape" alone is inside the fixed prefix ("unexpected response shape: ") and would pass
    # for any payload -- assert on the payload-specific part, the type name, instead.
    assert "dict" in snap.error


def test_conversations_and_vessels_have_independent_caches():
    clock = _Clock()
    data, calls = _data({"conversations": [{"a": 1}], "ais-cache": [{"b": 2}]}, clock)
    data.conversations()
    data.vessels()
    clock.now += proxy_data.CONVERSATIONS_TTL_SEC + 1     # conversations stale, vessels not
    data.conversations()
    data.vessels()
    assert sum("conversations" in c for c in calls) == 2
    assert sum("ais-cache" in c for c in calls) == 1
