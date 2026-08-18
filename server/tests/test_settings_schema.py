"""The setting catalogue: what the control panel is allowed to expose, and how each
value is validated.

Scope is deliberately the 26 settings start-all.bat names. The proxy reads 65 env vars;
the rest are code defaults that no operator should be editing from a web form.
"""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.settings_schema import BY_KEY, SETTINGS, SettingType, validate_value  # noqa: E402


def test_every_setting_has_a_description():
    """The description carries the prose from start-all.bat -- the sea-box reasoning, the
    rollback notes, the rate-limit warning. A setting without one is a knob with its
    documentation thrown away."""
    missing = [s.key for s in SETTINGS if not s.description.strip()]
    assert missing == []


def test_keys_are_unique():
    assert len(BY_KEY) == len(SETTINGS)


def test_the_api_keys_are_marked_secret():
    for key in ("ANTHROPIC_API_KEY", "GROQ_API_KEY", "OPENROUTER_API_KEY",
                "AISSTREAM_API_KEY", "AISSTREAM_API_KEY2", "AISHUB_USERNAME"):
        assert BY_KEY[key].type is SettingType.SECRET, key


def test_settings_the_proxy_reads_but_the_operator_should_not_touch_are_absent():
    """AIS_HINT_MIN_SCORE cost 11 precision points when relaxed and WHISPER_PROMPT cost ~11
    WER points; neither belongs in a web form, and neither is in start-all.bat."""
    for key in ("AIS_HINT_MIN_SCORE", "WHISPER_PROMPT", "AIS_SUGGEST_FLOOR",
                "CONVERSATION_CORRECT_MODEL"):
        assert key not in BY_KEY, key


def test_a_bool_accepts_on_and_off_only():
    spec = BY_KEY["AIS_HINT_FILTER"]
    assert validate_value(spec, "off") == "off"
    with pytest.raises(ValueError):
        validate_value(spec, "false")


def test_an_enum_rejects_a_value_outside_its_choices():
    spec = BY_KEY["STT_BACKEND"]
    assert validate_value(spec, "whisper_cpp") == "whisper_cpp"
    with pytest.raises(ValueError, match="STT_BACKEND"):
        validate_value(spec, "vosk")


def test_an_int_below_its_minimum_is_rejected():
    """AISHUB answers a caller polling faster than 60 s with no data at all."""
    spec = BY_KEY["AISHUB_POLL_SEC"]
    assert validate_value(spec, "900") == "900"
    with pytest.raises(ValueError, match="60"):
        validate_value(spec, "30")


def test_a_bbox_needs_four_numbers_in_range():
    spec = BY_KEY["AISHUB_BBOX"]
    assert validate_value(spec, "51.4,52.6,2.0,4.25") == "51.4,52.6,2.0,4.25"
    with pytest.raises(ValueError):
        validate_value(spec, "51.4,52.6,2.0")


def test_a_bbox_with_min_above_max_is_rejected():
    """Silently inverted bounds would return an empty vessel box and look like a dead feed."""
    with pytest.raises(ValueError, match="latmin"):
        validate_value(BY_KEY["AISHUB_BBOX"], "52.6,51.4,2.0,4.25")


def test_the_sea_box_reasoning_survived_into_the_description():
    """That comment is some of the best documentation in the project."""
    assert "4.25" in BY_KEY["AISHUB_BBOX"].description


def test_the_ais_feed_can_be_switched_off():
    """whisper-proxy.py falls through to "AIS feed: disabled" on any value that is not
    aishub or aisstream, so a two-choice enum would remove the only way to turn it off."""
    assert validate_value(BY_KEY["AIS_SOURCE"], "off") == "off"


def test_every_default_is_valid_against_its_own_spec():
    """A default that its own validator rejects would fail on first save, not at edit time."""
    for spec in SETTINGS:
        validate_value(spec, spec.default)


def test_every_enum_offers_choices_and_every_range_is_ordered():
    for spec in SETTINGS:
        if spec.type is SettingType.ENUM:
            assert spec.choices, f"{spec.key} is an enum with no choices"
        if spec.minimum is not None and spec.maximum is not None:
            assert spec.minimum <= spec.maximum, f"{spec.key} has minimum above maximum"
