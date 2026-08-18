"""Port clearing. Without it "Restart" is a quiet no-op: Python's ThreadingHTTPServer sets
SO_REUSEADDR, so a second proxy binds alongside the first, takes the port, and leaves the
original running as a zombie."""
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.ports import PortHeldByStranger, clear_port, image_name, pid_listening_on  # noqa: E402

_LISTENER = (
    "import socket, time; "
    "s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); "
    "s.bind(('127.0.0.1', {port})); s.listen(); time.sleep(120)"
)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_until_listening(port: int, timeout: float = 15.0) -> int:
    deadline = time.time() + timeout
    while time.time() < deadline:
        pid = pid_listening_on(port)
        if pid:
            return pid
        time.sleep(0.1)
    raise AssertionError(f"nothing ever listened on {port}")


def test_a_listening_socket_in_this_process_is_found():
    port = _free_port()
    with socket.socket() as s:
        s.bind(("127.0.0.1", port))
        s.listen()
        assert pid_listening_on(port) == os.getpid()


def test_nothing_listening_is_reported_as_nothing():
    assert pid_listening_on(_free_port()) is None


def test_the_image_name_of_this_process_is_a_python():
    assert "python" in (image_name(os.getpid()) or "").lower()


def test_the_image_name_of_a_pid_that_is_gone_is_none():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    assert image_name(child.pid) in (None, "")


def test_clear_port_kills_a_listener_it_recognises():
    port = _free_port()
    child = subprocess.Popen([sys.executable, "-c", _LISTENER.format(port=port)])
    try:
        _wait_until_listening(port)
        assert clear_port(port, {Path(sys.executable).name.lower()}) is True
        assert pid_listening_on(port) is None
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=15)


def test_clear_port_refuses_to_kill_something_it_did_not_start():
    """The operator may be on a phone and cannot see what would die."""
    port = _free_port()
    child = subprocess.Popen([sys.executable, "-c", _LISTENER.format(port=port)])
    try:
        pid = _wait_until_listening(port)
        with pytest.raises(PortHeldByStranger) as caught:
            clear_port(port, {"sdrsharp.exe"})
        assert caught.value.pid == pid
        assert "python" in caught.value.image.lower()
        assert pid_listening_on(port) == pid   # still alive
    finally:
        child.kill()
        child.wait(timeout=15)


def test_clearing_a_free_port_is_a_no_op():
    assert clear_port(_free_port(), {"python.exe"}) is False
