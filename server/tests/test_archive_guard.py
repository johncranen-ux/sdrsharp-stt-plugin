"""The guard that stops a test writing into the operator's real ground truth."""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import conversation_archive as archive  # noqa: E402


def test_opening_the_real_archive_is_refused():
    real = archive.default_db_path(_SERVER_DIR)
    with pytest.raises(AssertionError, match="the real one"):
        archive.connect(real)


def test_a_temporary_archive_is_allowed(tmp_path):
    conn = archive.connect(tmp_path / "a.db")
    conn.close()
