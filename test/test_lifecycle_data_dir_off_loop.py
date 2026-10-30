"""App hook contexts create the app's ``data`` dir off the event loop.

Every app lifecycle event builds an ``AppContext`` for the app, and that context
hands the app its ``<app_dir>/data`` directory, creating it first. Two builders
do this: ``LifecycleDispatcher._build_context`` (dispatch and disable) and
``hooks_integration._build_app_context_from_info`` (enable and gateway boot).
The create is a blocking filesystem call, so each builder's async wrapper runs
it in an ``asyncio.to_thread`` hop while the context itself is still built on
the loop that owns it.

These tests drive each async entry point that builds a context and record
which thread created the data dir. Moving the create back onto the loop makes
the recorded thread the loop thread and fails the test. They also pin that the
directory still exists and is the one the hook's context points at.
"""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path
from typing import Any
from unittest import mock

import pytest

import kiro_crew.apps.hooks_integration as hooks_mod
import kiro_crew.apps.lifecycle as lifecycle_mod
from kiro_crew.apps.lifecycle import LifecycleDispatcher

_APP = "data-dir-app"


def _app_info() -> dict[str, Any]:
    return {
        "name": _APP,
        "manifest": {
            "name": _APP,
            "backend": {
                "hooks": {
                    "on_startup": "backend.hooks:on_startup",
                    "on_shutdown": "backend.hooks:on_shutdown",
                }
            },
        },
    }


@pytest.fixture
def harness(tmp_path: Path):
    """Point the app dir at tmp and record the thread each data-dir create ran on.

    Yields ``(data_dir, mkdir_threads, invoked_contexts)``. The hook itself is
    stubbed: these tests are about the context the dispatcher builds for it,
    not about loading app code.
    """
    root = tmp_path / "apps" / _APP
    data_dir = root / "data"
    mkdir_threads: list[threading.Thread] = []
    invoked: list[Any] = []
    real_mkdir = Path.mkdir

    def _recording_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        if self == data_dir:
            mkdir_threads.append(threading.current_thread())
        real_mkdir(self, *args, **kwargs)

    async def _fake_invoke(
        self: LifecycleDispatcher, app_name: str, hook_path: str, ctx: Any, *, phase: str
    ) -> bool:
        invoked.append(ctx)
        return True

    with (
        mock.patch.dict(os.environ, {"KIROCREW_HOME": str(tmp_path)}),
        mock.patch.object(lifecycle_mod, "app_dir", lambda name: root),
        mock.patch.object(hooks_mod, "app_dir", lambda name: root),
        mock.patch.object(Path, "mkdir", _recording_mkdir),
        mock.patch.object(LifecycleDispatcher, "_invoke", _fake_invoke),
    ):
        yield data_dir, mkdir_threads, invoked


def _assert_created_off_loop(
    data_dir: Path, mkdir_threads: list[threading.Thread], invoked: list[Any]
) -> None:
    loop_thread = threading.current_thread()
    assert mkdir_threads, "the data dir was never created"
    assert all(
        t is not loop_thread for t in mkdir_threads
    ), "the app data dir was created on the event-loop thread"
    assert data_dir.is_dir()
    assert [ctx.data_dir for ctx in invoked] == [data_dir]


@pytest.mark.asyncio
async def test_dispatch_startup_creates_data_dir_off_loop(harness) -> None:
    data_dir, mkdir_threads, invoked = harness

    assert await LifecycleDispatcher().dispatch_startup([_app_info()]) == [_APP]

    _assert_created_off_loop(data_dir, mkdir_threads, invoked)


@pytest.mark.asyncio
async def test_dispatch_shutdown_creates_data_dir_off_loop(harness) -> None:
    data_dir, mkdir_threads, invoked = harness

    assert await LifecycleDispatcher().dispatch_shutdown([_app_info()]) == [_APP]

    _assert_created_off_loop(data_dir, mkdir_threads, invoked)


@pytest.mark.asyncio
async def test_dispatch_enable_creates_data_dir_off_loop(harness) -> None:
    data_dir, mkdir_threads, invoked = harness

    assert await LifecycleDispatcher().dispatch_enable(_app_info()) is True

    _assert_created_off_loop(data_dir, mkdir_threads, invoked)


@pytest.mark.asyncio
async def test_dispatch_disable_creates_data_dir_off_loop(harness) -> None:
    data_dir, mkdir_threads, invoked = harness

    assert await LifecycleDispatcher().dispatch_disable(_app_info()) is True

    _assert_created_off_loop(data_dir, mkdir_threads, invoked)


@pytest.mark.asyncio
async def test_on_app_disable_shutdown_hook_creates_data_dir_off_loop(harness) -> None:
    """The gateway disable path builds the same context through the dispatcher."""
    data_dir, mkdir_threads, invoked = harness

    with (
        mock.patch.object(hooks_mod, "_lifecycle_dispatcher", LifecycleDispatcher()),
        mock.patch.object(hooks_mod, "_route_registry", None),
        mock.patch.object(hooks_mod, "sel", lambda: mock.MagicMock()),
    ):
        result = await hooks_mod.on_app_disable(_APP, _app_info(), startup_stopped=True)

    assert result["hooks_shutdown"] == "ok"
    _assert_created_off_loop(data_dir, mkdir_threads, invoked)


@contextlib.contextmanager
def _gateway_wiring(info: dict[str, Any]):
    """Stub the gateway wiring around the context build: no crons, routes or SEL."""
    with (
        mock.patch.object(hooks_mod, "_lifecycle_dispatcher", LifecycleDispatcher()),
        mock.patch.object(hooks_mod, "_route_registry", None),
        mock.patch.object(hooks_mod, "sel", lambda: mock.MagicMock()),
        mock.patch.object(hooks_mod, "app_execution_denied", lambda *a, **kw: ""),
        mock.patch.object(
            hooks_mod, "register_app_crons_with_service", mock.AsyncMock(return_value=[])
        ),
        mock.patch.object(hooks_mod, "record_loaded_hook_signature", mock.AsyncMock()),
        mock.patch.object(hooks_mod, "list_apps", lambda: [info]),
        mock.patch.object(LifecycleDispatcher, "cache_shutdown_for", mock.AsyncMock()),
    ):
        yield


@pytest.mark.asyncio
async def test_on_app_enable_creates_data_dir_off_loop(harness) -> None:
    """The gateway enable path builds its shared route + startup context off the loop."""
    data_dir, mkdir_threads, invoked = harness

    with _gateway_wiring(_app_info()):
        await hooks_mod.on_app_enable(_APP, _app_info())

    _assert_created_off_loop(data_dir, mkdir_threads, invoked)


@pytest.mark.asyncio
async def test_on_gateway_startup_creates_data_dir_off_loop(harness) -> None:
    """Gateway boot builds the same context, once per enabled app, off the loop."""
    data_dir, mkdir_threads, invoked = harness
    info = {**_app_info(), "enabled": True}

    with _gateway_wiring(info):
        await hooks_mod.on_gateway_startup()

    _assert_created_off_loop(data_dir, mkdir_threads, invoked)


def test_build_context_itself_does_not_create_the_data_dir(harness) -> None:
    """The create lives in the async callers, so the sync builders stay fs-free."""
    data_dir, mkdir_threads, _ = harness

    ctx = LifecycleDispatcher()._build_context(_app_info(), phase="startup")
    with mock.patch.object(hooks_mod, "_route_registry", None):
        gateway_ctx = hooks_mod._build_app_context_from_info(_app_info())

    assert ctx.data_dir == data_dir
    assert gateway_ctx.data_dir == data_dir
    assert mkdir_threads == []
