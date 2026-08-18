"""Stored settings -> the environment a managed child process is started with."""
from __future__ import annotations

import os

from webapp.settings_schema import BY_KEY


def build_env(values: dict[str, str], base: dict[str, str] | None = None) -> dict[str, str]:
    """The parent environment, with every configured setting applied over it.

    An empty value REMOVES the variable rather than exporting an empty string. The two are
    not equivalent: `ANTHROPIC_API_KEY=""` looks present and fails later with a confusing
    error, where unset is documented to disable identification cleanly. Removing also means
    clearing a value in the UI takes effect even when the launching shell had one set.
    """
    env = dict(os.environ if base is None else base)
    for key, value in values.items():
        if key not in BY_KEY:
            continue
        stripped = (value or "").strip()
        if stripped:
            env[key] = stripped
        else:
            env.pop(key, None)
    return env
