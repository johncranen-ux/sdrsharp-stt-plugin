"""config.json: values only, defaults merged on read, validated and atomic on write."""
import json
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store  # noqa: E402
from webapp.settings_schema import BY_KEY  # noqa: E402


def test_a_missing_file_yields_the_defaults(tmp_path):
    values = config_store.load(tmp_path / "config.json")
    assert values["AISHUB_POLL_SEC"] == "900"
    assert values["AIS_SUGGEST_TIEBREAK"] == "off"


def test_a_stored_value_overrides_its_default(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"AISHUB_POLL_SEC": "1800"}), encoding="utf-8")
    assert config_store.load(path)["AISHUB_POLL_SEC"] == "1800"


def test_every_setting_is_present_after_load(tmp_path):
    """A caller building an environment must never have to ask whether a key exists."""
    values = config_store.load(tmp_path / "config.json")
    assert set(values) == set(BY_KEY)


def test_a_key_not_in_the_catalogue_is_refused_on_save(tmp_path):
    """The catalogue is the whole surface. A stray key would be a setting nobody described."""
    with pytest.raises(config_store.UnknownSetting, match="NOT_A_SETTING"):
        config_store.save(tmp_path / "config.json", {"NOT_A_SETTING": "1"})


def test_an_invalid_value_is_refused_before_anything_is_written(tmp_path):
    path = tmp_path / "config.json"
    config_store.save(path, {"AISHUB_POLL_SEC": "900"})
    with pytest.raises(ValueError, match="AISHUB_POLL_SEC"):
        config_store.save(path, {"AISHUB_POLL_SEC": "5"})
    assert json.loads(path.read_text(encoding="utf-8"))["AISHUB_POLL_SEC"] == "900"


def test_saving_leaves_no_temp_file_behind(tmp_path):
    path = tmp_path / "config.json"
    config_store.save(path, {"AISHUB_POLL_SEC": "900"})
    assert [p.name for p in tmp_path.iterdir()] == ["config.json"]


def test_a_round_trip_preserves_every_value(tmp_path):
    path = tmp_path / "config.json"
    original = config_store.load(path)
    original["AIS_SUGGEST_N"] = "5"
    config_store.save(path, original)
    assert config_store.load(path) == original


def test_a_secret_is_masked_for_display(tmp_path):
    values = config_store.load(tmp_path / "config.json")
    values["GROQ_API_KEY"] = "gsk_realkeymaterial"
    shown = config_store.redacted_values(values)
    assert shown["GROQ_API_KEY"] == "●●●●"
    assert "gsk_realkeymaterial" not in json.dumps(shown)


def test_an_unset_secret_reads_as_empty_not_as_masked(tmp_path):
    """Masking an empty value would tell the operator a key is set when it is not."""
    values = config_store.load(tmp_path / "config.json")
    assert config_store.redacted_values(values)["GROQ_API_KEY"] == ""


def test_redaction_leaves_non_secrets_alone(tmp_path):
    values = config_store.load(tmp_path / "config.json")
    assert config_store.redacted_values(values)["AISHUB_POLL_SEC"] == "900"


def test_a_config_that_is_not_valid_utf8_falls_back_to_defaults(tmp_path):
    """load() runs at server startup. UnicodeDecodeError subclasses ValueError rather than
    OSError, so it escapes an except clause that looks like it covers unreadable files."""
    path = tmp_path / "config.json"
    path.write_bytes(b"\xff\xfe\x00\x01not utf-8 at all \x80\x81")
    assert config_store.load(path)["AISHUB_POLL_SEC"] == "900"


def test_load_ignores_an_unknown_key_present_in_the_file(tmp_path):
    """A config written by a newer version must not stop an older one from starting, per
    load()'s own docstring."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"NOT_A_SETTING": "1", "AISHUB_POLL_SEC": "1800"}),
                     encoding="utf-8")
    values = config_store.load(path)
    assert "NOT_A_SETTING" not in values
    assert values["AISHUB_POLL_SEC"] == "1800"


def test_a_hand_edited_json_bool_does_not_silently_mistype(tmp_path):
    """A hand-edited {"AIS_CALLSIGN_SUFFIX_FALLBACK": true} used to become the Python string
    "True" via str(v). The proxy tests == "on", so the setting silently turned off."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"AIS_CALLSIGN_SUFFIX_FALLBACK": True}), encoding="utf-8")
    values = config_store.load(path)
    assert values["AIS_CALLSIGN_SUFFIX_FALLBACK"] == BY_KEY["AIS_CALLSIGN_SUFFIX_FALLBACK"].default


def test_a_stored_out_of_range_int_falls_back_to_its_default(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"AISHUB_POLL_SEC": "5"}), encoding="utf-8")
    values = config_store.load(path)
    assert values["AISHUB_POLL_SEC"] == BY_KEY["AISHUB_POLL_SEC"].default


def test_saving_one_key_leaves_the_other_keys_intact(tmp_path):
    path = tmp_path / "config.json"
    config_store.save(path, {"AISHUB_POLL_SEC": "1800"})
    config_store.save(path, {"AIS_SUGGEST_N": "5"})
    values = config_store.load(path)
    assert values["AIS_SUGGEST_N"] == "5"
    assert values["AISHUB_POLL_SEC"] == "1800"


def test_saving_the_mask_for_a_set_secret_leaves_it_unchanged(tmp_path):
    path = tmp_path / "config.json"
    fixture_key = "gsk_test_fixture_not_a_real_key"
    config_store.save(path, {"GROQ_API_KEY": fixture_key})
    config_store.save(path, {"GROQ_API_KEY": config_store.MASK, "AISHUB_POLL_SEC": "1800"})
    values = config_store.load(path)
    assert values["GROQ_API_KEY"] == fixture_key
    assert values["AISHUB_POLL_SEC"] == "1800"
