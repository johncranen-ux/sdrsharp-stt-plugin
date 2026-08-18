"""A deterministic stand-in for the proxy: prints a banner, optionally binds a port, sleeps.

Real children are not used in supervisor tests -- the proxy talks to Groq and to an AIS feed,
and the counter opens a TCP connection to another machine. This one only does what the
supervisor can observe.
"""
import socket
import sys
import time

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    print("fake child started", flush=True)
    held = None
    if port:
        held = socket.socket()
        held.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        held.bind(("127.0.0.1", port))
        held.listen()
        print(f"listening on {port}", flush=True)
    time.sleep(300)
