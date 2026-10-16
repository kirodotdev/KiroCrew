"""Shared setup for the session-trust persistence tests: the hermetic store, the
owner and app identities, and a fresh-gateway restart."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state

from kiro_crew.dashboard import chat_trust_persistence
from kiro_crew.dashboard import session_trust_store as store
from kiro_crew.dashboard.chat_handlers import api_chat_mode, api_chat_slot_approve
from kiro_crew.safety_override import reset_singleton
from kiro_crew.testing.wait import async_wait_until

#: Captured before the autouse fixture stubs it, for the tests of the real key path.
_REAL_SIGNING_ROOT = store._signing_root
_REAL_HOST_BOOT_ID = store.platform_compat.host_boot_id


@pytest.fixture(autouse=True)
def _hermetic(tmp_path, _floor_monkeypatch):
    _floor_monkeypatch.setenv("KIROCREW_HOME", str(tmp_path))
    _floor_monkeypatch.setattr(store, "_path_override", tmp_path / "session-trust" / "grants.json")
    _floor_monkeypatch.setattr(store, "_signing_root", lambda: b"k" * 32)
    # One host boot for the whole test unless a test reboots it.
    _floor_monkeypatch.setattr(store.platform_compat, "host_boot_id", lambda: "boot-1")
    _floor_monkeypatch.setattr(chat_trust_persistence, "_owner_stopped", False)
    _floor_monkeypatch.setattr(chat_trust_persistence, "_withdrawn_keys", set())
    _floor_monkeypatch.setattr(chat_trust_persistence, "_withdrawn_all", False)
    _floor_monkeypatch.setattr(chat_trust_persistence, "_stopping", False)
    # Markers a test writes stand for a previous run's stop unless it says otherwise.
    _floor_monkeypatch.setattr(chat_trust_persistence, "_STARTED", float("inf"))
    chat_trust_persistence._stop_marked.clear()
    reset_singleton()
    yield
    # Before the patches unwind the path overrides: a clear worker still running
    # would otherwise act on the real data home.
    _join_trust_workers()
    reset_singleton()


def _join_trust_workers() -> None:
    import threading

    for worker in threading.enumerate():
        if worker.name in ("session-trust-force-clear", "session-trust-signal-clear"):
            worker.join(5.0)
            assert not worker.is_alive(), worker.name


async def _until(condition, what: str) -> None:
    """Wait, bounded, for *condition* -- a real signal, never a fixed duration."""
    await async_wait_until(condition, describe=lambda: what)


def _queued_store_writes() -> int:
    """How many store writes are queued behind the one holding the write order."""
    return len(getattr(chat_trust_persistence._write_order(), "_waiters", None) or ())


def _grant(keys) -> bool:
    """Remember *keys* the way the one production writer does."""
    return store.grant_counting(keys)[0]


def _state(tmp_path):
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.owner_id = ""
    st.notify = MagicMock()
    return st


def _app(state, identity: dict) -> web.Application:
    @web.middleware
    async def _auth(request: web.Request, handler):
        for k, v in identity.items():
            request[k] = v
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat/mode", api_chat_mode)
    app.router.add_post("/api/chat/slots/{slot}/approve", api_chat_slot_approve)
    return app


OWNER = {"app": "", "user": "local-app"}
APP_TOKEN = {"app": "crew-keyboard", "user": ""}
INTERNAL = {"app": "", "user": "local-app", "internal_auth": True}


async def _mode(state, identity, body):
    async with TestClient(TestServer(_app(state, identity))) as client:
        resp = await client.post("/api/chat/mode", json=body)
        assert resp.status == 200, await resp.text()


async def _restart(tmp_path, *slot_keys, app: str = ""):
    """A fresh gateway: new state, the same chats rebuilt, then the boot restore."""
    fresh = _state(tmp_path)
    slots = [
        fresh.get_or_create_slot(k, app=app) if app else fresh.get_or_create_slot(k)
        for k in slot_keys
    ]
    restored = await chat_trust_persistence.restore_persisted_trust_async(fresh)
    return fresh, slots, restored
