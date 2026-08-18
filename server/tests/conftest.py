"""Stop a test ever managing the live processes.

On 2026-08-18 a test built a control-panel app over a config file that did not exist. Every
setting fell back to its default -- LOG_DIR to the real server/logs, PROXY_PORT to 9000 -- so
the app got a REAL supervisor pointed at the real pid file. The test then exercised the stop
route with a valid CSRF token, which is a correct thing to test, and killed the proxy that was
carrying live radio traffic. The plugin logged "target machine actively refused it" for the
next twenty minutes.

The lesson is not "remember to pass a config". It is that a test suite which can reach the
production pid file will eventually use it, so this makes the reach itself impossible: any
Supervisor built during a test must live under tmp_path.
"""
import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_SERVER_DIR))

REAL_LOG_DIR = (_SERVER_DIR / "logs").resolve()


@pytest.fixture(autouse=True)
def _never_manage_live_processes(monkeypatch):
    """Refuse, loudly, to construct a Supervisor over the real log directory."""
    from webapp import supervisor as supervisor_module

    original_init = supervisor_module.Supervisor.__init__

    def guarded_init(self, paths, load_values):
        if Path(paths.log_dir).resolve() == REAL_LOG_DIR:
            raise AssertionError(
                f"this test built a Supervisor over {REAL_LOG_DIR} -- the real one, holding the "
                f"pid files of the running proxy and counter. Give the app a config whose "
                f"LOG_DIR is under tmp_path, or pass a fake supervisor. On 2026-08-18 this "
                f"exact reach killed a live capture.")
        original_init(self, paths, load_values)

    monkeypatch.setattr(supervisor_module.Supervisor, "__init__", guarded_init)
