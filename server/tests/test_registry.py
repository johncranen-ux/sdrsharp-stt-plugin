"""The managed-process catalogue: what gets started, with which command line, from config."""
import sys
from pathlib import Path

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

from webapp import config_store, registry  # noqa: E402


def _values(**overrides) -> dict[str, str]:
    values = config_store.load(Path("does-not-exist.json"))   # every key at its default
    values.update(overrides)
    return values


def test_the_proxy_is_started_by_running_whisper_proxy_with_this_interpreter():
    paths = registry.resolve_paths(_values(), _SERVER_DIR)
    argv = registry.argv_for(registry.BY_NAME["proxy"], _values(), paths)
    assert argv[0] == sys.executable
    assert Path(argv[1]).name == "whisper-proxy.py"
    assert Path(argv[1]).exists()


def test_the_proxy_is_never_started_through_the_batch_file():
    """Proven on 2026-08-18: `start` needs an interactive window station, so start-all.bat
    cannot be launched from a service or from a detached parent."""
    paths = registry.resolve_paths(_values(), _SERVER_DIR)
    for spec in registry.PROCESSES:
        argv = registry.argv_for(spec, _values(), paths)
        joined = " ".join(argv).lower()
        assert "start-all" not in joined
        assert "cmd" not in Path(argv[0]).name.lower()


def test_the_counter_is_pointed_at_the_configured_station():
    values = _values(AIS_STATION_HOST="10.0.0.5", AIS_STATION_HTTP_PORT="8200",
                     AIS_STATION_NMEA_PORT="10222")
    paths = registry.resolve_paths(values, _SERVER_DIR)
    argv = registry.argv_for(registry.BY_NAME["counter"], values, paths)
    assert "--station" in argv and "10.0.0.5:8200" in argv
    assert argv[argv.index("--port") + 1] == "10222"
    assert Path(argv[argv.index("--log") + 1]).parent == paths.log_dir


def test_both_processes_declare_the_port_they_must_own():
    """The counter listens too -- AIS-catcher is pointed at it and pushes. Declaring no port
    for it on 2026-08-18 meant no port was cleared before a start, and since
    ais_station_count.py sets SO_REUSEADDR a second counter bound alongside a hand-started
    one and silently took the station's connection over."""
    assert registry.port_for(registry.BY_NAME["proxy"], _values(PROXY_PORT="9001")) == 9001
    assert registry.port_for(registry.BY_NAME["counter"],
                             _values(AIS_STATION_NMEA_PORT="10222")) == 10222


def test_the_counters_declared_port_is_the_one_its_command_line_listens_on():
    """The two must agree, or clearing frees a port the child then does not take."""
    values = _values(AIS_STATION_NMEA_PORT="10222")
    paths = registry.resolve_paths(values, _SERVER_DIR)
    argv = registry.argv_for(registry.BY_NAME["counter"], values, paths)
    listening_on = argv[argv.index("--port") + 1]
    assert int(listening_on) == registry.port_for(registry.BY_NAME["counter"], values)


def test_a_disabled_process_reports_itself_disabled():
    assert registry.is_enabled(registry.BY_NAME["counter"], _values(COUNTER_ENABLED="on"))
    assert not registry.is_enabled(registry.BY_NAME["counter"], _values(COUNTER_ENABLED="off"))


def test_the_child_environment_carries_the_secrets_and_not_the_app_settings():
    env = registry.env_for(registry.BY_NAME["proxy"],
                           _values(GROQ_API_KEY="gsk_test", WEBAPP_BIND_HOST="0.0.0.0"))
    assert env["GROQ_API_KEY"] == "gsk_test"
    assert "WEBAPP_BIND_HOST" not in env
    assert env["PYTHONUNBUFFERED"] == "1"


def test_the_log_directory_defaults_to_server_logs():
    paths = registry.resolve_paths(_values(), _SERVER_DIR)
    assert paths.log_dir == _SERVER_DIR / "logs"


def test_an_explicit_log_directory_is_honoured(tmp_path):
    paths = registry.resolve_paths(_values(LOG_DIR=str(tmp_path)), _SERVER_DIR)
    assert paths.log_dir == tmp_path
