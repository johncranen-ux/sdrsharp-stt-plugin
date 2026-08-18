"""Sessions, CSRF tokens, login rate limiting, and the bind-address guard.

In memory on purpose: there is one operator, and a panel restart logging them out is the right
amount of ceremony for something that can start and stop processes.
"""
from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass

COOKIE_NAME = "cp_session"
CSRF_HEADER = "X-CSRF-Token"

_LOOPBACK = {"127.0.0.1", "::1", "localhost", ""}


class TooManyAttempts(Exception):
    """Login refused for now. A LAN-reachable password prompt is not a free oracle."""


class UnsafeBind(Exception):
    """Refusing to listen beyond loopback with no password configured."""


@dataclass
class Session:
    token: str
    csrf: str
    created_at: float
    last_seen_at: float


class SessionStore:
    def __init__(self, ttl_sec: float = 43_200, idle_sec: float = 7_200,
                 clock: Callable[[], float] = time.time):
        self._ttl = ttl_sec
        self._idle = idle_sec
        self._clock = clock
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        now = self._clock()
        session = Session(token=secrets.token_urlsafe(32), csrf=secrets.token_urlsafe(32),
                          created_at=now, last_seen_at=now)
        self._sessions[session.token] = session
        return session

    def get(self, token: str | None) -> Session | None:
        """The session, refreshed; None if unknown, idle too long, or past its absolute TTL."""
        if not token:
            return None
        session = self._sessions.get(token)
        if session is None:
            return None
        now = self._clock()
        if now - session.last_seen_at > self._idle or now - session.created_at > self._ttl:
            self._sessions.pop(token, None)
            return None
        session.last_seen_at = now
        return session

    def destroy(self, token: str) -> None:
        self._sessions.pop(token, None)

    def count(self) -> int:
        return len(self._sessions)


@dataclass
class _Attempts:
    failures: int = 0
    first_at: float = 0.0


class LoginThrottle:
    def __init__(self, max_failures: int = 5, window_sec: float = 300,
                 clock: Callable[[], float] = time.time):
        self._max = max_failures
        self._window = window_sec
        self._clock = clock
        self._by_client: dict[str, _Attempts] = {}

    def check(self, client: str) -> None:
        record = self._by_client.get(client)
        if record is None:
            return
        elapsed = self._clock() - record.first_at
        if elapsed > self._window:
            self._by_client.pop(client, None)
            return
        if record.failures >= self._max:
            raise TooManyAttempts(
                f"too many failed attempts; try again in {int(self._window - elapsed)}s")

    def record_failure(self, client: str) -> None:
        now = self._clock()
        record = self._by_client.get(client)
        if record is None or now - record.first_at > self._window:
            self._by_client[client] = _Attempts(failures=1, first_at=now)
            return
        record.failures += 1

    def record_success(self, client: str) -> None:
        self._by_client.pop(client, None)


def check_bind_allowed(host: str, has_password: bool) -> None:
    """Raise unless it is safe to listen on `host`.

    The failure this prevents is silent: an app bound to 0.0.0.0 with no password starts
    cleanly, works perfectly, and hands anyone on the network six API keys and the ability to
    start processes. Refusing to start is the only signal that arrives in time.
    """
    if has_password or (host or "").strip().lower() in _LOOPBACK:
        return
    raise UnsafeBind(
        f"WEBAPP_BIND_HOST is {host!r}, which is reachable from the network, and no password "
        f"is set. Run `py -m webapp.set_password` from the server directory, or set "
        f"WEBAPP_BIND_HOST back to 127.0.0.1.")
