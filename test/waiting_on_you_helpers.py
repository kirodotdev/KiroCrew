"""Shared stand-ins for the waiting-on-you tests: a store with a fixed key, the app, an ask."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from aiohttp import web
from chat_test_helpers import _make_state

from kiro_crew.dashboard import waiting_on_you as store_mod
from kiro_crew.dashboard import waiting_on_you_events as events
from kiro_crew.dashboard.handlers import waiting_on_you as routes
from kiro_crew.testing.wait import finished

KEY = b"k" * 32
SLOT = "chat-1-1700000000"
SESSION = f"dashboard:{SLOT}"


class Clock:
    """A clock the test moves by hand."""

    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def store_path() -> Path:
    from kiro_crew.config.loader import config_dir

    return Path(config_dir()) / store_mod.STORE_FILENAME


def new_store(clock: Clock | None = None, *, key: bytes = KEY) -> store_mod.WaitingStore:
    return store_mod.WaitingStore(store_path(), clock or Clock(), key=lambda: key)


@pytest.fixture(name="clock")
def clock_fixture() -> Clock:
    return Clock()


@pytest.fixture(name="store")
def store_fixture(monkeypatch, clock) -> store_mod.WaitingStore:
    """The data home's store, keyed with a fixed test key and a hand-moved clock."""
    made = new_store(clock)
    monkeypatch.setattr(store_mod, "_STORES", {store_path(): made})
    return made


@pytest.fixture(name="state")
def state_fixture(tmp_path, store):
    st = _make_state(tmp_path)
    st.broadcast_ws = MagicMock()
    st.push_slots_update = MagicMock()
    st.push_slot_patch = MagicMock()
    st.owner_id = ""
    st._waiting_on_you_store = store
    st.get_or_create_slot(SLOT)
    return st


def ask(key: str = "scope-override", options: tuple[str, ...] = ("I posted it", "Hold")) -> dict:
    return {
        "key": key,
        "title": f"Post the override ({key})",
        "detail": "Security Scope Review blocks merge until a writer posts it.",
        "options": [{"label": label, "description": ""} for label in options],
    }


async def pin(store: store_mod.WaitingStore, item: dict, slot: str = SLOT) -> dict[str, Any]:
    public, _changed, _keys = await store.declare(slot, item, persist=True)
    return public


async def settle_retirements() -> None:
    """Wait for every retirement the rows scheduled; a hang fails naming each stuck task."""

    async def _drain() -> None:
        while True:
            finished_tasks = {task for task in events._RETIRE_TASKS if task.done()}
            events._RETIRE_TASKS.difference_update(finished_tasks)
            for task in finished_tasks:
                task.result()
            if not events._RETIRE_TASKS:
                return
            await asyncio.wait(set(events._RETIRE_TASKS))

    try:
        await finished(_drain(), "the scheduled waiting-on-you retirements")
    except AssertionError as exc:
        stacks = io.StringIO()
        for task in list(events._RETIRE_TASKS):
            task.print_stack(file=stacks)
        raise AssertionError(f"{exc}\nstill pending:\n{stacks.getvalue()}") from None


def make_app(state: Any, *, user: str = "local-app", app_name: str = "") -> web.Application:
    """The chat send, the queued-message edit and the three waiting-on-you routes,
    behind a test auth layer.

    The layer publishes the caller's Shared Crew actor as the real middleware does,
    so a row the owner sends is authored by the owner.
    """
    from kiro_crew.dashboard.chat import api_chat
    from kiro_crew.dashboard.chat_api.prompt_queue import api_chat_slot_queue_edit
    from kiro_crew.shared_crew import identity

    @web.middleware
    async def _auth(request: web.Request, handler: Any) -> web.StreamResponse:
        request["app"] = app_name
        request["user"] = user
        request["is_dashboard_user"] = app_name == ""
        request.setdefault("internal_auth", False)
        await identity.publish_actor(request, trust=None, serve_peer=None)
        return await handler(request)

    app = web.Application(middlewares=[_auth])
    app["state"] = state
    app.router.add_post("/api/chat", api_chat)
    app.router.add_patch("/api/chat/slots/{slot}/queue/{queue_id}", api_chat_slot_queue_edit)
    app.router.add_post("/api/agent-waiting/set", routes.api_agent_waiting_set)
    app.router.add_post("/api/agent-waiting/clear", routes.api_agent_waiting_clear)
    app.router.add_post(
        "/api/chat/slots/{slot}/waiting-on-you/{key}/dismiss", routes.api_waiting_on_you_dismiss
    )
    return app


def user_rows(state: Any, slot: str = SLOT) -> list[dict]:
    return [m for m in state._slots[slot].messages if m.get("role") == "user"]


def waiting_rows(state: Any, slot: str = SLOT) -> list[dict]:
    return [
        m
        for m in state._slots[slot].messages
        if m.get("role") == "card"
        and (m.get("meta") or {}).get("card", {}).get("surface") == "waiting"
    ]
