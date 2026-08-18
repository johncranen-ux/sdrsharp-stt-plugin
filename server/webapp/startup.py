"""Reading the config, refusing an unsafe bind, and handing the app to uvicorn."""
from __future__ import annotations

import sys
from pathlib import Path

from webapp import config_store, credentials
from webapp.app import create_app
from webapp.auth import UnsafeBind, check_bind_allowed

SERVER_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = SERVER_DIR / "config.json"
CREDENTIALS_PATH = SERVER_DIR / "credentials.json"


def build(server_dir: Path = SERVER_DIR, config_path: Path = CONFIG_PATH,
          credentials_path: Path = CREDENTIALS_PATH):
    """(app, host, port), after the bind guard has had its say."""
    values = config_store.load(config_path)
    host = (values.get("WEBAPP_BIND_HOST") or "127.0.0.1").strip()
    port = int((values.get("WEBAPP_PORT") or "8787").strip())
    check_bind_allowed(host, credentials.has_password(credentials_path))
    return create_app(server_dir=server_dir, config_path=config_path,
                      credentials_path=credentials_path), host, port


def main() -> int:
    try:
        app, host, port = build()
    except UnsafeBind as exc:
        print(f"[control panel] {exc}", file=sys.stderr)
        return 2
    # Imported here rather than at module scope so that `build()` -- which the tests use --
    # does not pay for uvicorn's import, and so a broken uvicorn cannot break the bind guard.
    import uvicorn

    print(f"[control panel] http://{host}:{port}", flush=True)
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0
