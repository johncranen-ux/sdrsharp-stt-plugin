"""The one password that guards a panel which starts processes and holds six API keys."""
import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import credentials  # noqa: E402


def test_a_saved_password_verifies_and_a_wrong_one_does_not(tmp_path):
    path = tmp_path / "credentials.json"
    credentials.save_password(path, "correct horse battery staple")

    stored = credentials.load_hash(path)
    assert credentials.verify_password(stored, "correct horse battery staple") is True
    assert credentials.verify_password(stored, "correct horse battery stapl") is False


def test_the_password_itself_is_never_written_to_disk(tmp_path):
    path = tmp_path / "credentials.json"
    credentials.save_password(path, "correct horse battery staple")
    assert "correct horse" not in path.read_text(encoding="utf-8")
    assert json.loads(path.read_text(encoding="utf-8"))["password_hash"].startswith("$argon2")


def test_no_password_file_means_no_password(tmp_path):
    """Distinct from an empty one. check_bind_allowed refuses a non-loopback bind on this."""
    assert credentials.load_hash(tmp_path / "credentials.json") is None
    assert credentials.has_password(tmp_path / "credentials.json") is False


def test_a_short_password_is_refused(tmp_path):
    """This panel is reachable from a LAN and executes processes. Twelve characters is the
    floor; rate limiting (auth.py) covers the rest."""
    with pytest.raises(ValueError, match="at least 12"):
        credentials.save_password(tmp_path / "credentials.json", "hunter2")
    assert not (tmp_path / "credentials.json").exists()


def test_a_corrupt_credentials_file_reads_as_no_password(tmp_path):
    """Fail closed on reading: a damaged file must not verify anything, and must not crash
    the app at import either."""
    path = tmp_path / "credentials.json"
    path.write_text("{not json", encoding="utf-8")
    assert credentials.load_hash(path) is None


def test_verify_tolerates_a_hash_it_cannot_parse():
    assert credentials.verify_password("not-a-hash", "anything") is False
    assert credentials.verify_password(None, "anything") is False
    assert credentials.verify_password("", "") is False


def test_the_stored_file_is_restricted_to_this_account(tmp_path):
    path = tmp_path / "credentials.json"
    credentials.save_password(path, "correct horse battery staple")
    import os
    import subprocess
    if os.name != "nt":
        pytest.skip("Windows ACLs")
    out = subprocess.run(["icacls", str(path)], capture_output=True, text=True).stdout
    grantees = {line.replace(str(path), "").strip().split(":(")[0].lower()
                for line in out.splitlines() if ":(" in line}
    assert len(grantees) == 1
