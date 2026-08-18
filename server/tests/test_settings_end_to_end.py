"""The phase's actual claim: a proxy started from a built environment behaves as it does now.

Everything else tests a part. This starts the real thing.

Environment hazards this test must stay clear of:
  - A live production proxy runs on port 9000 serving real radio traffic right now. The child
    here must never bind that port, so it always gets a free one from `_free_port()`.
  - The proxy reads and writes a shared AIS vessel cache on disk. The child is pointed at a
    cache file inside `tmp_path` so it can never write over the live cache.
  - The child must never reach the network or spend the operator's API quota, so every secret
    that would enable an outbound call (Anthropic, Groq, OpenRouter, aisstream x2, AISHub) is
    cleared before the environment is built.
"""
import json
import re
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

# Values that would let the child reach the network or spend the operator's quota. Cleared
# before the environment is built so the proxy starts with those features disabled, which is
# documented behaviour, rather than actually calling out.
_NETWORK_SECRETS = (
    "ANTHROPIC_API_KEY",
    "GROQ_API_KEY",
    "OPENROUTER_API_KEY",
    "AISSTREAM_API_KEY",
    "AISSTREAM_API_KEY2",
    "AISHUB_USERNAME",
)


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
    values["AIS_CACHE_FILE"] = str(tmp_path / "test-ais-cache.json")
    for key in _NETWORK_SECRETS:
        values[key] = ""

    env = build_env(values)
    # NOTE: CONVERSATIONS_FILE and VESSELS_LOG_FILE are hardcoded in the proxy with no env
    # override, so unlike AIS_CACHE_FILE this child cannot be pointed away from the live
    # files. It is safe only incidentally: a proxy that receives no audio never mutates the
    # store it loaded, and terminate() on Windows does not run atexit handlers. Making those
    # paths configurable belongs to the phase 2 Paths group.
    #
    # stdout goes to a file rather than subprocess.PIPE: an undrained pipe blocks a child that
    # logs enough at startup before it ever binds the port, and the failure would report
    # "proxy never answered" -- pointing at the wrong subsystem entirely. A file in tmp_path
    # keeps the diagnostics that DEVNULL would have discarded.
    log_path = tmp_path / "proxy.log"
    with open(log_path, "w", encoding="utf-8") as log:
        child = subprocess.Popen(
            [sys.executable, str(_SERVER_DIR / "whisper-proxy.py")],
            cwd=str(_SERVER_DIR), env=env,
            stdout=log, stderr=subprocess.STDOUT)
    try:
        deadline = time.time() + 30
        body = None
        while time.time() < deadline:
            if child.poll() is not None:
                # Notice a dead child instead of waiting out the full 30 s for it.
                pytest.fail(f"the proxy exited early with code {child.returncode}")
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
    # utf-8-sig, matching the importer: errors="replace" would corrupt the EXPECTED side of
    # this comparison and report a false failure, rather than tolerate a genuinely bad file.
    parsed = parse_batch((_SERVER_DIR / "start-all.bat").read_text(encoding="utf-8-sig"))
    assert len(parsed) >= 11, "the set-line parser matched nothing; the guard below is vacuous"
    for key, value in parsed.items():
        # pytest.fail, not a bare assert: several of these values are live API keys, and a
        # bare assert has pytest rewrite both operands' repr() into the failure output --
        # exactly the scenario this test exists to catch would leak the secret into the log.
        if value.strip() and env.get(key) != value:
            pytest.fail(f"{key} did not survive the round trip")


# Plumbing the batch file uses to find itself -- not settings, and correctly never catalogued.
_BATCH_PLUMBING = {"SCRIPT_DIR", "PROXY_SCRIPT"}
# Deliberately NOT import_batch.parse_batch: that filters through BY_KEY, the very catalogue
# this test polices, so a renamed key would drop out of the expected and actual sides together
# and the assertion would pass over its own blind spot. Scanning the file independently also
# catches a NEW set line that was never catalogued, which build_env silently drops.
# Also deliberately NOT character-for-character the importer's _SET_RE (which would inherit
# its blind spots): this one additionally tolerates the `set "NAME=value"` quoting form.
_SET_LINE = re.compile(r'^set\s+"?([A-Za-z_]\w*)=(.*)$', re.IGNORECASE)


def _keys_the_launcher_sets(batch_path: Path) -> set[str]:
    found = set()
    for line in batch_path.read_text(encoding="utf-8-sig").splitlines():
        match = _SET_LINE.match(line.strip())
        if match and match.group(2).strip():
            found.add(match.group(1).upper())
    return found - _BATCH_PLUMBING


@pytest.mark.skipif(not (_SERVER_DIR / "start-all.bat").exists(),
                    reason="start-all.bat is gitignored; present only on a configured machine")
def test_every_setting_the_launcher_configures_reaches_the_child(tmp_path):
    """Catalogue-independent. Catches BOTH a renamed/dropped catalogue key -- whose configured
    value would silently revert to a code default the operator never chose -- and a new set
    line in start-all.bat that was never catalogued, which build_env drops without a word."""
    config = tmp_path / "config.json"
    import_into(_SERVER_DIR / "start-all.bat", config)
    env = build_env(config_store.load(config), base={})
    launcher_keys = _keys_the_launcher_sets(_SERVER_DIR / "start-all.bat")
    assert len(launcher_keys) >= 11, \
        "the set-line parser matched nothing; the guard below is vacuous"
    missing = sorted(launcher_keys - set(env))
    assert missing == [], f"configured settings that never reached the environment: {missing}"
