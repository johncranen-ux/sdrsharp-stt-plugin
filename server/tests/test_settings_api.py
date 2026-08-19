"""The settings form, and the one rule that matters: a secret leaves this process never."""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store, settings_api  # noqa: E402
from webapp.settings_schema import SETTINGS, SettingType  # noqa: E402

_SECRETS = [s.key for s in SETTINGS if s.type is SettingType.SECRET]


def _values(**over):
    values = config_store.load(Path("does-not-exist.json"))
    values.update(over)
    return values


def test_no_secret_value_appears_anywhere_in_the_form():
    values = _values(**{key: "super-secret-value" for key in _SECRETS})
    body = settings_api.form(values)
    assert "super-secret-value" not in str(body)


def test_a_secret_reports_only_whether_it_is_set():
    key = _SECRETS[0]
    assert settings_api.form(_values(**{key: "x"}))["fields"][key]["set"] is True
    assert settings_api.form(_values(**{key: ""}))["fields"][key]["set"] is False


def test_the_form_is_grouped_in_schema_order():
    groups = [g["name"] for g in settings_api.form(_values())["groups"]]
    assert groups == list(dict.fromkeys(s.group for s in SETTINGS))


def test_submitting_an_empty_secret_leaves_the_stored_one_alone():
    """The form cannot show a secret, so an empty box means "unchanged", not "clear it"."""
    key = _SECRETS[0]
    applied = settings_api.apply(_values(**{key: "kept"}), {key: ""})
    assert applied.values[key] == "kept"
    assert key not in applied.changed


def test_a_secret_can_be_cleared_explicitly():
    key = _SECRETS[0]
    applied = settings_api.apply(_values(**{key: "kept"}), {key: settings_api.CLEAR})
    assert applied.values[key] == ""
    assert key in applied.changed


def test_an_invalid_value_is_rejected_with_the_key_named():
    port = next(s.key for s in SETTINGS if s.type is SettingType.INT)
    with pytest.raises(settings_api.Invalid) as caught:
        settings_api.apply(_values(), {port: "not a number"})
    assert port in str(caught.value)


def test_an_unknown_key_is_refused_rather_than_stored():
    with pytest.raises(settings_api.Invalid):
        settings_api.apply(_values(), {"NOT_A_SETTING": "x"})


def test_the_response_says_which_processes_must_be_restarted():
    """A setting the proxy reads at startup is not live until it restarts, and a form that
    does not say so silently lies about what is in effect."""
    exported = next(s.key for s in SETTINGS if s.exported and s.type is SettingType.BOOL)
    applied = settings_api.apply(_values(), {exported: "off"})
    assert "proxy" in applied.restart_needed
