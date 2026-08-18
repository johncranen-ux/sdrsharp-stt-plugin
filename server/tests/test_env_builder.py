"""Turning stored settings into the environment a child process is started with."""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.env_builder import build_env  # noqa: E402


def test_a_setting_becomes_an_environment_variable():
    env = build_env({"AISHUB_POLL_SEC": "900"}, base={})
    assert env["AISHUB_POLL_SEC"] == "900"


def test_an_empty_value_is_omitted_rather_than_exported_empty():
    """ANTHROPIC_API_KEY="" would look present and fail later with a confusing error, where
    unset is documented to disable identification cleanly."""
    env = build_env({"ANTHROPIC_API_KEY": ""}, base={})
    assert "ANTHROPIC_API_KEY" not in env


def test_the_base_environment_is_inherited():
    """The child needs PATH and SystemRoot; building an env from nothing breaks Python."""
    env = build_env({"AISHUB_POLL_SEC": "900"}, base={"PATH": "C:\\Windows"})
    assert env["PATH"] == "C:\\Windows"


def test_a_setting_overrides_the_same_name_in_the_base():
    """A stale value inherited from the launching shell must not win over config.json."""
    env = build_env({"AISHUB_POLL_SEC": "1800"}, base={"AISHUB_POLL_SEC": "900"})
    assert env["AISHUB_POLL_SEC"] == "1800"


def test_an_empty_setting_removes_an_inherited_value():
    """Otherwise clearing a key in the UI would silently keep working from the old shell."""
    env = build_env({"ANTHROPIC_API_KEY": ""}, base={"ANTHROPIC_API_KEY": "sk-stale"})
    assert "ANTHROPIC_API_KEY" not in env


def test_a_key_outside_the_catalogue_is_not_exported():
    env = build_env({"NOT_A_SETTING": "1"}, base={})
    assert "NOT_A_SETTING" not in env
