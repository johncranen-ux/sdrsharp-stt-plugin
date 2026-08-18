# Control Panel — Phase 1: Settings Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every operator-facing setting a validated, described, typed value in `config.json`, and be able to launch the proxy from it exactly as `start-all.bat` does today.

**Architecture:** A pydantic `SettingSpec` catalogue is the single source of truth: it validates saves, drives form rendering later, and builds the environment dict a child process is started with. `config.json` holds values only. A one-time importer reads current values out of `start-all.bat`; the batch file is then kept read-only as a fallback and never regenerated.

**Tech Stack:** Python 3.14, pydantic v2, pytest. No web framework in this phase — no routes, no UI, no process launching. Those are Phase 2.

**Spec:** `docs/superpowers/specs/2026-08-18-control-panel-webapp-design.md`

## Global Constraints

- **Windows 11.** Paths are Windows paths; use `pathlib.Path`, never assume POSIX separators.
- **Scope is the settings `start-all.bat` exposes** — nothing else. 27 settings after the 2026-08-18 additions. The proxy reads 65 env vars; the other 38 stay as code defaults and must NOT appear.
- **Secrets never leave the server.** `SettingSpec.secret` marks them; anything rendering or serialising for a client must be able to mask by that flag alone. In this phase that means `redacted_values()` exists and is tested.
- **Atomic writes.** `config.json` is written temp-then-replace so an interrupted save cannot truncate it.
- **`config.json` is gitignored.** It holds API keys. Add the ignore rule in Task 1.
- **Never regenerate `start-all.bat`.** It is read-only input. Its prose comments are the source of the `description` fields and must not be destroyed.
- **Tests live in `server/tests/`** beside the existing 809 and run with `py -m pytest server/tests`.

---

## File Structure

| file | responsibility |
|---|---|
| `server/webapp/__init__.py` | empty package marker |
| `server/webapp/settings_schema.py` | `SettingSpec`, `SettingType`, and `SETTINGS` — the catalogue of 27 |
| `server/webapp/config_store.py` | load/save `config.json`, atomic write, defaults merge, `redacted_values()` |
| `server/webapp/env_builder.py` | values dict → environment dict for a child process |
| `server/webapp/import_batch.py` | one-time read of current values out of `start-all.bat` |
| `server/tests/test_settings_schema.py` | catalogue shape and per-type validation |
| `server/tests/test_config_store.py` | round-trip, atomicity, redaction |
| `server/tests/test_env_builder.py` | env dict correctness and omission rules |
| `server/tests/test_import_batch.py` | parsing the real batch file |

Four small modules rather than one `settings.py`: the catalogue is data, the store is I/O, the env builder is pure transformation, and the importer is a one-shot migration that will be deleted once every deployment has run it. They change for different reasons.

---

### Task 1: The setting catalogue

**Files:**
- Create: `server/webapp/__init__.py`
- Create: `server/webapp/settings_schema.py`
- Create: `server/tests/test_settings_schema.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: nothing.
- Produces: `SettingType` (str enum: `SECRET`, `TEXT`, `INT`, `BOOL`, `ENUM`, `BBOX`, `PATH`), `SettingSpec` (pydantic model with fields `key: str`, `type: SettingType`, `default: str`, `group: str`, `description: str`, `choices: list[str] | None = None`, `minimum: int | None = None`, `maximum: int | None = None`), `SETTINGS: list[SettingSpec]`, `BY_KEY: dict[str, SettingSpec]`, and `validate_value(spec: SettingSpec, raw: str) -> str` which returns the normalised string or raises `ValueError`.

Every value is stored and returned as a **string**, because that is what an environment variable is. Typing exists to validate and to render, not to change the storage format.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_settings_schema.py
"""The setting catalogue: what the control panel is allowed to expose, and how each
value is validated.

Scope is deliberately the 27 settings start-all.bat names. The proxy reads 65 env vars;
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest server/tests/test_settings_schema.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp'`

- [ ] **Step 3: Write minimal implementation**

Create `server/webapp/__init__.py` empty. Then:

```python
# server/webapp/settings_schema.py
"""What the control panel is allowed to expose, and how each value is validated.

Scope is the settings start-all.bat names -- 27 of the 65 environment variables the proxy
reads. That file is the curated operator surface: a setting becomes operator-facing by being
added there with the prose comment that explains it, so this catalogue inherits that
documentation rather than competing with it.

Every value is stored as a STRING, because that is what an environment variable is. The type
exists to validate input and to render a control, never to change the storage format.
"""
from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class SettingType(str, enum.Enum):
    SECRET = "secret"
    TEXT = "text"
    INT = "int"
    BOOL = "bool"
    ENUM = "enum"
    BBOX = "bbox"
    PATH = "path"


class SettingSpec(BaseModel):
    key: str
    type: SettingType
    default: str
    group: str
    description: str
    choices: list[str] | None = None
    minimum: int | None = None
    maximum: int | None = None


BOOL_CHOICES = ("on", "off")


def validate_value(spec: SettingSpec, raw: str) -> str:
    """Return the normalised value, or raise ValueError naming the setting."""
    value = (raw or "").strip()

    if spec.type is SettingType.BOOL:
        if value.lower() not in BOOL_CHOICES:
            raise ValueError(f"{spec.key}: expected 'on' or 'off', got {raw!r}")
        return value.lower()

    if spec.type is SettingType.ENUM:
        if value not in (spec.choices or []):
            raise ValueError(
                f"{spec.key}: expected one of {', '.join(spec.choices or [])}, got {raw!r}")
        return value

    if spec.type is SettingType.INT:
        try:
            number = int(value)
        except ValueError:
            raise ValueError(f"{spec.key}: expected a whole number, got {raw!r}") from None
        if spec.minimum is not None and number < spec.minimum:
            raise ValueError(f"{spec.key}: must be at least {spec.minimum}, got {number}")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError(f"{spec.key}: must be at most {spec.maximum}, got {number}")
        return str(number)

    if spec.type is SettingType.BBOX:
        parts = [p.strip() for p in value.split(",")]
        if len(parts) != 4:
            raise ValueError(
                f"{spec.key}: expected latmin,latmax,lonmin,lonmax, got {raw!r}")
        try:
            latmin, latmax, lonmin, lonmax = (float(p) for p in parts)
        except ValueError:
            raise ValueError(f"{spec.key}: all four bounds must be numbers, got {raw!r}") from None
        if not (-90 <= latmin <= 90 and -90 <= latmax <= 90):
            raise ValueError(f"{spec.key}: latitude out of range in {raw!r}")
        if not (-180 <= lonmin <= 180 and -180 <= lonmax <= 180):
            raise ValueError(f"{spec.key}: longitude out of range in {raw!r}")
        # Inverted bounds return an empty vessel box, which looks exactly like a dead feed.
        if latmin >= latmax:
            raise ValueError(f"{spec.key}: latmin must be below latmax, got {raw!r}")
        if lonmin >= lonmax:
            raise ValueError(f"{spec.key}: lonmin must be below lonmax, got {raw!r}")
        return ",".join(parts)

    return value


SETTINGS: list[SettingSpec] = [
    # ---- Secrets -------------------------------------------------------------
    SettingSpec(key="ANTHROPIC_API_KEY", type=SettingType.SECRET, default="", group="Secrets",
                description="Enables conversation resolution and correction. Unset disables "
                            "identification entirely."),
    SettingSpec(key="GROQ_API_KEY", type=SettingType.SECRET, default="", group="Secrets",
                description="Required when STT_BACKEND is groq, which is the default."),
    SettingSpec(key="OPENROUTER_API_KEY", type=SettingType.SECRET, default="", group="Secrets",
                description="Alternative LLM provider for the correction pass."),
    SettingSpec(key="AISSTREAM_API_KEY", type=SettingType.SECRET, default="", group="Secrets",
                description="Only used when AIS_SOURCE is aisstream."),
    SettingSpec(key="AISSTREAM_API_KEY2", type=SettingType.SECRET, default="", group="Secrets",
                description="Second aisstream key, used as a fallback."),
    SettingSpec(key="AISHUB_USERNAME", type=SettingType.SECRET, default="", group="Secrets",
                description="AISHub username, issued for a station contributing an AIS feed. "
                            "Signing up alone is not enough."),

    # ---- STT -----------------------------------------------------------------
    SettingSpec(key="STT_BACKEND", type=SettingType.ENUM, default="groq", group="STT",
                choices=["groq", "whisper_cpp"],
                description="groq is Groq's hosted Whisper API, no GPU involved, and is what "
                            "this deployment uses. whisper_cpp is a local whisper.cpp server "
                            "on an AMD GPU under WSL2 -- fully supported for anyone running "
                            "this with their own hardware. Changing this needs a restart."),
    SettingSpec(key="GROQ_MODEL", type=SettingType.TEXT, default="whisper-large-v3", group="STT",
                description="Groq's Whisper model. large-v3 measured 17.1% pooled WER on "
                            "235 English clips."),
    SettingSpec(key="WHISPER_BACKEND_PORT", type=SettingType.INT, default="8080", group="STT",
                minimum=1, maximum=65535,
                description="Port the local whisper.cpp server listens on inside WSL. Only "
                            "used when STT_BACKEND is whisper_cpp."),

    # ---- AIS source ----------------------------------------------------------
    SettingSpec(key="AIS_SOURCE", type=SettingType.ENUM, default="aishub", group="AIS source",
                choices=["aishub", "aisstream"],
                description="Where vessel data comes from. aishub polls a REST API; aisstream "
                            "is a websocket feed, kept live and tested so reverting works."),
    SettingSpec(key="AISHUB_BBOX", type=SettingType.BBOX, default="51.4,52.6,2.0,4.25",
                group="AIS source",
                description="latmin,latmax,lonmin,lonmax. The sea box, set 2026-08-13: Maas "
                            "Approach works ships at sea, never river traffic already inside. "
                            "The old wide box (51.0,53.2,2.0,6.0) carried the whole Rhine/Maas "
                            "inland network -- 8,381 vessels with 685 duplicate-name groups "
                            "against this box's 1,537 and 43, a 94% cut in the name collisions "
                            "that cause misidentification. The east edge is 4.25, PAST Hoek van "
                            "Holland (4.12), on purpose: MINERAL JINDEOK was at 4.113 while "
                            "calling."),
    SettingSpec(key="AISHUB_POLL_SEC", type=SettingType.INT, default="900", group="AIS source",
                minimum=60, maximum=86400,
                description="Seconds between AISHub polls. Values under 60 are refused: AISHub "
                            "answers a faster caller with no data at all."),
    SettingSpec(key="AIS_SILENCE_WARN_SEC", type=SettingType.INT, default="0", group="AIS source",
                minimum=0, maximum=86400,
                description="Warn when a CONNECTED AIS feed stops delivering -- the failure "
                            "that otherwise looks identical to a quiet channel. 0 is off. "
                            "Applies to the aisstream path only; AISHub reports its own failed "
                            "polls. Six days were once lost to a feed that failed quietly."),
    SettingSpec(key="AIS_CACHE_FILE", type=SettingType.PATH, default="", group="AIS source",
                description="Override where the vessel cache lives. Leave EMPTY for production. "
                            "A bench must point at a frozen snapshot from the week its labels "
                            "cover; arms measured against different caches are not comparable."),

    # ---- Identification ------------------------------------------------------
    SettingSpec(key="CONVERSATION_RESOLVER", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Decide vessel identity after each exchange ends, from the whole "
                            "exchange rather than one transmission. Never touches the live "
                            "transcript."),
    SettingSpec(key="AIS_HINT_FILTER", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Stops ordinary speech ('good day') being matched to real ships."),
    SettingSpec(key="AIS_NAME_FILTER", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Stops a mis-heard name matching a short vessel spelled inside it "
                            "('Orason' -> RA). Off restores the old WRatio scorer at cutoff 80."),
    SettingSpec(key="AIS_PARTIAL_CALLSIGN", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Identify a vessel from a partly-garbled spelled-out callsign when "
                            "a spoken name agrees. Off restores exact-callsign matching only."),
    SettingSpec(key="RESOLVER_LIVE_CANDIDATES", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Offer the resolver the vessel the live per-transmission pass "
                            "already matched, as a lead rather than a verdict."),
    SettingSpec(key="PROMPT_ECHO_FILTER", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Drops transcriptions that are the decoding prompt read back."),
    SettingSpec(key="AIS_LIVE_MATCH_MAX_AGE_MIN", type=SettingType.INT, default="360",
                group="Identification", minimum=0, maximum=100000,
                description="How old a vessel's AIS fix may be for the live pass to re-offer "
                            "it. Age counts from the last SUCCESSFUL poll, not the wall clock, "
                            "so a stalled feed freezes the cutoff instead of ageing every ship "
                            "out at once. Measured 2026-08-18: precision 87.1 -> 88.3, six "
                            "false positives removed, nothing lost. 0 disables the bound."),
    SettingSpec(key="AIS_CALLSIGN_SUFFIX_FALLBACK", type=SettingType.BOOL, default="on",
                group="Identification",
                description="Try the TAIL of a spelled-out callsign that decoded cleanly but "
                            "short ('call SUNvictor seven' swallowed the V of V7B2710). The "
                            "tail must fit exactly one cached callsign AND a resembling name "
                            "must be spoken in the same conversation. On by decision rather "
                            "than by measurement -- if identification regresses, switch this "
                            "off first."),
    SettingSpec(key="AIS_SUGGEST", type=SettingType.BOOL, default="on", group="Identification",
                description="Under a conversation nobody was identified in, show the best few "
                            "vessel names found BELOW the identification cutoff, labelled "
                            "unconfirmed. Never names anyone. Right ship in the list 9 times "
                            "out of 35."),
    SettingSpec(key="AIS_SUGGEST_N", type=SettingType.INT, default="3", group="Identification",
                minimum=1, maximum=10,
                description="How many possible matches to list. 5 finds 3 more of the 35 at "
                            "the cost of two more wrong names to read each time."),
    SettingSpec(key="AIS_SUGGEST_TIEBREAK", type=SettingType.BOOL, default="off",
                group="Identification",
                description="Rank equally-scoring suggestions by plausibility instead of "
                            "arbitrarily. OFF: it could not be measured, because scoring "
                            "proximity needs each vessel's position AT THE TIME and a frozen "
                            "cache keeps only the latest fix."),

    # ---- Ports ---------------------------------------------------------------
    SettingSpec(key="PROXY_PORT", type=SettingType.INT, default="9000", group="Ports",
                minimum=1, maximum=65535,
                description="Port the proxy listens on. The SDR# plugin must be pointed at the "
                            "same port."),
]

BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SETTINGS}
```

Add to `.gitignore`, beside the existing secret-bearing entries:

```
# Holds the API keys the control panel manages.
config.json
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m pytest server/tests/test_settings_schema.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add server/webapp/__init__.py server/webapp/settings_schema.py server/tests/test_settings_schema.py .gitignore
git commit -m "Describe the operator-facing settings as a validated catalogue"
```

---

### Task 2: The config store

**Files:**
- Create: `server/webapp/config_store.py`
- Create: `server/tests/test_config_store.py`

**Interfaces:**
- Consumes: `SETTINGS`, `BY_KEY`, `SettingType`, `validate_value` from Task 1.
- Produces: `load(path: Path) -> dict[str, str]` (defaults merged under stored values), `save(path: Path, values: dict[str, str]) -> None` (validates every value, writes atomically), `redacted_values(values: dict[str, str]) -> dict[str, str]` (secrets replaced by `"●●●●"` when non-empty, `""` when unset), and `UnknownSetting(ValueError)`.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_config_store.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest server/tests/test_config_store.py -v`
Expected: FAIL — `ImportError: cannot import name 'config_store'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/webapp/config_store.py
"""config.json -- values only, defaults merged on read, validated and atomic on write.

Values only, because the descriptions and types live in settings_schema.py and would
otherwise drift into two places. Atomic, because an interrupted save that truncated this file
would take the API keys with it.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from webapp.settings_schema import BY_KEY, SETTINGS, SettingType, validate_value

MASK = "●●●●"


class UnknownSetting(ValueError):
    """A key that is not in the catalogue. The catalogue is the whole surface."""


def load(path: Path) -> dict[str, str]:
    """Every catalogue key, stored value where there is one, default otherwise.

    Complete by construction so a caller building an environment never has to ask whether a
    key exists. Unknown keys already in the file are ignored rather than raising: a config
    written by a newer version must not stop an older one from starting.
    """
    stored: dict[str, str] = {}
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            stored = {k: str(v) for k, v in raw.items()}
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError):
        pass
    return {s.key: stored.get(s.key, s.default) for s in SETTINGS}


def save(path: Path, values: dict[str, str]) -> None:
    """Validate everything, then write atomically. Refuses unknown keys."""
    unknown = sorted(set(values) - set(BY_KEY))
    if unknown:
        raise UnknownSetting(f"not settings: {', '.join(unknown)}")

    # Validate the whole batch BEFORE touching the file, so a bad value cannot leave a
    # half-applied config behind.
    clean = {key: validate_value(BY_KEY[key], value) for key, value in values.items()}

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(clean, handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def redacted_values(values: dict[str, str]) -> dict[str, str]:
    """Values safe to send to a browser: secrets masked, everything else verbatim.

    An UNSET secret stays empty rather than masked -- showing dots for a key that was never
    configured would tell the operator it is set when it is not.
    """
    out = {}
    for key, value in values.items():
        spec = BY_KEY.get(key)
        if spec and spec.type is SettingType.SECRET and value:
            out[key] = MASK
        else:
            out[key] = value
    return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m pytest server/tests/test_config_store.py -v`
Expected: PASS, 10 tests.

- [ ] **Step 5: Commit**

```bash
git add server/webapp/config_store.py server/tests/test_config_store.py
git commit -m "Store settings in config.json, validated and written atomically"
```

---

### Task 3: The environment builder

**Files:**
- Create: `server/webapp/env_builder.py`
- Create: `server/tests/test_env_builder.py`

**Interfaces:**
- Consumes: `load` from Task 2; `BY_KEY` from Task 1.
- Produces: `build_env(values: dict[str, str], base: dict[str, str] | None = None) -> dict[str, str]`.

The rule that matters: **an empty value is omitted, not exported as an empty string.** `AIS_CACHE_FILE=""` in the environment is not the same as unset — the proxy reads it with `os.environ.get("AIS_CACHE_FILE", "")` and `.strip() or default`, so empty happens to be safe there, but `ANTHROPIC_API_KEY=""` would make the key look present and fail later with a confusing error rather than the clear "unset disables identification".

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_env_builder.py
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest server/tests/test_env_builder.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.env_builder'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/webapp/env_builder.py
"""Stored settings -> the environment a managed child process is started with."""
from __future__ import annotations

import os

from webapp.settings_schema import BY_KEY


def build_env(values: dict[str, str], base: dict[str, str] | None = None) -> dict[str, str]:
    """The parent environment, with every configured setting applied over it.

    An empty value REMOVES the variable rather than exporting an empty string. The two are
    not equivalent: `ANTHROPIC_API_KEY=""` looks present and fails later with a confusing
    error, where unset is documented to disable identification cleanly. Removing also means
    clearing a value in the UI takes effect even when the launching shell had one set.
    """
    env = dict(os.environ if base is None else base)
    for key, value in values.items():
        if key not in BY_KEY:
            continue
        if (value or "").strip():
            env[key] = value
        else:
            env.pop(key, None)
    return env
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m pytest server/tests/test_env_builder.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add server/webapp/env_builder.py server/tests/test_env_builder.py
git commit -m "Build a child process environment from stored settings"
```

---

### Task 4: Import the current values from start-all.bat

**Files:**
- Create: `server/webapp/import_batch.py`
- Create: `server/tests/test_import_batch.py`

**Interfaces:**
- Consumes: `BY_KEY`, `validate_value` from Task 1; `save` from Task 2.
- Produces: `parse_batch(text: str) -> dict[str, str]` and `import_into(batch_path: Path, config_path: Path) -> dict[str, str]`.

Only **active** `set` lines are imported. A commented `:: set X=off` line documents a rollback that is not currently applied; importing it would silently turn a feature off during migration.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_import_batch.py
"""One-time migration: read the values currently in start-all.bat into config.json."""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store  # noqa: E402
from webapp.import_batch import import_into, parse_batch  # noqa: E402

_SAMPLE = """\
@echo off
set ANTHROPIC_API_KEY=sk-ant-example
set STT_BACKEND=groq
:: set AIS_HINT_FILTER=off
::   set AIS_CACHE_FILE=%~dp0frozen.json
set AISHUB_BBOX=51.4,52.6,2.0,4.25
set SCRIPT_DIR=%~dp0
"""


def test_an_active_setting_is_imported():
    assert parse_batch(_SAMPLE)["STT_BACKEND"] == "groq"


def test_a_commented_rollback_is_not_imported():
    """`:: set AIS_HINT_FILTER=off` documents a rollback that is NOT currently applied.
    Importing it would silently turn a shipped fix off during the migration."""
    assert "AIS_HINT_FILTER" not in parse_batch(_SAMPLE)


def test_batch_plumbing_is_not_imported():
    """SCRIPT_DIR is how the .bat finds itself, not a setting anyone should see."""
    assert "SCRIPT_DIR" not in parse_batch(_SAMPLE)


def test_a_value_containing_commas_survives():
    assert parse_batch(_SAMPLE)["AISHUB_BBOX"] == "51.4,52.6,2.0,4.25"


def test_importing_writes_a_config_that_loads_back(tmp_path):
    batch = tmp_path / "start-all.bat"
    batch.write_text(_SAMPLE, encoding="utf-8")
    config = tmp_path / "config.json"
    import_into(batch, config)
    values = config_store.load(config)
    assert values["STT_BACKEND"] == "groq"
    assert values["ANTHROPIC_API_KEY"] == "sk-ant-example"
    # Not mentioned in the batch file, so it must come from the catalogue default.
    assert values["AIS_SUGGEST_N"] == "3"


def test_importing_the_real_batch_file_produces_a_valid_config(tmp_path):
    """The migration has to work on the actual file, not just a sample. This is the test
    that catches a value the schema rejects -- e.g. a bbox the validator will not accept."""
    real = _SERVER_DIR / "start-all.bat"
    if not real.exists():
        import pytest
        pytest.skip("start-all.bat is gitignored; present only on a configured machine")
    config = tmp_path / "config.json"
    values = import_into(real, config)
    assert values["STT_BACKEND"] in ("groq", "whisper_cpp")
    assert config_store.load(config)["AISHUB_BBOX"].count(",") == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest server/tests/test_import_batch.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'webapp.import_batch'`

- [ ] **Step 3: Write minimal implementation**

```python
# server/webapp/import_batch.py
"""One-time migration: read the values currently set in start-all.bat into config.json.

The batch file is INPUT ONLY and is never regenerated. Its prose comments are the source of
the catalogue's descriptions and are some of the best documentation in the project; a
round-trip through this module would destroy them. After the migration it stays on disk as a
read-only fallback.

This module is expected to be deleted once every deployment has run it once.
"""
from __future__ import annotations

import re
from pathlib import Path

from webapp import config_store
from webapp.settings_schema import BY_KEY, validate_value

# Active `set NAME=value` only, anchored at the start of the line. A commented line
# (`:: set X=off`) documents a rollback that is NOT currently applied, and importing it
# would silently turn a shipped fix off during the migration.
_SET_RE = re.compile(r"^set\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse_batch(text: str) -> dict[str, str]:
    """Every catalogue setting the batch file actively sets, in file order."""
    found: dict[str, str] = {}
    for line in text.splitlines():
        match = _SET_RE.match(line.strip("\ufeff"))
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        # Keys outside the catalogue are batch plumbing (SCRIPT_DIR, PROXY_SCRIPT).
        if key in BY_KEY:
            found[key] = value
    return found


def import_into(batch_path: Path, config_path: Path) -> dict[str, str]:
    """Write a config.json from the batch file's current values plus catalogue defaults.

    Validates as it goes, so a value the schema rejects fails here -- during a migration a
    human is watching -- rather than at the next restart.
    """
    text = Path(batch_path).read_text(encoding="utf-8", errors="replace")
    imported = parse_batch(text)

    values = {spec.key: spec.default for spec in BY_KEY.values()}
    for key, raw in imported.items():
        values[key] = validate_value(BY_KEY[key], raw)

    config_store.save(config_path, values)
    return values
```

- [ ] **Step 4: Run test to verify it passes**

Run: `py -m pytest server/tests/test_import_batch.py -v`
Expected: PASS, 6 tests (the last skips if `start-all.bat` is absent).

- [ ] **Step 5: Commit**

```bash
git add server/webapp/import_batch.py server/tests/test_import_batch.py
git commit -m "Import current values out of start-all.bat, once"
```

---

### Task 5: Prove the built environment actually runs the proxy

**Files:**
- Create: `server/tests/test_settings_end_to_end.py`

**Interfaces:**
- Consumes: everything from Tasks 1–4.
- Produces: nothing. This is the gate on the whole phase.

The phase's claim is "a proxy started from this env dict behaves exactly as it does today". Every preceding task tests a part; this tests the claim. It launches the real proxy as a subprocess with a built environment, waits for it to answer, and stops it — so a typo in a key name, a value the proxy parses differently, or an omission that changes behaviour fails here rather than in production.

- [ ] **Step 1: Write the failing test**

```python
# server/tests/test_settings_end_to_end.py
"""The phase's actual claim: a proxy started from a built environment behaves as it does now.

Everything else tests a part. This starts the real thing.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store  # noqa: E402
from webapp.env_builder import build_env  # noqa: E402
from webapp.import_batch import import_into  # noqa: E402


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.skipif(not (_SERVER_DIR / "start-all.bat").exists(),
                    reason="start-all.bat is gitignored; present only on a configured machine")
def test_a_proxy_started_from_the_built_environment_serves_requests(tmp_path):
    config = tmp_path / "config.json"
    import_into(_SERVER_DIR / "start-all.bat", config)

    values = config_store.load(config)
    port = _free_port()
    values["PROXY_PORT"] = str(port)
    # Keep the test off the live cache and off the network feed.
    values["AIS_SOURCE"] = "aishub"
    values["AISHUB_USERNAME"] = ""

    env = build_env(values)
    child = subprocess.Popen(
        [sys.executable, str(_SERVER_DIR / "whisper-proxy.py")],
        cwd=str(_SERVER_DIR), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.time() + 30
        body = None
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/conversations",
                                            timeout=2) as response:
                    body = response.read()
                break
            except Exception:
                time.sleep(0.5)
        assert body is not None, "proxy never answered on the configured port"
        json.loads(body)          # a real response, not an error page
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()


@pytest.mark.skipif(not (_SERVER_DIR / "start-all.bat").exists(),
                    reason="start-all.bat is gitignored; present only on a configured machine")
def test_the_built_environment_matches_what_the_batch_file_sets(tmp_path):
    """Catches the quiet failure: a setting renamed or dropped in the catalogue, so the proxy
    silently falls back to a code default that differs from what the operator had."""
    from webapp.import_batch import parse_batch
    config = tmp_path / "config.json"
    import_into(_SERVER_DIR / "start-all.bat", config)
    env = build_env(config_store.load(config), base={})
    for key, value in parse_batch((_SERVER_DIR / "start-all.bat")
                                  .read_text(encoding="utf-8", errors="replace")).items():
        if value.strip():
            assert env.get(key) == value, f"{key} did not survive the round trip"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `py -m pytest server/tests/test_settings_end_to_end.py -v`
Expected: on a configured machine, FAIL or ERROR until Tasks 1–4 are complete. If Tasks 1–4 are already done it may pass immediately — in that case verify it can fail by temporarily renaming a key in `SETTINGS` and re-running; it must report "did not survive the round trip". Restore the key afterwards.

- [ ] **Step 3: No implementation needed**

This task adds no production code. If it fails, the fix belongs in Tasks 1–4.

- [ ] **Step 4: Run the whole suite**

Run: `py -m pytest server/tests -q`
Expected: PASS. The pre-existing count is 809; this phase adds 32, so expect 841 passed, 1 xfailed.

- [ ] **Step 5: Commit**

```bash
git add server/tests/test_settings_end_to_end.py
git commit -m "Prove the proxy runs from a built environment"
```

---

## Self-review notes

**Spec coverage for Phase 1.** Section 2's `config.json`-plus-pydantic-schema is Tasks 1–2; the description-carries-the-prose requirement is tested in Task 1; the 22-plus-5 scope decision is Task 1's catalogue and its exclusion test; the one-time import with the batch file kept read-only is Task 4; atomic writes are Task 2.

**Deliberately deferred to later phases, not forgotten:**
- **The Paths group** (SDR# install, captures directory, log directory, AIS station host/port). Section 2 calls for it, but none of those paths is read from the environment by the proxy today — they are arguments to processes that Phase 2 introduces. Adding path settings now would mean settings nothing consumes. They belong in the Phase 2 plan, alongside the registry entries that use them.
- **`GET /api/health` reporting which paths resolve** — same reason; there is no web framework in this phase.
- Auth, the supervisor, and every UI tab are Phases 2–3 as the spec's build order states.

**Type consistency check.** `validate_value(spec, raw) -> str` is used with that signature in Tasks 1, 2 and 4. `load`/`save` take `Path` first in Tasks 2, 4 and 5. `build_env(values, base=None)` is called with the `base=` keyword in every test and with a single argument in Task 5.
