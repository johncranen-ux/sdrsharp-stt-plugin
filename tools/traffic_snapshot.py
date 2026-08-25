"""Accumulate GitHub traffic statistics that GitHub itself throws away.

The traffic API only ever returns the last 14 days, and what falls out of that window is
deleted permanently -- there is no archive and no way to ask for it later. So the launch week
of a public repository is recoverable only if something wrote it down at the time.

This writes it down. Run it daily (Task Scheduler / cron); each run merges the 14-day window
into a cumulative history file, so history grows without bound and a missed day costs nothing
as long as the gap stays under 14 days.

Usage:
    py tools/traffic_snapshot.py                 # fetch and merge
    py tools/traffic_snapshot.py --report        # merge, then print a summary
    py tools/traffic_snapshot.py --report-only   # print from the stored file, no network

Authentication is delegated to the `gh` CLI, which must be installed and logged in with an
account having push access -- the traffic endpoints refuse anything less. Delegating means no
token is read, stored or passed by this script.
"""
from __future__ import annotations

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

DEFAULT_REPO = "johncranen-ux/sdrsharp-stt-plugin"
DEFAULT_OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                           "traffic", "history.json")

# Per-day series. The API reports these as a list of dated buckets.
DAY_SERIES = ("views", "clones")
# Snapshots: not per-day, so they are stored against the moment they were taken.
SNAPSHOT_SERIES = ("referrers", "paths", "releases", "totals")


def _empty() -> dict:
    return {"repo": None, "first_snapshot_at": None, "last_snapshot_at": None,
            **{k: {} for k in DAY_SERIES}, **{k: {} for k in SNAPSHOT_SERIES}}


def gh_api(path: str) -> object:
    """One `gh api` call, returning parsed JSON."""
    if not shutil.which("gh"):
        raise SystemExit("gh CLI not found. Install it and run `gh auth login`.")
    proc = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        if "403" in err or "Resource not accessible" in err:
            raise SystemExit(
                f"gh api {path} was refused (403). The traffic endpoints require PUSH access "
                f"to the repository; a read-only token is not enough.")
        raise SystemExit(f"gh api {path} failed: {err}")
    return json.loads(proc.stdout)


def merge_days(stored: dict, fetched: list, key: str) -> tuple[int, int]:
    """Merge one dated series into `stored`, in place. Returns (added, updated).

    Two rules, and both matter:

    A date present in the fetch OVERWRITES the stored value rather than being skipped. The
    current day is always partial -- a snapshot at 09:00 sees fewer views than the same day
    seen tomorrow -- so the later read of a given date is the better one.

    A date absent from the fetch is NEVER removed. Absence means "outside the 14-day window",
    which is precisely the data this script exists to keep. Treating the fetch as authoritative
    would delete the entire history on every run, which is the one bug that would make the
    whole thing pointless while still appearing to work.
    """
    added = updated = 0
    for bucket in fetched:
        day = bucket["timestamp"][:10]
        value = {"count": bucket.get("count", 0), "uniques": bucket.get("uniques", 0)}
        if day not in stored:
            added += 1
        elif stored[day] != value:
            updated += 1
        else:
            continue
        stored[day] = value
    return added, updated


def load(path: str) -> dict:
    if not os.path.exists(path):
        return _empty()
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        raise SystemExit(f"{path} exists but could not be read ({exc}). Refusing to overwrite "
                         f"it -- move it aside if you meant to start over.")
    base = _empty()
    base.update({k: v for k, v in data.items() if k in base})
    for k in DAY_SERIES + SNAPSHOT_SERIES:
        if not isinstance(base.get(k), dict):
            base[k] = {}
    return base


def save(path: str, data: dict) -> None:
    """Write atomically: a half-written history is worse than a stale one."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, path)


def collect(repo: str, stored: dict, now: str) -> list[str]:
    """Fetch everything and merge it into `stored`. Returns lines describing what changed."""
    lines = []
    stored["repo"] = repo

    for series, endpoint in (("views", "views"), ("clones", "clones")):
        payload = gh_api(f"repos/{repo}/traffic/{endpoint}")
        added, updated = merge_days(stored[series], payload.get(endpoint, []), series)
        lines.append(f"{series:<9} window total={payload.get('count', 0):<6} "
                     f"unique={payload.get('uniques', 0):<6} "
                     f"(+{added} new days, {updated} revised)")

    refs = gh_api(f"repos/{repo}/traffic/popular/referrers")
    stored["referrers"][now] = [
        {"referrer": r["referrer"], "count": r["count"], "uniques": r["uniques"]} for r in refs]

    paths = gh_api(f"repos/{repo}/traffic/popular/paths")
    stored["paths"][now] = [
        {"path": p["path"], "count": p["count"], "uniques": p["uniques"]} for p in paths]

    downloads = {}
    for rel in gh_api(f"repos/{repo}/releases"):
        for asset in rel.get("assets", []):
            downloads[f"{rel['tag_name']}/{asset['name']}"] = asset["download_count"]
    stored["releases"][now] = downloads

    meta = gh_api(f"repos/{repo}")
    stored["totals"][now] = {"stars": meta.get("stargazers_count", 0),
                             "forks": meta.get("forks_count", 0),
                             "watchers": meta.get("subscribers_count", 0),
                             "open_issues": meta.get("open_issues_count", 0)}

    lines.append(f"referrers {len(refs)}, popular paths {len(paths)}, "
                 f"release assets {len(downloads)}")
    lines.append("stars={stars} forks={forks} watchers={watchers}".format(**stored["totals"][now]))
    return lines


def report(data: dict) -> str:
    out = [f"Traffic history for {data.get('repo') or '(unknown repo)'}",
           f"  recorded {data.get('first_snapshot_at') or '-'} .. "
           f"{data.get('last_snapshot_at') or '-'}"]

    for series in DAY_SERIES:
        days = data.get(series, {})
        if not days:
            out.append(f"\n{series}: nothing recorded yet")
            continue
        total = sum(d["count"] for d in days.values())
        uniq = sum(d["uniques"] for d in days.values())
        active = sum(1 for d in days.values() if d["count"])
        # `uniq` is a sum of PER-DAY unique counts and is deliberately not labelled "unique".
        # GitHub dedupes within a day, so one cloner appearing on four days contributes four:
        # on this repo the window read 52 clones / 1 unique while the daily buckets summed to
        # 4. A true distinct count for a period exists only as the API's own window figure,
        # printed by collect(), and cannot be reconstructed from daily buckets.
        out.append(f"\n{series}: {total} total over {len(days)} recorded days "
                   f"({active} with activity)")
        out.append(f"  daily uniques sum to {uniq} -- NOT a distinct-visitor count; "
                   f"someone returning on 4 days counts 4 times")
        out.append(f"  {'date':<12}{'count':>8}{'unique':>8}")
        for day in sorted(days)[-14:]:
            d = days[day]
            out.append(f"  {day:<12}{d['count']:>8}{d['uniques']:>8}")

    rel = data.get("releases", {})
    if rel:
        latest = rel[max(rel)]
        out.append("\nrelease downloads (all time, as of last snapshot):")
        for name, n in sorted(latest.items()) or [("(no assets)", 0)]:
            out.append(f"  {name:<50}{n:>8}")

    tot = data.get("totals", {})
    if tot:
        latest = tot[max(tot)]
        out.append("\nstars={stars}  forks={forks}  watchers={watchers}  "
                   "open_issues={open_issues}".format(**latest))

    refs = data.get("referrers", {})
    if refs and refs[max(refs)]:
        out.append("\ntop referrers (last 14d, as of last snapshot):")
        for r in refs[max(refs)][:10]:
            out.append(f"  {r['referrer']:<30}{r['count']:>8}{r['uniques']:>8}")

    return "\n".join(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=os.environ.get("TRAFFIC_REPO", DEFAULT_REPO))
    ap.add_argument("--out", default=os.environ.get("TRAFFIC_OUT", DEFAULT_OUT))
    ap.add_argument("--report", action="store_true", help="print a summary after fetching")
    ap.add_argument("--report-only", action="store_true",
                    help="print the stored summary without contacting GitHub")
    args = ap.parse_args(argv)

    data = load(args.out)

    if args.report_only:
        print(report(data))
        return 0

    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    lines = collect(args.repo, data, now)
    data["first_snapshot_at"] = data.get("first_snapshot_at") or now
    data["last_snapshot_at"] = now
    save(args.out, data)

    print(f"[traffic] {args.repo} @ {now}")
    for line in lines:
        print(f"  {line}")
    print(f"  -> {args.out}")

    if args.report:
        print()
        print(report(data))
    return 0


if __name__ == "__main__":
    sys.exit(main())
