"""Shared setup for the ``POST /api/chat/slots`` publish-on-commit tests.

The fixture, the app and the bounded waits that more than one of the
``test_slot_create_*`` modules use. Not a test module.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_folders, chat_handlers
from kiro_crew.dashboard import slot_create_transaction as txn
from kiro_crew.memory_stores import provision_member_memory

#: The bound on every wait in this file. It only names a hang: a passing run
#: never waits on it, and the wait-discipline ratchet wants 30 s or more.
WAIT = 30


def _config(**members: Any) -> KiroCrewConfig:
    cfg = KiroCrewConfig()
    cfg.agents = {"default": KiroCrewAgentConfig(kiro_agent="kirocrew")}
    cfg.default_agent = "default"
    for name in members:
        cfg.agents[name] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        provision_member_memory(cfg, name)
    return cfg


@pytest.fixture
def state(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> Any:
    dashboard_state = _make_state(tmp_path)
    dashboard_state.sessions.get_provider.return_value = None
    dashboard_state.sessions.resumable_sid.return_value = ""
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    return dashboard_state


def _use_config(monkeypatch: pytest.MonkeyPatch, cfg: KiroCrewConfig) -> None:
    cfg.save()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))


def _slots_app(state: Any) -> web.Application:
    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    app.router.add_patch("/api/chat/slots/{slot}/folder", chat_folders.api_chat_slot_folder)
    return app


async def _post(state: Any, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    async with TestClient(TestServer(as_owner(_slots_app(state)))) as client:
        resp = await asyncio.wait_for(client.post("/api/chat/slots", json=payload), WAIT)
        return resp.status, await resp.json()


async def _drain(*tasks: asyncio.Task[Any]) -> None:
    """Cancel whatever is still running and wait for all of it, bounded."""
    for task in tasks:
        if not task.done():
            task.cancel()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), WAIT)


class _HeldRecord:
    """The first create's selection write stops until released, then writes or refuses."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, refuse: bool) -> None:
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.seen: list[bool] = []
        self._calls = 0
        self._refuse = refuse
        self._real = chat_handlers._record_explicit_agent_selection
        monkeypatch.setattr(chat_handlers, "_record_explicit_agent_selection", self._held)

    async def _held(self, *args: Any, **kwargs: Any) -> Any:
        self._calls += 1
        if self._calls == 1:
            self.entered.set()
            await asyncio.wait_for(self.release.wait(), WAIT)
            if self._refuse:
                raise OSError("selection write refused")
        return await self._real(*args, **kwargs)


async def _until(condition: Callable[[], bool]) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0)

    await asyncio.wait_for(poll(), WAIT)


def _pending(state: Any, name: str = "demo", **kwargs: Any) -> txn.PendingSlotCreate:
    existing, pending = state.prepare_slot(name, **kwargs)
    assert existing is None and pending is not None
    return txn.PendingSlotCreate(state, pending)
