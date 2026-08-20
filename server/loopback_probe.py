"""Does this machine's loopback TCP lose the tail of a bulk transfer?

Reproduces the fault root-caused on 2026-08-19: a bulk transfer over 127.0.0.1 intermittently
loses a ~33 KB run of segments near the end of the stream. The sender retransmits, never gets
an ACK, gives up at Windows' data-retransmission limit and sends RST; the receiver sees
ConnectionResetError [WinError 10054] having stopped short.

The give-up point is arithmetic, not contention. With MinRto 300ms and
TcpMaxDataRetransmissions at its default of 5, it is 300ms x (2^6 - 1) = 18,900 ms. Every
failure measured so far landed 18.93-19.00s and every success 0.03-0.05s -- bimodal, nothing
in between. A constant duration is the signature of a fixed timeout; contention scatters. So
this probe reports the two clusters and says whether the failures are tight enough to BE that
signature, rather than only counting them.

Deliberately plain TCP between two threads of one process: no HTTP, no framework, nothing
imported from the proxy. The fault reproduces at that level, which is what puts it below the
application. Anything this probe still fails on cannot be blamed on our code.

Primary use is a before/after around a change to the machine -- toggling Norton's network
inspection is the open hypothesis:

    py loopback_probe.py --label before --out before.json
    (make the change)
    py loopback_probe.py --label after  --out after.json
    py loopback_probe.py --compare before.json after.json

Nothing here touches the running proxy, the panel or any real data. It binds an ephemeral
port on 127.0.0.1 and talks only to itself.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import statistics
import sys
import threading
import time
from dataclasses import asdict, dataclass, field

DEFAULT_SIZE = 1_800_000   # the real /api/ais-cache body: 6046 vessels, ~1.8 MB
DEFAULT_RUNS = 24          # the earlier measurement's sample size, which failed 6/24 here
DEFAULT_TIMEOUT = 60.0     # must exceed 18.9s or the probe truncates the very thing it measures

# A failure cluster no wider than this reads as one fixed timeout rather than as contention.
FIXED_TIMEOUT_SPREAD_SEC = 1.0


def _refuse_if_in_use(port: int) -> None:
    """Fail before binding if anything already answers on this port.

    Belt and braces with SO_EXCLUSIVEADDRUSE below, and the one that gives a readable
    message. A probe that shares a port with the proxy would be sitting in front of the
    plugin's audio path, so the cost of a false negative here is lost radio traffic.
    """
    checker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    checker.settimeout(0.5)
    try:
        checker.connect(("127.0.0.1", port))
    except OSError:
        return          # nothing answered, which is what we want
    finally:
        checker.close()
    raise OSError(f"something is already listening on 127.0.0.1:{port}")


@dataclass
class Trial:
    index: int
    elapsed: float
    received: int
    expected: int
    outcome: str            # ok | reset | short | timeout | error
    detail: str = ""
    sender_error: str = ""

    @property
    def failed(self) -> bool:
        return self.outcome != "ok"

    @property
    def shortfall(self) -> int:
        return self.expected - self.received


def build_payload(size: int, random_bytes: bool = False) -> bytes:
    """Bytes to push down the socket.

    ASCII by default rather than os.urandom, because the transfer that fails in production is
    a JSON body, and an inline inspection driver is entitled to treat text and entropy
    differently. --random exists to test exactly that.
    """
    if random_bytes:
        return os.urandom(size)
    unit = b'{"mmsi":"000000000","name":"EXAMPLE VESSEL","callsign":"XXXX"},'
    return (unit * (size // len(unit) + 1))[:size]


def _sender(listener: socket.socket, payload: bytes, errors: dict, runs: int,
            stop: threading.Event) -> None:
    """Accept each trial's connection and write the whole payload.

    sendall is left blocking on purpose. The stack's own retransmit-and-give-up is the thing
    under measurement, so imposing a send timeout here would hide it.
    """
    for index in range(runs):
        if stop.is_set():
            return
        try:
            conn, _ = listener.accept()
        except OSError:
            return
        try:
            conn.sendall(payload)
        except OSError as exc:
            errors[index] = f"{type(exc).__name__}: {exc}"
        finally:
            conn.close()


def run_trial(index: int, port: int, payload: bytes, timeout: float,
              sender_errors: dict) -> Trial:
    """One connect-receive-close cycle, timed from connect to the end of the read loop."""
    received = 0
    outcome = "ok"
    detail = ""
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    client.settimeout(timeout)
    started = time.monotonic()
    try:
        client.connect(("127.0.0.1", port))
        while True:
            block = client.recv(65536)
            if not block:
                break
            received += len(block)
    except ConnectionResetError as exc:
        outcome, detail = "reset", f"{type(exc).__name__}: {exc}"
    except socket.timeout:
        outcome, detail = "timeout", f"no data for {timeout:.0f}s"
    except OSError as exc:
        outcome, detail = "error", f"{type(exc).__name__}: {exc}"
    finally:
        elapsed = time.monotonic() - started
        client.close()

    if outcome == "ok" and received != len(payload):
        # A clean EOF that still arrived short: the tail was lost without a reset reaching us.
        outcome, detail = "short", f"clean EOF {len(payload) - received} bytes short"

    return Trial(index=index, elapsed=elapsed, received=received, expected=len(payload),
                 outcome=outcome, detail=detail, sender_error=sender_errors.get(index, ""))


def probe(size: int = DEFAULT_SIZE, runs: int = DEFAULT_RUNS,
          timeout: float = DEFAULT_TIMEOUT, random_bytes: bool = False,
          port: int = 0, progress=None) -> list[Trial]:
    """Run `runs` transfers and return one Trial each.

    port=0 takes an ephemeral one. Pass the real service port instead when testing a firewall
    or IDS hypothesis: those attach rules to a LISTENING SERVICE, so an ephemeral-to-ephemeral
    transfer can bypass the very inspection under suspicion. Measured 2026-08-20: ephemeral
    ran 0/100 failures at 0.00s while the original fault, on port 9000, ran 0.03-0.05s when it
    succeeded at all. Requires the real service to be stopped.
    """
    payload = build_payload(size, random_bytes)
    if port:
        _refuse_if_in_use(port)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    # No SO_REUSEADDR, and SO_EXCLUSIVEADDRUSE on top of it. Absence of SO_REUSEADDR is NOT
    # enough on Windows: the live proxy listens on 0.0.0.0:9000, and a wildcard bind does not
    # exclude a later 127.0.0.1 bind on the same port -- the more specific bind just wins for
    # incoming connections. On 2026-08-20 that let this probe silently take over the port the
    # SDR# plugin posts audio to while the proxy was carrying live traffic. SO_EXCLUSIVEADDRUSE
    # makes bind fail on ANY conflicting binding, which is the behaviour this needs.
    if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    port = listener.getsockname()[1]

    sender_errors: dict[int, str] = {}
    stop = threading.Event()
    thread = threading.Thread(target=_sender, args=(listener, payload, sender_errors, runs, stop),
                              daemon=True)
    thread.start()

    trials: list[Trial] = []
    try:
        for index in range(runs):
            trial = run_trial(index, port, payload, timeout, sender_errors)
            trials.append(trial)
            if progress:
                progress(trial)
    finally:
        stop.set()
        listener.close()
        thread.join(timeout=2.0)
    return trials


@dataclass
class Summary:
    label: str
    size: int
    runs: int
    failures: int
    failure_rate: float
    ok_elapsed: dict = field(default_factory=dict)
    fail_elapsed: dict = field(default_factory=dict)
    shortfall: dict = field(default_factory=dict)
    outcomes: dict = field(default_factory=dict)
    verdict: str = ""


def _spread(values: list[float]) -> dict:
    if not values:
        return {}
    return {"n": len(values), "min": min(values), "median": statistics.median(values),
            "max": max(values)}


def summarise(trials: list[Trial], label: str = "") -> Summary:
    """Turn trials into the two clusters and a verdict. Pure -- this is what the tests cover."""
    failures = [t for t in trials if t.failed]
    ok_times = [t.elapsed for t in trials if not t.failed]
    fail_times = [t.elapsed for t in failures]
    shortfalls = [float(t.shortfall) for t in failures if t.shortfall > 0]

    outcomes: dict[str, int] = {}
    for trial in trials:
        outcomes[trial.outcome] = outcomes.get(trial.outcome, 0) + 1

    if not trials:
        verdict = "NO DATA"
    elif not failures:
        verdict = "CLEAN -- every transfer completed"
    elif len(fail_times) == 1:
        verdict = ("FAULT PRESENT -- one failure, too few to read a signature from; "
                   "raise --runs before drawing a conclusion")
    elif max(fail_times) - min(fail_times) <= FIXED_TIMEOUT_SPREAD_SEC:
        # The point of the whole probe: a tight band cannot be load or contention.
        verdict = (f"FAULT REPRODUCED -- failures cluster at "
                   f"{statistics.median(fail_times):.2f}s within "
                   f"{max(fail_times) - min(fail_times):.2f}s, the fixed-timeout signature")
    else:
        verdict = ("FAILURES, BUT SCATTERED -- spread over "
                   f"{max(fail_times) - min(fail_times):.2f}s, which is not the fixed-timeout "
                   "signature; this may be a different fault")

    return Summary(label=label, size=trials[0].expected if trials else 0, runs=len(trials),
                   failures=len(failures),
                   failure_rate=len(failures) / len(trials) if trials else 0.0,
                   ok_elapsed=_spread(ok_times), fail_elapsed=_spread(fail_times),
                   shortfall=_spread(shortfalls), outcomes=outcomes, verdict=verdict)


def format_summary(summary: Summary) -> str:
    lines = []
    head = f"{summary.failures}/{summary.runs} transfers failed"
    if summary.label:
        head = f"[{summary.label}] {head}"
    lines.append(f"{head}  ({summary.failure_rate:.0%}, {summary.size:,} bytes each)")
    if summary.ok_elapsed:
        s = summary.ok_elapsed
        lines.append(f"  succeeded ({s['n']:>2}): {s['min']:.2f}s - {s['max']:.2f}s"
                     f"   median {s['median']:.2f}s")
    if summary.fail_elapsed:
        s = summary.fail_elapsed
        lines.append(f"  failed    ({s['n']:>2}): {s['min']:.2f}s - {s['max']:.2f}s"
                     f"   median {s['median']:.2f}s")
    if summary.shortfall:
        s = summary.shortfall
        lines.append(f"  bytes lost:   {int(s['min']):,} - {int(s['max']):,}"
                     f"   median {int(s['median']):,}")
    if summary.outcomes:
        lines.append("  outcomes:     "
                     + ", ".join(f"{k} {v}" for k, v in sorted(summary.outcomes.items())))
    lines.append(f"  VERDICT: {summary.verdict}")
    return "\n".join(lines)


def compare(before: Summary, after: Summary) -> str:
    """Read a before/after pair as one result, since that is the actual question being asked."""
    lines = [format_summary(before), "", format_summary(after), ""]
    if before.failures == 0:
        lines.append("INCONCLUSIVE: the BEFORE run was already clean, so there was no fault "
                     "for the change to fix. Re-run the baseline -- the failure rate is "
                     "intermittent, and a clean 24 does not mean a healthy machine.")
    elif after.failures == 0:
        lines.append(f"CHANGE HELPED: {before.failures}/{before.runs} failing -> "
                     f"{after.failures}/{after.runs}. The change is implicated. Confirm by "
                     f"reverting it and seeing the failures return -- one direction is not proof.")
    elif after.failure_rate < before.failure_rate / 2:
        lines.append(f"PARTIAL: {before.failure_rate:.0%} -> {after.failure_rate:.0%}. Lower, "
                     f"but not gone. An intermittent fault can drift this much on its own; "
                     f"raise --runs before believing it.")
    else:
        lines.append(f"NO EFFECT: {before.failure_rate:.0%} -> {after.failure_rate:.0%}. The "
                     f"change is not the cause. Rule it out and put it back.")
    return "\n".join(lines)


def _load(path: str) -> Summary:
    with open(path, encoding="utf-8") as handle:
        return Summary(**json.load(handle)["summary"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--size", type=int, default=DEFAULT_SIZE,
                        help=f"payload bytes per transfer (default {DEFAULT_SIZE:,})")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS,
                        help=f"number of transfers (default {DEFAULT_RUNS})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help="receive timeout; must stay above 18.9s to see the give-up")
    parser.add_argument("--random", action="store_true",
                        help="send random bytes instead of JSON-like ASCII")
    parser.add_argument("--port", type=int, default=0,
                        help="listen on this port instead of an ephemeral one; use the real "
                             "service port (9000) to test a firewall/IDS hypothesis, with "
                             "that service stopped")
    parser.add_argument("--label", default="", help="name this run, e.g. before / after")
    parser.add_argument("--out", help="write trials and summary to this JSON file")
    parser.add_argument("--compare", nargs=2, metavar=("BEFORE", "AFTER"),
                        help="read two --out files and report whether the change helped")
    args = parser.parse_args(argv)

    if args.compare:
        print(compare(_load(args.compare[0]), _load(args.compare[1])))
        return 0

    if args.timeout <= 19.0:
        print(f"--timeout {args.timeout} is at or below the 18.9s give-up point; a failure "
              f"would be cut short and misreported as a timeout.", file=sys.stderr)
        return 2

    label = f" [{args.label}]" if args.label else ""
    where = f"127.0.0.1:{args.port}" if args.port else "127.0.0.1 (ephemeral port)"
    print(f"{args.runs} transfers of {args.size:,} bytes over {where}{label}", flush=True)

    def progress(trial: Trial) -> None:
        mark = "ok" if not trial.failed else trial.outcome.upper()
        note = "" if not trial.failed else f"  {trial.shortfall:,} bytes short  {trial.detail}"
        print(f"  {trial.index + 1:>3}/{args.runs}  {trial.elapsed:>6.2f}s  {mark}{note}",
              flush=True)

    try:
        trials = probe(size=args.size, runs=args.runs, timeout=args.timeout,
                       random_bytes=args.random, port=args.port, progress=progress)
    except OSError as exc:
        print(f"could not listen on port {args.port}: {exc}\nStop the service using it first "
              f"-- this probe must not share a port with a live one.", file=sys.stderr)
        return 3
    summary = summarise(trials, args.label)
    print()
    print(format_summary(summary))

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            json.dump({"summary": asdict(summary), "trials": [asdict(t) for t in trials]},
                      handle, indent=2)
        print(f"\nwrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
