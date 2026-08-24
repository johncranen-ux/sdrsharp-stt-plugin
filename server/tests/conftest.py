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


@pytest.fixture(autouse=True)
def _never_read_the_real_captures(monkeypatch):
    """Refuse to serve audio out of the operator's real capture directory.

    Same reasoning as the supervisor guard above. CAPTURES_DIR's catalogue default is a real
    path on the operator's machine, and a test that builds an app over a config file which does
    not exist gets every setting's default -- so a route test would happily read, and return,
    an hour of recorded radio traffic. That directory is 1.5 GB of received transmissions and
    is gitignored for legal reasons (NL Telecommunicatiewet 18.13); no test may touch it.
    """
    from webapp import clips as clips_module
    from webapp.settings_schema import BY_KEY

    real = Path(BY_KEY["CAPTURES_DIR"].default).resolve()
    original = clips_module.clip_path

    def guarded_clip_path(captures_root, day, clip):
        if captures_root is not None and Path(captures_root).resolve() == real:
            raise AssertionError(
                f"this test read the real capture directory {real} -- 1.5 GB of received radio "
                f"traffic. Give the app a config whose CAPTURES_DIR is under tmp_path.")
        return original(captures_root, day, clip)

    monkeypatch.setattr(clips_module, "clip_path", guarded_clip_path)


@pytest.fixture(autouse=True)
def _never_open_the_real_archive(monkeypatch):
    """Refuse to open the operator's real conversation archive.

    Same reasoning as the two guards above. CONVERSATIONS_DB's default is a real path, and a
    test that builds an app over a config file which does not exist gets every setting's
    default -- so a route test posting a comment would write a verdict into the ground truth
    the identification benchmark is scored against. Unlike a killed process, that corruption
    would be silent and might not be noticed for weeks.
    """
    import conversation_archive

    real = conversation_archive.default_db_path(_SERVER_DIR).resolve()
    original = conversation_archive.connect

    def guarded_connect(path):
        if Path(path).resolve() == real:
            raise AssertionError(
                f"this test opened {real} -- the real one, holding the operator's conversation "
                f"archive and every ground-truth verdict recorded in the panel. Give the app a "
                f"config whose CONVERSATIONS_DB is under tmp_path.")
        return original(path)

    monkeypatch.setattr(conversation_archive, "connect", guarded_connect)


@pytest.fixture(autouse=True)
def _archive_resolved_conversations_under_tmp_path(monkeypatch, tmp_path):
    """Point stt_proxy.conversations' archive writes at a throwaway database.

    The guard above makes reaching the real archive an AssertionError, but _archive_rows (in
    stt_proxy/conversations.py) swallows every exception and only logs it -- correctly, since
    live transcription must survive a broken disk. Left alone, the nine _store_resolved /
    _resolve_window tests in test_whisper_proxy.py would each trip the guard silently and print
    "[conv] could not archive to ... the real one" on every run. Redirecting CONVERSATIONS_DB
    keeps their archive behaviour real -- rows still get written, just nowhere that matters --
    and keeps the suite's output clean.
    """
    from stt_proxy import conversations as conversations_module

    monkeypatch.setattr(conversations_module, "CONVERSATIONS_DB", str(tmp_path / "conversations.db"))
