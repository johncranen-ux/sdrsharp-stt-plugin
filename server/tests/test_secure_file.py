"""config.json holds six plaintext API keys. It inherits its directory's ACL, which on a
normal Windows install lets other accounts read it. This is the at-rest half of "secrets
never leave the server"."""
import getpass
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.secure_file import restrict  # noqa: E402

pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows ACLs")


def _grantees(path: Path) -> set[str]:
    """The accounts icacls lists for a file, lowercased.

    icacls prints the path on the first line followed by `ACCOUNT:(FLAGS)` entries, wrapping
    onto continuation lines that carry only the entry.
    """
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    found = set()
    for line in out.splitlines():
        entry = line.replace(str(path), "").strip()
        if ":(" in entry:
            found.add(entry.split(":(")[0].lower())
    return found


def test_restrict_leaves_this_account_as_the_only_grantee(tmp_path):
    """The pre-state is asserted too, so this cannot pass vacuously on a machine whose
    temp directory happens to be tight already."""
    secret = tmp_path / "config.json"
    secret.write_text('{"GROQ_API_KEY": "gsk_example"}', encoding="utf-8")

    before = _grantees(secret)
    assert len(before) > 1, f"nothing to remove; the test proves nothing here: {before}"

    assert restrict(secret) is True

    after = _grantees(secret)
    assert len(after) == 1, f"expected one grantee, got {after}"
    assert getpass.getuser().lower() in next(iter(after))
    assert secret.read_text(encoding="utf-8") == '{"GROQ_API_KEY": "gsk_example"}'


def test_restrict_reports_failure_rather_than_raising(tmp_path):
    """A hardening failure must never be the reason a setting cannot be saved."""
    assert restrict(tmp_path / "does-not-exist.json") is False
