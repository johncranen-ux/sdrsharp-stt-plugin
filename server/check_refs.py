#!/usr/bin/env python3
"""Validate a hand-edited reference file before anything is scored against it.

`bench.load_references` skips any line with neither a tab nor a colon, **silently**: no
error, no warning, the clip simply disappears from the run and pooled WER is computed over
fewer clips than the operator believes. An editor set to insert spaces instead of tabs does
exactly that to every line it touches, and the resulting number looks entirely normal.

That is the same failure family as the rest of this harness's history -- a confident-looking
result computed over the wrong data -- so this makes it loud instead.

    py check_refs.py references-iq-2026-08-09.txt D:/SDR/iq-arms-0809/squelch-on

The captures directory is optional; give it and the clip-level checks run too.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(_SERVER_DIR))

import bench  # noqa: E402


def check(refs_path: str | Path, captures: str | Path | None = None) -> dict:
    """Report every way this reference file would quietly mis-score a run.

    `ok` is False only for faults that corrupt the measurement -- dropped lines, or a file
    that is not there. An empty reference is surfaced but not a failure: `0066:` is the
    documented way to mark a static-only clip, and the operator may well mean it.
    """
    refs_path = Path(refs_path)
    report: dict = {
        "path": refs_path, "exists": refs_path.exists(),
        "data_lines": 0, "parsed": 0, "dropped": [], "empty": [],
        "clips": None, "with_reference": None, "missing": [], "extra": [], "ok": False,
    }
    if not refs_path.exists():
        return report

    refs = bench.load_references(refs_path)
    raw = [line for line in refs_path.read_text(encoding="utf-8").splitlines()
           if line.strip() and not line.strip().startswith("#")]

    # Mirror load_references' own test, on the STRIPPED line -- a trailing tab does not
    # survive .strip(), which is exactly how three clips went missing from the real
    # 2026-08-09 draft. The colon form is immune, which is why it is the documented
    # alternative for marking a clip with no speech in it.
    report["dropped"] = [l for l in raw
                         if "\t" not in l.strip() and ":" not in l.strip()]
    report["data_lines"] = len(raw)
    report["parsed"] = len(refs)
    report["empty"] = sorted(cid for cid, text in refs.items() if not text.strip())

    if captures is not None:
        ids = {cid for cid, _ in bench.discover_clips(Path(captures))}
        report["clips"] = len(ids)
        report["with_reference"] = len(ids & set(refs))
        report["missing"] = sorted(ids - set(refs))
        report["extra"] = sorted(set(refs) - ids)

    report["ok"] = not report["dropped"]
    return report


def format_report(report: dict) -> list[str]:
    out = []
    if not report["exists"]:
        return [f"{report['path']}: no such file"]

    out.append(f"{report['path'].name}: {report['data_lines']} data lines "
               f"-> {report['parsed']} parsed")
    if report["dropped"]:
        out.append(f"\n  {len(report['dropped'])} line(s) SILENTLY DROPPED "
                   f"(no tab, no colon):")
        out += [f"    {l[:90]!r}" for l in report["dropped"][:10]]

    if report["clips"] is not None:
        out.append(f"\n  clips: {report['clips']}   "
                   f"with a reference: {report['with_reference']}")
        if report["missing"]:
            shown = ", ".join(report["missing"][:15])
            more = " ..." if len(report["missing"]) > 15 else ""
            out.append(f"  {len(report['missing'])} clip(s) have NO reference and will be "
                       f"excluded: {shown}{more}")
        if report["extra"]:
            out.append(f"  {len(report['extra'])} reference id(s) match no clip: "
                       f"{', '.join(report['extra'][:15])}")

    if report["empty"]:
        out.append(f"\n  {len(report['empty'])} reference(s) are empty: "
                   f"{', '.join(report['empty'][:15])}")
        out.append("  (intended for static-only clips; delete the line if that is not why)")

    out.append("\nOK" if report["ok"] else "\nFIX THE DROPPED LINES BEFORE SCORING")
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print(__doc__)
        return 2
    report = check(argv[0], argv[1] if len(argv) > 1 else None)
    print("\n".join(format_report(report)))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
