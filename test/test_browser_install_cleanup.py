"""Lifecycle coverage for browser-install shutdown cleanup."""

from __future__ import annotations

import inspect
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web

from kiro_crew.dashboard import server as srv


@pytest.mark.asyncio
async def test_browser_install_cleanup_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    stop = AsyncMock(side_effect=RuntimeError("stop failed"))
    monkeypatch.setattr(srv.handlers, "stop_browser_install", stop)
    app = web.Application()
    state = SimpleNamespace()
    app["state"] = state
    srv._register_browser_install_cleanup(app, state)

    cleanup = next(hook for hook in app.on_cleanup if hook.__name__ == "_browser_install_shutdown")
    with caplog.at_level(logging.DEBUG):
        await cleanup(app)

    stop.assert_awaited_once_with(state)
    assert "browser install stop failed during shutdown" in caplog.text


def test_both_gateway_modes_register_install_cleanup_before_runner_setup() -> None:
    registration = "_register_browser_install_cleanup(app, state)"
    for entrypoint in (srv.start_dashboard, srv.start_api_server):
        source = inspect.getsource(entrypoint)
        assert registration in source
        assert source.index(registration) < source.index("await runner.setup()")
