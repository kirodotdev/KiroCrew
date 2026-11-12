"""An app whose backend runs only in the gateway process (``backend.hooks``, no spawn).

Such an app never starts a backend, so the spawn is not where its secret can be
moved out of an older version's folder file, and no ``KIROCREW_PROXY_SECRET`` ever
reaches it. Loading its hooks moves the secret, and its context mints its app tokens
(``AppContext.app_token``) with no secret at all.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any
from unittest import mock

import pytest
from app_manager_helpers import app_home_fixture  # noqa: F401 - fixture
from app_manager_helpers import _make_app_source

import kiro_crew.apps.hooks_integration as hooks_mod
from kiro_crew import member_records
from kiro_crew.apps import manager_records, protected_app_state
from kiro_crew.apps.lifecycle import LifecycleDispatcher
from kiro_crew.apps.manager import disable_app, enable_app, get_app, install_app, update_app
from kiro_crew.dashboard import token_auth_tokens
from kiro_crew.dashboard.token_auth import (
    read_app_secret,
    validate_app_secret,
    validate_token_with_app,
)
from kiro_crew.testing.clock import ManualClock

APP = "test-app"
HOOKS = {"on_startup": "backend.hooks:on_startup"}
LEFT_IN_FOLDER = "seen-by-a-sandbox"


@pytest.fixture(autouse=True)
def _outside_a_sandbox(_floor_monkeypatch):
    # The gateway runs unsandboxed; a test host may itself be a sandbox.
    _floor_monkeypatch.delenv(member_records._IN_SANDBOX_MARKER, raising=False)


def _source(tmp_path, version="1.0.0"):
    src = _make_app_source(
        tmp_path / version,
        version=version,
        backend={"hooks": HOOKS},
        permissions={"api": ["/api/sessions"]},
    )
    (src / "backend").mkdir()
    (src / "backend" / "hooks.py").write_text("async def on_startup(ctx):\n    pass\n")
    return src


def _install_as_an_older_version_left_it(tmp_path, app_home):
    """Installed and enabled, with the secret only in the app's folder."""
    assert install_app(_source(tmp_path)).ok
    assert enable_app(APP).ok
    protected_app_state.secret_path(APP).unlink()
    legacy = app_home / "apps" / APP / ".app_secret"
    legacy.write_text(LEFT_IN_FOLDER, encoding="utf-8")
    return legacy


def _info() -> dict[str, Any]:
    info = get_app(APP)
    assert info is not None and info["manifest"]["backend"]["hooks"] == HOOKS
    return info


@contextlib.contextmanager
def _gateway_wiring(contexts: list[Any]):
    """The gateway around a hook load, with the hook itself stubbed to keep its context."""

    async def _keep_context(self, app_name, hook_path, ctx, *, phase):
        contexts.append(ctx)
        return True

    with (
        mock.patch.object(hooks_mod, "_lifecycle_dispatcher", LifecycleDispatcher()),
        mock.patch.object(hooks_mod, "_route_registry", None),
        mock.patch.object(hooks_mod, "app_execution_denied", lambda *a, **kw: ""),
        mock.patch.object(
            hooks_mod, "register_app_crons_with_service", mock.AsyncMock(return_value=[])
        ),
        mock.patch.object(hooks_mod, "record_loaded_hook_signature", mock.AsyncMock()),
        mock.patch.object(LifecycleDispatcher, "cache_shutdown_for", mock.AsyncMock()),
        mock.patch.object(LifecycleDispatcher, "_invoke", _keep_context),
    ):
        yield


def _assert_moved(legacy) -> str:
    assert not os.path.lexists(legacy)
    stored = read_app_secret(APP)
    assert stored and stored != LEFT_IN_FOLDER
    assert validate_app_secret(APP, stored) is True
    assert validate_app_secret(APP, LEFT_IN_FOLDER) is False
    return stored


@pytest.mark.asyncio
async def test_boot_moves_the_secret_of_an_app_installed_before_it_left_the_folder(
    tmp_path, app_home
):
    legacy = _install_as_an_older_version_left_it(tmp_path, app_home)
    assert read_app_secret(APP) == ""

    contexts: list[Any] = []
    with _gateway_wiring(contexts), mock.patch.object(hooks_mod, "list_apps", lambda: [_info()]):
        await hooks_mod.on_gateway_startup()

    assert len(contexts) == 1
    _assert_moved(legacy)


@pytest.mark.asyncio
async def test_enable_moves_the_secret_of_an_app_installed_and_then_updated(tmp_path, app_home):
    legacy = _install_as_an_older_version_left_it(tmp_path, app_home)
    # The update keeps the folder file, so the app still has no secret the gateway takes.
    assert update_app(_source(tmp_path, version="1.1.0")).ok
    assert legacy.read_text(encoding="utf-8") == LEFT_IN_FOLDER
    assert read_app_secret(APP) == ""

    contexts: list[Any] = []
    with _gateway_wiring(contexts):
        await hooks_mod.on_app_enable(APP, _info())

    assert len(contexts) == 1
    stored = _assert_moved(legacy)
    # A later load keeps the moved secret.
    with _gateway_wiring(contexts):
        await hooks_mod.on_app_enable(APP, _info())
    assert read_app_secret(APP) == stored


@pytest.mark.asyncio
async def test_the_hook_context_mints_this_apps_token(tmp_path, app_home, monkeypatch):
    # One stepped clock for the enable cutoff and the mint, so iat > cutoff never ties.
    clock = ManualClock(tick=1.0)
    clock.install(monkeypatch, manager_records)
    clock.install(monkeypatch, token_auth_tokens)
    _install_as_an_older_version_left_it(tmp_path, app_home)
    contexts: list[Any] = []
    with _gateway_wiring(contexts):
        await hooks_mod.on_app_enable(APP, _info())

    token = await contexts[0].app_token()

    valid, user_id, reason, app = validate_token_with_app(token, use_session_exp=True)
    assert (valid, user_id, app) == (True, APP, APP), reason


@pytest.mark.asyncio
async def test_a_context_kept_past_a_disable_mints_nothing(tmp_path, app_home):
    assert install_app(_source(tmp_path)).ok
    assert enable_app(APP).ok
    contexts: list[Any] = []
    with _gateway_wiring(contexts):
        await hooks_mod.on_app_enable(APP, _info())
    assert await contexts[0].app_token()

    assert disable_app(APP).ok

    assert await contexts[0].app_token() == ""


@pytest.mark.parametrize(
    ("port", "host", "origin"),
    [
        ("6777", None, "http://127.0.0.1:6777"),
        ("6777", "::1", "http://[::1]:6777"),
        ("6777", "10.0.0.5", ""),
        (None, None, ""),
        ("0", None, ""),
        ("not-a-port", None, ""),
    ],
)
def test_the_hook_context_gives_the_origin_a_spawned_backend_gets(
    tmp_path, monkeypatch, port, host, origin
):
    from kiro_crew.apps import backend
    from kiro_crew.apps.context import build_app_context

    spawn_rule = backend._loopback_gateway_origin

    for name, value in (("KIROCREW_BOUND_PORT", port), ("KIROCREW_BOUND_HOST", host)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    # Never a guess from the operator's port or the client's default.
    monkeypatch.setenv("KIROCREW_PORT", "5476")

    ctx = build_app_context(APP, tmp_path)

    assert ctx.gateway_origin() == origin == spawn_rule()
