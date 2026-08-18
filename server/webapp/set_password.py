"""py -m webapp.set_password -- the only way the control panel's password is set.

Deliberately not a route: an unauthenticated "set the first password" endpoint is exactly the
window that authentication exists to close.
"""
from __future__ import annotations

import getpass
import sys
from pathlib import Path

from webapp.credentials import MIN_LENGTH, save_password

CREDENTIALS_PATH = Path(__file__).resolve().parent.parent / "credentials.json"


def main(argv: list[str] | None = None) -> int:
    path = Path(argv[0]) if argv else CREDENTIALS_PATH
    first = getpass.getpass("New control panel password: ")
    second = getpass.getpass("Repeat: ")
    if first != second:
        print("They do not match. Nothing was written.", file=sys.stderr)
        return 1
    try:
        save_password(path, first)
    except ValueError as exc:
        print(f"{exc}. Nothing was written.", file=sys.stderr)
        return 1
    print(f"Password set in {path} (minimum length {MIN_LENGTH}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
