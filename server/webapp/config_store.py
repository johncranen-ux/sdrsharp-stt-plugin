"""config.json -- values only, defaults merged on read, validated and atomic on write.

Values only, because the descriptions and types live in settings_schema.py and would
otherwise drift into two places. Atomic, because an interrupted save that truncated this file
would take the API keys with it.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from webapp.settings_schema import BY_KEY, SETTINGS, SettingType, validate_value

MASK = "●●●●"


class UnknownSetting(ValueError):
    """A key that is not in the catalogue. The catalogue is the whole surface."""


def load(path: Path) -> dict[str, str]:
    """Every catalogue key, stored value where there is one, default otherwise.

    Complete by construction so a caller building an environment never has to ask whether a
    key exists. Unknown keys already in the file are ignored rather than raising: a config
    written by a newer version must not stop an older one from starting.
    """
    stored: dict[str, str] = {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            stored = {k: str(v) for k, v in raw.items()}
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        pass
    return {s.key: stored.get(s.key, s.default) for s in SETTINGS}


def save(path: Path, values: dict[str, str]) -> None:
    """Validate everything, then write atomically. Refuses unknown keys."""
    unknown = sorted(set(values) - set(BY_KEY))
    if unknown:
        raise UnknownSetting(f"not settings: {', '.join(unknown)}")

    # Validate the whole batch BEFORE touching the file, so a bad value cannot leave a
    # half-applied config behind.
    clean = {key: validate_value(BY_KEY[key], value) for key, value in values.items()}

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def redacted_values(values: dict[str, str]) -> dict[str, str]:
    """Values safe to send to a browser: secrets masked, everything else verbatim.

    An UNSET secret stays empty rather than masked -- showing dots for a key that was never
    configured would tell the operator it is set when it is not.
    """
    out = {}
    for key, value in values.items():
        spec = BY_KEY.get(key)
        if spec and spec.type is SettingType.SECRET and value:
            out[key] = MASK
        else:
            out[key] = value
    return out
