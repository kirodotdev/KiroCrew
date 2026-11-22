"""A running app backend can read the gateway's secret files.

The external Crew Tunnel app reads ``run/gateway-<port>.secret`` (then
``.local_secret``) and mints an owner token from ``/api/token/local`` to proxy the
dashboard. With those files masked in the backend's sandbox, every tunnel start fails
with ``token mint failed: 403 Forbidden``. The dashboard-token-auth spec, "App backends
mint through the local secret", has the contract and its history.

Only the arguments of the spawn are checked; no sandbox and no process runs here.
"""

from __future__ import annotations

import json
from pathlib import Path

from app_backend_helpers import app_env_fixture  # noqa: F401

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app

_SECRET_NAMES = (
    ".local_secret",
    "run/gateway-8765.secret",
    "run/gateway-8765-127.0.0.1.secret",
)


def test_the_backend_spawn_hides_no_gateway_secret_file(app_env, tmp_path, monkeypatch):
    import kiro_crew.apps.backend as bmod

    (app_env / "run").mkdir(exist_ok=True)
    for name in _SECRET_NAMES:
        (app_env / name).write_text("x", encoding="utf-8")

    seen: dict = {}

    def _spy_wrap(argv, **kwargs):
        seen["hidden"] = tuple(str(p) for p in kwargs.get("extra_hidden_dirs") or ())
        return (list(argv), None)

    monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
    monkeypatch.setattr(
        bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
    )

    src = tmp_path / "source" / "tunnel-like"
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": "tunnel-like",
                "version": "1.0.0",
                "displayName": "Tunnel-like",
                "description": "a backend that mints a local token",
                "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
            }
        )
    )
    (src / "server.py").write_text("import time\ntime.sleep(30)\n")
    install_app(src)

    bmod.start_app_backend("tunnel-like")

    assert "hidden" in seen, "the backend never reached the sandbox wrap"
    for name in _SECRET_NAMES:
        path = str(Path(app_env) / name)
        assert path not in seen["hidden"], f"{name} is hidden from the running backend"
