"""Pin the setting catalogue's defaults against the proxy's own code defaults.

Fifteen settings appear in start-all.bat only as commented-out rollbacks, so for those the
catalogue default in settings_schema.py is the only value that will ever reach the proxy
after migration. Today it matches the proxy's own `os.environ.get(..., "default")` fallback;
nothing besides this test keeps the two in sync as either side is edited.
"""
import re
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.settings_schema import BY_KEY, SettingType, validate_value  # noqa: E402

_ENV_DEFAULT = re.compile(r'os\.environ\.get\(\s*"([A-Z_0-9]+)"\s*,\s*"([^"]*)"')

# AISHUB_BBOX diverges ON PURPOSE: the catalogue carries the sea box adopted 2026-08-13,
# while the proxy's code default is still the old wide box. Documented, not accidental.
_DELIBERATE_DIVERGENCE = {"AISHUB_BBOX"}


def test_catalogue_defaults_match_the_proxy_code_defaults():
    """For the settings start-all.bat only comments out, the catalogue default is the only
    value that reaches the proxy after migration. A drift would silently change behaviour."""
    found: dict[str, str] = {}
    for path in (_SERVER_DIR / "stt_proxy").glob("*.py"):
        for key, default in _ENV_DEFAULT.findall(path.read_text(encoding="utf-8")):
            found.setdefault(key, default)
    mismatched = {
        key: (spec.default, found[key])
        for key, spec in BY_KEY.items()
        if key in found and key not in _DELIBERATE_DIVERGENCE and spec.default != found[key]
    }
    assert mismatched == {}, f"catalogue drifted from the proxy's code defaults: {mismatched}"


def test_conversations_keep_is_in_the_catalogue_and_cannot_be_set_to_zero():
    """The rolling window's size must be settable from the panel, and never settable to 0.

    Settable: the proxy reads CONVERSATIONS_KEEP from its environment, and env_builder only
    exports keys that are in this catalogue -- so a panel-managed proxy silently ignored any
    value until this spec existed.

    Never 0: `rows[-0:]` is `rows[0:]`, the WHOLE list. A KEEP of 0 therefore does not keep
    nothing, it disables the cap entirely and lets conversations.json grow without bound --
    which webapp/proxy_data.py then fetches in full every 15 seconds. minimum=1 makes that
    unreachable rather than merely undocumented.
    """
    spec = BY_KEY["CONVERSATIONS_KEEP"]
    assert spec.type is SettingType.INT
    assert spec.default == "300"       # pinned against the proxy's own fallback by the test above
    assert spec.exported is True
    assert spec.minimum == 1

    with pytest.raises(ValueError):
        validate_value(spec, "0")
    assert validate_value(spec, "150") == "150"
