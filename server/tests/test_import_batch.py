"""One-time migration: read the values currently in start-all.bat into config.json."""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

import pytest  # noqa: E402

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
        pytest.skip("start-all.bat is gitignored; present only on a configured machine")
    config = tmp_path / "config.json"
    values = import_into(real, config)
    assert values["STT_BACKEND"] in ("groq", "whisper_cpp")
    assert config_store.load(config)["AISHUB_BBOX"].count(",") == 3


@pytest.mark.parametrize("line", [
    "SET STT_BACKEND=groq",
    "Set STT_BACKEND=groq",
    "  set STT_BACKEND=groq",
    "\tset STT_BACKEND=groq",
])
def test_cmd_honours_these_so_the_importer_must_too(line):
    """cmd.exe treats SET, Set and set identically and allows leading whitespace. Dropping
    such a line is silent and looks exactly like a correctly-excluded comment."""
    assert parse_batch(line)["STT_BACKEND"] == "groq"


@pytest.mark.parametrize("line", [
    ":: set STT_BACKEND=groq",
    "  :: set STT_BACKEND=groq",
    "REM set STT_BACKEND=groq",
    "rem set STT_BACKEND=groq",
])
def test_a_commented_line_stays_excluded_however_it_is_written(line):
    """The guard that matters: relaxing the anchor must not start importing rollbacks."""
    assert parse_batch(line) == {}


def test_a_value_containing_equals_survives():
    """The capture group must take the whole remainder of the line, not stop at the first =."""
    assert parse_batch("set GROQ_MODEL=whisper=large=v3")["GROQ_MODEL"] == "whisper=large=v3"
