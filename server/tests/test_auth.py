"""Sessions, CSRF and the guard that stops the panel opening on a LAN with no password."""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.auth import (  # noqa: E402
    LoginThrottle, SessionStore, TooManyAttempts, UnsafeBind, check_bind_allowed,
)


class _Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


def test_a_created_session_is_retrievable_by_its_token():
    store = SessionStore()
    session = store.create()
    assert store.get(session.token) is session
    assert store.get("not-a-token") is None
    assert store.get(None) is None


def test_each_session_gets_its_own_csrf_token():
    store = SessionStore()
    first, second = store.create(), store.create()
    assert first.csrf != second.csrf
    assert first.token != second.token
    assert len(first.csrf) >= 32


def test_a_session_expires_after_the_idle_window():
    clock = _Clock()
    store = SessionStore(idle_sec=100, clock=clock)
    session = store.create()
    clock.now += 99
    assert store.get(session.token) is not None   # activity refreshes it
    clock.now += 99
    assert store.get(session.token) is not None
    clock.now += 101
    assert store.get(session.token) is None


def test_a_session_expires_at_its_absolute_ttl_however_active():
    clock = _Clock()
    store = SessionStore(ttl_sec=200, idle_sec=1_000, clock=clock)
    session = store.create()
    for _ in range(4):
        clock.now += 60
        store.get(session.token)
    assert store.get(session.token) is None


def test_destroying_a_session_makes_its_token_useless():
    store = SessionStore()
    session = store.create()
    store.destroy(session.token)
    assert store.get(session.token) is None
    assert store.count() == 0


def test_five_failures_lock_a_client_out_and_the_window_expires():
    clock = _Clock()
    throttle = LoginThrottle(max_failures=5, window_sec=300, clock=clock)
    for _ in range(5):
        throttle.check("192.168.2.9")
        throttle.record_failure("192.168.2.9")
    with pytest.raises(TooManyAttempts):
        throttle.check("192.168.2.9")
    clock.now += 301
    throttle.check("192.168.2.9")


def test_a_lockout_is_per_client():
    throttle = LoginThrottle(max_failures=1)
    throttle.record_failure("192.168.2.9")
    with pytest.raises(TooManyAttempts):
        throttle.check("192.168.2.9")
    throttle.check("192.168.2.10")


def test_a_success_clears_the_failure_count():
    throttle = LoginThrottle(max_failures=2)
    throttle.record_failure("192.168.2.9")
    throttle.record_success("192.168.2.9")
    throttle.record_failure("192.168.2.9")
    throttle.check("192.168.2.9")


def test_binding_beyond_loopback_without_a_password_is_refused():
    """The failure this exists to prevent is silent: an app that starts, works, and is open."""
    with pytest.raises(UnsafeBind, match="set_password"):
        check_bind_allowed("0.0.0.0", has_password=False)
    with pytest.raises(UnsafeBind):
        check_bind_allowed("192.168.2.18", has_password=False)


def test_loopback_without_a_password_is_allowed_and_any_bind_with_one_is():
    check_bind_allowed("127.0.0.1", has_password=False)
    check_bind_allowed("localhost", has_password=False)
    check_bind_allowed("::1", has_password=False)
    check_bind_allowed("0.0.0.0", has_password=True)
