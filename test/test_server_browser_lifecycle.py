"""The browser CLI's gateway hooks keep the gateway's shutdown and startup alive.

``kiro_crew.dashboard.server_runtime.browser_lifecycle`` registers the hooks that close
this gateway's browser sessions, its relay client and the CLI dashboard process at
cleanup, and that reclaim a previous life's stranded sessions at startup. Each step is
best-effort: a failure is logged and the next step still runs, because a shutdown that
raises would leave the gateway's exit to its deadline. These tests drive every step
failing through the names the hooks read on ``kiro_crew.dashboard.server``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web

import kiro_crew.dashboard.server as server


def _fail(calls: list[str], name: str):
    def _step(*_args: object) -> None:
        calls.append(name)
        raise RuntimeError(name)

    return _step


@pytest.mark.asyncio
async def test_the_view_shutdown_runs_every_step_when_each_one_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _close_relay_client(_app: web.Application) -> None:
        calls.append("relay")
        raise RuntimeError("relay")

    monkeypatch.setattr(server, "handlers", SimpleNamespace(close_relay_client=_close_relay_client))
    monkeypatch.setattr(
        server, "browser_cli_launcher", SimpleNamespace(close_all=_fail(calls, "close_all"))
    )
    monkeypatch.setattr(server, "browser_cli_view", SimpleNamespace(stop=_fail(calls, "stop")))
    state = SimpleNamespace(
        _browser_view_unregister=lambda: calls.append("unregister"), _background_tasks=set()
    )
    app = web.Application()
    server._register_browser_view_cleanup(app, state)
    (shutdown,) = [hook for hook in app.on_cleanup if hook.__name__ == "_browser_view_shutdown"]

    await shutdown(app)

    assert calls == ["unregister", "relay", "close_all", "stop"]


@pytest.mark.asyncio
async def test_the_view_shutdown_without_a_registered_stop_still_closes_everything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def _close_relay_client(_app: web.Application) -> None:
        calls.append("relay")

    monkeypatch.setattr(server, "handlers", SimpleNamespace(close_relay_client=_close_relay_client))
    monkeypatch.setattr(
        server, "browser_cli_launcher", SimpleNamespace(close_all=lambda: calls.append("close_all"))
    )
    monkeypatch.setattr(
        server, "browser_cli_view", SimpleNamespace(stop=lambda: calls.append("stop"))
    )
    state = SimpleNamespace(_background_tasks=set())
    app = web.Application()
    server._register_browser_view_cleanup(app, state)
    (shutdown,) = [hook for hook in app.on_cleanup if hook.__name__ == "_browser_view_shutdown"]

    await shutdown(app)

    assert calls == ["relay", "close_all", "stop"]


@pytest.mark.asyncio
async def test_a_failed_startup_reclaim_is_logged_and_the_stop_stays_registered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    registered: list[str] = []

    def _register_pre_exec_stop(label: str, _stop: object) -> object:
        registered.append(label)
        return lambda: None

    monkeypatch.setattr(server, "register_pre_exec_stop", _register_pre_exec_stop)
    monkeypatch.setattr(
        server,
        "browser_cli_launcher",
        SimpleNamespace(reclaim_stranded=_fail(calls, "reclaim_stranded")),
    )
    state = SimpleNamespace(_background_tasks=set())
    app = web.Application()
    server._register_browser_view_cleanup(app, state)
    (startup,) = [hook for hook in app.on_startup if hook.__name__ == "_browser_sessions_startup"]

    await startup(app)
    pending = list(state._background_tasks)
    assert len(pending) == 1
    await asyncio.wait_for(asyncio.gather(*pending), timeout=30)

    assert registered == ["Browser view"]
    assert calls == ["reclaim_stranded"]
    assert state._background_tasks == set()
    assert callable(state._browser_view_unregister)
