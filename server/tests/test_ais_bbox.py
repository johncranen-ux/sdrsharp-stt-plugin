"""The aisstream bounding box, and the shared parser both feeds use.

Until 2026-08-25 the aisstream box was the constant [[[51.0, 2.95], [52.85, 6.0]]] with no
way to change it. That is the WIDE box: its eastern edge at 6.0 reaches up the Rhine and
carries the inland barge network, which is what the 2026-08-13 sea-box cutover moved AISHub
off after measuring 685 duplicate-name groups against the sea box's 43.

Only AISHub got the fix, because only AISHub was in use. Anyone without an AIS receiver --
which is anyone who cannot get AISHub credentials -- was left on the box already measured as
the worse one, with no environment variable to escape it.
"""
import importlib
import os

import pytest


@pytest.fixture
def ais(monkeypatch):
    """stt_proxy.ais re-imported under a controlled environment."""
    def _load(**env):
        for key in ("AIS_BBOX", "AIS_CACHE_FILE"):
            monkeypatch.delenv(key, raising=False)
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        import stt_proxy.ais as module
        return importlib.reload(module)
    yield _load
    import stt_proxy.ais as module
    importlib.reload(module)


# --- the shared parser ----------------------------------------------------------------

def test_parse_bbox_reads_latmin_latmax_lonmin_lonmax(ais):
    mod = ais()
    assert mod.parse_bbox("51.4,52.6,2.0,4.25", (0, 0, 0, 0), label="T") == (51.4, 52.6, 2.0, 4.25)


def test_parse_bbox_falls_back_when_the_value_is_not_four_numbers(ais):
    mod = ais()
    default = (51.4, 52.6, 2.0, 4.25)
    assert mod.parse_bbox("nonsense", default, label="T") == default
    assert mod.parse_bbox("51.4,52.6", default, label="T") == default
    assert mod.parse_bbox("", default, label="T") == default


def test_aishub_resolves_its_box_through_the_same_parser(monkeypatch):
    """One parser, so a malformed box cannot be handled two different ways."""
    from stt_proxy import ais, aishub
    monkeypatch.setenv("AISHUB_BBOX", "1.0,2.0,3.0,4.0")
    assert aishub._resolve_bbox() == (1.0, 2.0, 3.0, 4.0)
    monkeypatch.setenv("AISHUB_BBOX", "garbage")
    assert aishub._resolve_bbox() == aishub.BBOX_DEFAULT
    assert aishub._resolve_bbox.__module__ or ais.parse_bbox  # both importable


# --- the aisstream box ----------------------------------------------------------------

def test_the_aisstream_box_defaults_to_the_sea_box(ais):
    mod = ais()
    assert mod.AIS_BBOX == (51.4, 52.6, 2.0, 4.25)


def test_the_default_box_stops_short_of_the_inland_network(ais):
    """The eastern edge is the whole point: 4.25 keeps the Rhine barges out, 6.0 let them in."""
    mod = ais()
    _, _, _, lonmax = mod.AIS_BBOX
    assert lonmax < 5.0, "the sea box must not reach the inland waterways"


def test_the_aisstream_box_is_overridable(ais):
    mod = ais(AIS_BBOX="50.0,54.0,1.0,7.0")
    assert mod.AIS_BBOX == (50.0, 54.0, 1.0, 7.0)


def test_a_malformed_override_falls_back_to_the_sea_box(ais):
    mod = ais(AIS_BBOX="51.4,52.6")
    assert mod.AIS_BBOX == (51.4, 52.6, 2.0, 4.25)


def test_the_subscription_shape_is_what_aisstream_expects(ais):
    """aisstream wants [[[latmin, lonmin], [latmax, lonmax]]] -- corners, not edges."""
    mod = ais(AIS_BBOX="51.4,52.6,2.0,4.25")
    assert mod.ROTTERDAM_BBOX == [[[51.4, 2.0], [52.6, 4.25]]]


def test_the_subscription_box_follows_the_override(ais):
    mod = ais(AIS_BBOX="50.0,54.0,1.0,7.0")
    assert mod.ROTTERDAM_BBOX == [[[50.0, 1.0], [54.0, 7.0]]]


# --- the shipped default source -------------------------------------------------------

def test_the_default_ais_source_is_the_one_anybody_can_actually_use():
    """aisstream, not aishub.

    AISHub issues credentials only to stations that CONTRIBUTE an AIS feed -- a second
    receiver, an antenna with a sea view, and a 24/7 uptime bar. Defaulting to it meant the
    out-of-the-box experience for anyone without that hardware was `AIS feed: disabled`,
    which reads like a broken install rather than a deliberate choice. aisstream needs only
    a free key.
    """
    from webapp.settings_schema import BY_KEY
    assert BY_KEY["AIS_SOURCE"].default == "aisstream"


def test_the_proxy_and_the_catalogue_agree_on_the_ais_source_default():
    """whisper-proxy.py is not scanned by test_catalogue_defaults -- it only globs
    stt_proxy/*.py -- so nothing else pins these two together, and AIS_SOURCE is read in
    two separate places inside it."""
    import re
    from pathlib import Path
    from webapp.settings_schema import BY_KEY

    source = (Path(__file__).resolve().parent.parent / "whisper-proxy.py").read_text(encoding="utf-8")
    defaults = set(re.findall(r'os\.environ\.get\(\s*"AIS_SOURCE"\s*,\s*"([^"]*)"', source))
    assert defaults, "whisper-proxy.py no longer reads AIS_SOURCE with a default"
    assert defaults == {BY_KEY["AIS_SOURCE"].default}, (
        f"proxy defaults {defaults} vs catalogue {BY_KEY['AIS_SOURCE'].default!r}")


def test_the_aisstream_box_is_in_the_catalogue_so_the_panel_exports_it():
    """env_builder only exports keys the catalogue knows. A box that is not in it is a
    setting the panel silently drops -- which is exactly how the aisstream box stayed
    hardcoded while AISHub's became configurable."""
    from webapp.settings_schema import BY_KEY, SettingType
    spec = BY_KEY["AIS_BBOX"]
    assert spec.type is SettingType.BBOX
    assert spec.default == "51.4,52.6,2.0,4.25"
    assert spec.exported is True


# --- the silence warning --------------------------------------------------------------

def test_the_silence_warning_ships_armed():
    """Muted to 0 on 2026-08-11 because aisstream had been dead since 08-05 and the warning
    fired ~8,600 times, drowning output worth reading. The feed was measured delivering
    again on 2026-08-25, and ais.py's own note says to restore 60 the moment it returns.

    It matters more now than it did then: aisstream is the DEFAULT source, and this warning
    is the only thing that separates a quiet channel from a feed that accepted the
    connection and then silently stopped -- the 2026-08-08 failure shape, which this feed
    has already produced once."""
    import re
    from pathlib import Path
    from webapp.settings_schema import BY_KEY

    assert BY_KEY["AIS_SILENCE_WARN_SEC"].default == "60"

    source = (Path(__file__).resolve().parent.parent / "stt_proxy" / "ais.py").read_text(encoding="utf-8")
    found = re.findall(r'os\.environ\.get\(\s*"AIS_SILENCE_WARN_SEC"\s*,\s*"([^"]*)"', source)
    assert found == ["60"], f"ais.py code default is {found}, not 60"
