"""Restrict a file so only this account can read it.

config.json and credentials.json hold, respectively, six API keys and the password hash that
guards a panel able to start processes. Both inherit their directory's ACL, which on a normal
Windows install means every local user can read them.

icacls rather than moving the files to %LOCALAPPDATA%: the config must stay beside the code it
configures, so that a host migration is a copy of one directory rather than a hunt through two.
"""
from __future__ import annotations

import getpass
import os
import subprocess
from pathlib import Path


def restrict(path: Path) -> bool:
    """Break inheritance and grant full control to the current user alone.

    Returns whether it worked. It never raises: hardening is defence in depth, and a failure
    here must not become the reason an operator cannot save a setting or a password.
    """
    path = Path(path)
    if os.name != "nt" or not path.exists():
        return False
    try:
        done = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{getpass.getuser()}:F"],
            capture_output=True, text=True, timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0
