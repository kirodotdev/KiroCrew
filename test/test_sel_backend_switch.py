"""A spawned app backend holds no SEL MAC key: ``sel()`` and the spawn mask.

* ``sel()`` returns the gateway-route client inside a spawned app backend, so no
  backend loads ``trust/sel_hmac.key``; anywhere else it is the real log;
* ``trust/sel_hmac.key`` is in the spawn mask of EVERY app backend, builtin and
  not, whatever the child's own environment says;
* a ``critical=True`` write that the gateway did not take raises, so the action is
  refused.

The route and the client end to end are in ``test_sel_routes.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from app_backend_helpers import app_env_fixture  # noqa: F401

from kiro_crew.apps.manager import APP_MANIFEST_FILENAME, install_app
from kiro_crew.sel import sel, sel_hmac_key_path

_SPAWN_ENV = {
    "KIROCREW_SPAWNED": "1",
    "KIROCREW_APP_NAME": "switch-app",
    "KIROCREW_GATEWAY_ORIGIN": "http://127.0.0.1:1",
    "KIROCREW_PROXY_SECRET": "per-app-secret",
}


@pytest.fixture
def key_holder(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Replaces ``SecurityEventLog``; any entry means the process touched the key."""
    calls: list[str] = []

    class _KeyHolder:
        def __init__(self) -> None:
            calls.append("built")

        def log_api_access(self, **_kw: Any) -> None:
            calls.append("wrote")

    monkeypatch.setattr("kiro_crew.sel.SecurityEventLog", _KeyHolder)
    return calls


def test_a_spawned_backend_gets_the_gateway_client_and_never_the_key(
    monkeypatch, key_holder
) -> None:
    for k, v in _SPAWN_ENV.items():
        monkeypatch.setenv(k, v)
    assert type(sel()).__name__ == "SelClient"
    assert key_holder == []


@pytest.mark.parametrize("missing", ["KIROCREW_SPAWNED", "KIROCREW_APP_NAME"])
def test_outside_a_spawned_app_backend_sel_is_the_real_log(
    monkeypatch, key_holder, missing
) -> None:
    for k, v in _SPAWN_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(missing)
    assert type(sel()).__name__ != "SelClient"
    assert key_holder == ["built"]


def test_a_critical_write_the_gateway_did_not_take_refuses_the_action(
    monkeypatch, key_holder
) -> None:
    for k, v in _SPAWN_ENV.items():
        monkeypatch.setenv(k, v)  # the origin names a port nothing listens on
    with pytest.raises(Exception, match="gateway"):
        sel().log_api_access(operation="x.y", outcome="ok", critical=True)
    assert key_holder == []


def _spy_spawn(bmod: Any, monkeypatch: pytest.MonkeyPatch, *, route: bool = True) -> dict[str, Any]:
    """Spy the spawn. ``route`` gives the backend the origin and secret the mask needs."""
    seen: dict[str, Any] = {}
    if route:
        monkeypatch.setenv("KIROCREW_BOUND_PORT", "8123")
        monkeypatch.delenv("KIROCREW_BOUND_HOST", raising=False)
        monkeypatch.setattr(
            "kiro_crew.dashboard.token_auth.replace_legacy_app_secret", lambda _n: "per-app-secret"
        )
        # An enabled app holds the owner-enable marker the app token needs.
        monkeypatch.setattr("kiro_crew.apps.host_exec_grants.host_exec_granted", lambda _n: True)

    def _spy_wrap(argv, **kwargs):
        seen["hidden"] = kwargs.get("extra_hidden_dirs") or ()
        seen["visible"] = kwargs.get("extra_visible_dirs") or ()
        return (list(argv), None)

    monkeypatch.setattr(bmod, "wrap_argv", _spy_wrap)
    monkeypatch.setattr(
        bmod.subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("stop"))
    )
    return seen


def _install_plain(tmp_path: Path, name: str) -> None:
    src = tmp_path / "source" / name
    src.mkdir(parents=True)
    (src / APP_MANIFEST_FILENAME).write_text(
        json.dumps(
            {
                "name": name,
                "version": "1.0.0",
                "displayName": name,
                "description": "spawn mask",
                "backend": {"entryPoint": "server.py", "healthCheck": "/health"},
            }
        )
    )
    (src / "server.py").write_text("import time\ntime.sleep(30)\n")
    install_app(src)


def test_a_non_builtin_spawn_hides_the_key(app_env, tmp_path, monkeypatch) -> None:
    import kiro_crew.apps.backend as bmod

    seen = _spy_spawn(bmod, monkeypatch)
    _install_plain(tmp_path, "plain-app")
    bmod.start_app_backend("plain-app")
    assert str(sel_hmac_key_path()) in seen["hidden"]


@pytest.mark.parametrize("builtin", ["file-explorer", "md-notebook"])
def test_a_builtin_spawn_hides_the_key_and_no_carveout_lifts_it(
    app_env, monkeypatch, builtin
) -> None:
    import kiro_crew.apps.backend as bmod
    from kiro_crew import md_notebook_state as mns
    from kiro_crew import older_sandboxes
    from kiro_crew.apps.manager import register_builtin_apps

    register_builtin_apps()
    monkeypatch.setattr(older_sandboxes, "sandboxes_masking_only", lambda *a, **k: None)
    monkeypatch.setattr(mns, "older_sandboxes_masking_only_the_old_dir", lambda: [])
    seen = _spy_spawn(bmod, monkeypatch)
    bmod.start_app_backend(builtin)
    key = sel_hmac_key_path()
    assert str(key) in seen["hidden"]
    # A visible path that contains the key cancels its mask (sandbox_plan).
    for visible in seen["visible"]:
        assert key != Path(visible) and Path(visible) not in key.parents


def test_the_mask_does_not_depend_on_the_marker_the_child_can_clear(
    app_env, tmp_path, monkeypatch
) -> None:
    import kiro_crew.apps.backend as bmod

    seen = _spy_spawn(bmod, monkeypatch)
    _install_plain(tmp_path, "plain-app")
    monkeypatch.delenv("KIROCREW_SPAWNED", raising=False)
    monkeypatch.delenv("KIROCREW_APP_NAME", raising=False)
    bmod.start_app_backend("plain-app")
    assert str(sel_hmac_key_path()) in seen["hidden"]
