"""Which process holds a TCP port, and whether we are allowed to take it.

start-all.bat kills whatever holds :9000 unconditionally. A panel reachable from a phone must
not: the operator cannot see what would die. So a holder is killed only when its image name is
one the registry entry could have started, and anything else is reported and refused.

psutil rather than netstat + tasklist: tasklist has been observed in this project reporting
processes that no longer exist, and psutil answers pid, image name and start time from one API.
"""
from __future__ import annotations

import time

import psutil


class PortHeldByStranger(Exception):
    """Something we did not start is listening. Report it; never kill it."""

    def __init__(self, port: int, pid: int, image: str):
        super().__init__(f"port {port} is held by pid {pid} ({image}), which is not one of ours")
        self.port = port
        self.pid = pid
        self.image = image


def pid_listening_on(port: int) -> int | None:
    """The pid of the process listening on `port`, or None.

    psutil returns connections whose pid is None for processes this account cannot open; those
    are skipped rather than reported, because a pid we cannot see is one we cannot act on.
    """
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.AccessDenied, OSError):
        return None
    for conn in connections:
        if conn.status != psutil.CONN_LISTEN or not conn.laddr:
            continue
        if conn.laddr.port == port and conn.pid:
            return conn.pid
    return None


def image_name(pid: int) -> str | None:
    try:
        return psutil.Process(pid).name()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return None


def clear_port(port: int, expected_images: set[str], timeout_sec: float = 5.0) -> bool:
    """Free `port` if one of ours holds it. Returns whether anything was killed.

    Raises PortHeldByStranger when the holder's image is not in `expected_images`.
    """
    pid = pid_listening_on(port)
    if pid is None:
        return False
    name = image_name(pid) or "unknown"
    if name.lower() not in {image.lower() for image in expected_images}:
        raise PortHeldByStranger(port, pid, name)

    try:
        holder = psutil.Process(pid)
        holder.terminate()
        holder.wait(timeout=timeout_sec)
    except psutil.NoSuchProcess:
        pass
    except psutil.TimeoutExpired:
        holder.kill()
        holder.wait(timeout=timeout_sec)

    # The socket can outlive the process briefly. Waiting here means a start that follows
    # cannot lose the race and bind alongside a dying zombie -- the exact failure this exists
    # to prevent.
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        if pid_listening_on(port) is None:
            return True
        time.sleep(0.1)
    return True
