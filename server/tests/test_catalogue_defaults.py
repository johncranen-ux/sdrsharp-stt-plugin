"""Pin the setting catalogue's defaults against the proxy's own code defaults.

Fifteen settings appear in start-all.bat only as commented-out rollbacks, so for those the
catalogue default in settings_schema.py is the only value that will ever reach the proxy
after migration. Today it matches the proxy's own `os.environ.get(..., "default")` fallback;
nothing besides this test keeps the two in sync as either side is edited.
"""
import re
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp.settings_schema import BY_KEY  # noqa: E402

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
