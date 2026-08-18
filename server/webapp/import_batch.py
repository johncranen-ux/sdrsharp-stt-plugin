"""One-time migration: read the values currently set in start-all.bat into config.json.

The batch file is INPUT ONLY and is never regenerated. Its prose comments are the source of
the catalogue's descriptions and are some of the best documentation in the project; a
round-trip through this module would destroy them. After the migration it stays on disk as a
read-only fallback.

This module is expected to be deleted once every deployment has run it once.
"""
from __future__ import annotations

import re
from pathlib import Path

from webapp import config_store
from webapp.settings_schema import BY_KEY, validate_value

# Active `set NAME=value` only, anchored at the start of the line (case-insensitive, as cmd.exe
# is). A commented line (`:: set X=off`) documents a rollback that is NOT currently applied, and
# importing it would silently turn a shipped fix off during the migration.
_SET_RE = re.compile(r"^set\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$", re.IGNORECASE)


def parse_batch(text: str) -> dict[str, str]:
    """Every catalogue setting the batch file actively sets, in file order."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _SET_RE.match(line.strip())
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        # Keys outside the catalogue are batch plumbing (SCRIPT_DIR, PROXY_SCRIPT).
        if key in BY_KEY:
            found[key] = value
    return found


def import_into(batch_path: Path, config_path: Path) -> dict[str, str]:
    """Write a config.json from the batch file's current values plus catalogue defaults.

    Validates as it goes, so a value the schema rejects fails here -- during a migration a
    human is watching -- rather than at the next restart.
    """
    text = Path(batch_path).read_text(encoding="utf-8-sig")
    imported = parse_batch(text)

    values = {spec.key: spec.default for spec in BY_KEY.values()}
    for key, raw in imported.items():
        values[key] = validate_value(BY_KEY[key], raw)

    config_store.save(config_path, values)
    return values
