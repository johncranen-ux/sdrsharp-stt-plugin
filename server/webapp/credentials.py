"""The operator password: hashed with argon2id, stored in its own file, never in config.json.

A password hash is a credential, not a setting. It is never rendered in a form, never sent to
a browser and never edited as text, so it does not belong in the catalogue that drives both.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from webapp.secure_file import restrict

MIN_LENGTH = 12

_HASHER = PasswordHasher()


def hash_password(password: str) -> str:
    if len(password or "") < MIN_LENGTH:
        raise ValueError(f"password must be at least {MIN_LENGTH} characters")
    return _HASHER.hash(password)


def verify_password(stored: str | None, password: str) -> bool:
    """False for every failure, including a hash this build cannot parse.

    Fails closed and silently by design: the caller turns this into one 401 with no detail, so
    a login page cannot be used to tell a wrong password from a damaged file.
    """
    if not stored or not password:
        return False
    try:
        return _HASHER.verify(stored, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def load_hash(path: Path) -> str | None:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    value = raw.get("password_hash") if isinstance(raw, dict) else None
    return value if isinstance(value, str) and value else None


def has_password(path: Path) -> bool:
    return load_hash(path) is not None


def save_password(path: Path, password: str) -> None:
    """Hash, write atomically, then restrict the file to this account.

    The hash is computed BEFORE the file is touched, so a password that fails the length rule
    leaves no half-written credentials behind.
    """
    digest = hash_password(password)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".credentials-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"password_hash": digest}, handle, indent=1)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    restrict(path)
