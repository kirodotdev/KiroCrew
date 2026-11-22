"""A running app backend cannot read the gateway's secret files, unless it holds the grant.

Reverses ``test_app_backend_keeps_gateway_secret_readable.py``. The running backend's spawn
hides ``.local_secret`` and every ``run/gateway-<port>*.secret`` again. An app the owner
granted the ``dashboard_proxy`` capability gets, over each of those paths, a stand-in file
holding its own derived value ``D(app)`` (the dashboard-token-auth spec, "App backends
mint through a delegated value"); an app without the grant sees the empty stand-in.

Only the arguments of the spawn are checked; no sandbox and no process runs here. The real
namespace sandbox is exercised in ``test_app_backend_dashboard_proxy_real_sandbox.py``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from app_backend_helpers import app_env_fixture  # noqa: F401

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app

_SECRET_NAMES = (
    ".local_secret",
    "run/gateway-8765.secret",
    "run/gateway-8765-127.0.0.1.secret",
)
_APP = "tunnel-like"


def _install(tmp_path) -> None:
    src = tmp_path / "source" / _APP
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": _APP,
                "version": "1.0.0",
                "displayName": "Tunnel-like",
                "description": "a backend that mints a local token",
                "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
            }
        )
    )
    (src / "server.py").write_text("import time\ntime.sleep(30)\n")
    install_app(src)


def _spy_spawn(monkeypatch) -> dict:
    import kiro_crew.apps.backend as bmod

    seen: dict = {}

    def _spy_wrap(argv, **kwargs):
        seen["hidden"] = tuple(str(p) for p in kwargs.get("extra_hidden_dirs") or ())
        seen["stand_ins"] = tuple(kwargs.get("extra_stand_ins") or ())
        return (list(argv), None)

    monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
    monkeypatch.setattr(
        bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
    )
    return seen


def _write_secret_files(app_env) -> None:
    (app_env / "run").mkdir(exist_ok=True)
    for name in _SECRET_NAMES:
        (app_env / name).write_text("x", encoding="utf-8")


def test_the_running_backend_spawn_hides_every_gateway_secret_file(app_env, tmp_path, monkeypatch):
    import kiro_crew.apps.backend as bmod

    _write_secret_files(app_env)
    seen = _spy_spawn(monkeypatch)
    _install(tmp_path)

    bmod.start_app_backend(_APP)

    assert "hidden" in seen, "the backend never reached the sandbox wrap"
    for name in _SECRET_NAMES:
        path = str(Path(app_env) / name)
        assert path in seen["hidden"], f"{name} is readable by the running backend"


def test_an_app_without_the_grant_gets_the_empty_stand_in(app_env, tmp_path, monkeypatch):
    import kiro_crew.apps.backend as bmod

    _write_secret_files(app_env)
    seen = _spy_spawn(monkeypatch)
    _install(tmp_path)

    bmod.start_app_backend(_APP)

    assert seen["stand_ins"] == (), "an ungranted app was given stand-in content"


def test_a_granted_app_gets_its_derived_value_over_each_masked_path(app_env, tmp_path, monkeypatch):
    import kiro_crew.apps.backend as bmod
    from kiro_crew.apps import dashboard_proxy
    from kiro_crew.dashboard import boot_id

    _write_secret_files(app_env)
    seen = _spy_spawn(monkeypatch)
    _install(tmp_path)
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    monkeypatch.setattr(dashboard_proxy, "registry_verified", lambda _name: True)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, True))
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: True)
    assert dashboard_proxy.grant(_APP)[0] is True

    bmod.start_app_backend(_APP)

    install_id = dashboard_proxy.install_identity(_APP)
    expected = hmac.new(
        b"gateway-secret-in-words",
        b"dashboard-proxy\0"
        + _APP.encode()
        + b"\0"
        + install_id.encode()
        + b"\0"
        + boot_id.current_boot_id().encode(),
        hashlib.sha256,
    ).hexdigest()
    assert seen["stand_ins"], "a granted app was given no stand-in"
    masked = {masked_path for masked_path, _source in seen["stand_ins"]}
    for name in _SECRET_NAMES:
        assert str(Path(app_env) / name) in masked
    for _masked_path, source in seen["stand_ins"]:
        assert Path(source).read_text(encoding="utf-8") == expected


def test_a_second_app_does_not_get_the_first_apps_value(app_env, tmp_path, monkeypatch):
    from kiro_crew.apps import dashboard_proxy

    _install(tmp_path)
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    first = dashboard_proxy.stand_in_value(_APP)
    monkeypatch.setattr(dashboard_proxy, "registry_verified", lambda _name: True)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, True))
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: True)
    dashboard_proxy.grant(_APP)
    first = dashboard_proxy.stand_in_value(_APP)
    assert first and dashboard_proxy.stand_in_value("another-app") != first


@pytest.mark.parametrize("what", ["disable", "uninstall"])
def test_the_grant_ends_with_the_app(app_env, tmp_path, monkeypatch, what):
    from kiro_crew.apps import dashboard_proxy
    from kiro_crew.apps.manager import disable_app, uninstall_app

    _install(tmp_path)
    dashboard_proxy.set_gateway_secret("gateway-secret-in-words")
    monkeypatch.setattr(dashboard_proxy, "registry_verified", lambda _name: True)
    monkeypatch.setattr(dashboard_proxy, "_marker_state", lambda _name: (True, True))
    monkeypatch.setattr(dashboard_proxy, "_namespace_backend", lambda: True)
    dashboard_proxy.grant(_APP)
    assert dashboard_proxy.granted(_APP)

    (disable_app if what == "disable" else uninstall_app)(_APP)

    assert not dashboard_proxy.granted(_APP)
    assert dashboard_proxy.stand_in_value(_APP) is None
