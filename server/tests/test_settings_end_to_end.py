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
