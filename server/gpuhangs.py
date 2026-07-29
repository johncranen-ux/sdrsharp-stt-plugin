"""Count AMD GPU hangs from Windows' LiveKernelEvent 141 crash reports.

This is the objective detector for the GPU fault behind the recurring transcription
failures. Everything else tried previously is unreliable:

- The AMD driver-timeout popup undercounts badly — while one dialog is open, further
  hangs produce no new dialog, so a burst reads as a single event.
- A failed request only happens when a hang stalls an inference past the proxy watchdog's
  threshold. Hangs that the driver recovers from quickly are completely invisible to the
  client, so request failures measure a *subset* of hangs.
- `C:\\Windows\\LiveKernelReports` cannot be listed without elevation, and returns an empty
  result rather than an error when unprivileged — reading that as "no dumps exist" is a
  false negative that misled this investigation once already.

The reliable route is the Windows Error Reporting event each dump raises, which is readable
unprivileged. The WER event's own timestamp is when the report was *uploaded* (they arrive in
bulk, long after the fact); the true hang time is encoded in the dump filename, so that is
what gets parsed here.

Usage:
    py gpuhangs.py                        # last 7 days
    py gpuhangs.py --since 2026-07-27
    py gpuhangs.py --since "2026-07-29 17:59" --until "2026-07-29 18:39"
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess

DUMP_RE = re.compile(r"WATCHDOG-(\d{8})-(\d{4})\.dmp")

# Reads the WER records for LiveKernelEvent 141 (the GPU-hang live-dump class) and emits
# just the dump filenames; parsing the embedded timestamps is done in Python.
_PS_QUERY = r"""
Get-WinEvent -FilterHashtable @{LogName='Application'; ProviderName='Windows Error Reporting'; Id=1001; StartTime=(Get-Date '%s')} -ErrorAction SilentlyContinue |
  Where-Object { $_.Message -match 'LiveKernelEvent' -and $_.Message -match 'P1: 141' } |
  ForEach-Object { if ($_.Message -match 'WATCHDOG-\d{8}-\d{4}\.dmp') { $Matches[0] } }
"""


def find_hangs(since: datetime.datetime,
               until: datetime.datetime | None = None) -> list[datetime.datetime]:
    """Unique GPU hang times in the window, oldest first.

    `since` is applied twice with different meanings: once to bound the event-log query (by
    upload time) and again to filter the parsed hang times. Reports can be uploaded days
    after the hang, so the query window is widened generously to avoid missing old dumps.
    """
    query_from = (since - datetime.timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", _PS_QUERY % query_from],
        capture_output=True, text=True, timeout=300,
    )
    seen: set[datetime.datetime] = set()
    for match in DUMP_RE.finditer(proc.stdout):
        stamp = datetime.datetime.strptime(match.group(1) + match.group(2), "%Y%m%d%H%M")
        if stamp >= since and (until is None or stamp <= until):
            seen.add(stamp)
    return sorted(seen)


def summarize(hangs: list[datetime.datetime], window_hours: float | None = None) -> str:
    if not hangs:
        return "no GPU hangs recorded in this window"
    lines = []
    by_day: dict[str, list[datetime.datetime]] = {}
    for h in hangs:
        by_day.setdefault(h.strftime("%Y-%m-%d"), []).append(h)
    for day, items in sorted(by_day.items()):
        times = "  ".join(t.strftime("%H:%M") for t in items)
        lines.append(f"{day}: {len(items):>3} hangs")
        lines.append(f"     {times}")
    lines.append("")
    lines.append(f"total: {len(hangs)} hangs")
    if window_hours:
        lines.append(f"rate : {len(hangs) / window_hours:.2f} hangs/hour "
                     f"over {window_hours:.2f}h")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", help="e.g. 2026-07-27 or '2026-07-29 17:59'")
    parser.add_argument("--until", help="e.g. '2026-07-29 18:39'")
    args = parser.parse_args()

    def parse(value: str) -> datetime.datetime:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return datetime.datetime.strptime(value, fmt)
            except ValueError:
                continue
        raise SystemExit(f"unparseable date: {value}")

    since = parse(args.since) if args.since else (
        datetime.datetime.now() - datetime.timedelta(days=7))
    until = parse(args.until) if args.until else None

    hangs = find_hangs(since, until)
    span = ((until or datetime.datetime.now()) - since).total_seconds() / 3600
    print(summarize(hangs, span if span > 0 else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
