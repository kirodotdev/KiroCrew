"""A dashboard create answered with a failure leaves no session behind.

``api_chat_slot_create`` mints the slot inside its ``suspend_slots_push`` block,
then pins the member's private store, resolves the agent bindings and records
the selection. Two failures come back after the mint: ``store_unavailable``
(503) when the pin or the resolution raises, and ``session_rebound`` (409) when
the newborn changed while those awaits ran. The client is told the create
failed, so a newborn still in ``state._slots`` goes out in the block's owed
slots frame as a session nobody asked to keep, and every retry adds another
empty row. These tests force each failure and require that the key is neither
live nor listed in any frame.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_state
from dashboard_owner_helpers import as_owner

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard import chat_handlers
from kiro_crew.testing.wait import async_wait_until


def _member_config() -> KiroCrewConfig:
    from kiro_crew.config.loader import KiroCrewAgentConfig
    from kiro_crew.memory_stores import provision_member_memory

    cfg = KiroCrewConfig()
    cfg.agents = {
        "default": KiroCrewAgentConfig(kiro_agent="kirocrew"),
        "worker": KiroCrewAgentConfig(kiro_agent="kirocrew"),
    }
    cfg.default_agent = "default"
    provision_member_memory(cfg, "worker")
    return cfg


def _record_frames(state: Any) -> list[list[dict]]:
    seen: list[list[dict]] = []

    def capture(payload: dict) -> None:
        if payload.get("_type") == "slots":
            seen.append(json.loads(payload["slots"]))

    state._broadcast = capture  # type: ignore[method-assign]
    # Reopen the coalescing window so the block's owed push is sent, not absorbed.
    state._slots_broadcast_last = float("-inf")
    return seen


async def _drain_trailing_push(state: Any) -> None:
    """Wait until no coalesced slots push is still armed, so every frame the
    create caused has reached the recorder before the frames are read."""
    await async_wait_until(
        lambda: state._slots_broadcast_timer is None,
        describe=lambda: "a trailing slots push is still armed",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito"])
@pytest.mark.parametrize("failure", ["store_unavailable", "session_rebound"])
async def test_a_failed_create_neither_keeps_nor_announces_its_newborn(
    tmp_path, monkeypatch, failure, memory_mode
) -> None:
    from kiro_crew.session_agent_selection import session_agent_selection_name

    state = _make_state(tmp_path)
    cfg = _member_config()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    loop = asyncio.get_running_loop()
    slots_before = set(state._slots)
    restricted_before = set(state._restricted_keys)
    pinned: list[str] = []
    real_resolve = chat_handlers.resolve_agent_bindings

    async def pin(state_arg, session_key, agent, config, **kwargs):
        pinned.append(session_key)
        if failure == "store_unavailable":
            raise OSError("private store unavailable")
        # The store this test is not about: assign none and let the selection run.
        return ""

    def resolve(config, agent, project=None, **kwargs):
        result = real_resolve(config, agent, project, **kwargs)
        # The handler also resolves once before the mint; only the post-mint
        # call has a newborn to move. Another request moves it while this
        # resolution is off the loop.
        newborn = next((s for k, s in state._slots.items() if k not in slots_before), None)
        if newborn is not None:
            loop.call_soon_threadsafe(setattr, newborn, "project", str(tmp_path / "elsewhere"))
        return result

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)
    if failure == "session_rebound":
        monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve)
    frames = _record_frames(state)

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        response = await client.post(
            "/api/chat/slots", json={"agent": "worker", "memory_mode": memory_mode}
        )
        data = await response.json()
        await _drain_trailing_push(state)

    expected_status = 503 if failure == "store_unavailable" else 409
    assert response.status == expected_status, data
    assert data["code"] == failure
    # Positive control: the create got as far as the pin, so a slot was minted.
    assert len(pinned) == 1
    session_key = pinned[0]
    key = session_key.removeprefix("dashboard:")
    announced = [row for frame in frames for row in frame if row["key"] == key]
    assert announced == [], "a slots frame listed a session whose create failed"
    assert set(state._slots) == slots_before
    assert state._restricted_keys == restricted_before
    assert session_agent_selection_name(session_key) is None


# The body of a header New with a crewmate picked, in each shape that registers
# a different set of keys at the mint: none, a restricted key, an ephemeral key.
_SHAPES = {
    "persistent": {"agent": "worker"},
    "incognito": {"agent": "worker", "memory_mode": "incognito"},
    "ephemeral": {"agent": "worker", "ephemeral": True},
}


async def _post_create(state: Any, body: dict) -> int | None:
    """POST a create and return its status, or None when the handler raised a
    cancellation and the server dropped the connection instead of answering."""
    import aiohttp

    app = web.Application()
    app["state"] = state
    app.router.add_post("/api/chat/slots", chat_handlers.api_chat_slot_create)
    async with TestClient(TestServer(as_owner(app))) as client:
        try:
            response = await client.post("/api/chat/slots", json=body)
            status: int | None = response.status
        except aiohttp.ClientError:
            status = None
        await _drain_trailing_push(state)
    return status


def _assert_nothing_left(
    state: Any, frames: list[list[dict]], before: tuple[set, set, set], key: str
) -> None:
    from kiro_crew.session_agent_selection import session_agent_selection_name

    slots_before, restricted_before, ephemeral_before = before
    announced = [row for frame in frames for row in frame if row["key"] == key]
    assert announced == [], "a slots frame listed a session whose create failed"
    assert set(state._slots) == slots_before
    assert state._restricted_keys == restricted_before
    assert state._ephemeral_keys == ephemeral_before
    assert session_agent_selection_name(f"dashboard:{key}") is None


def _newborn_key(state: Any, slots_before: set) -> str | None:
    return next((k for k in state._slots if k not in slots_before), None)


def _member_state(tmp_path, monkeypatch) -> tuple[Any, KiroCrewConfig, tuple[set, set, set]]:
    state = _make_state(tmp_path)
    cfg = _member_config()
    monkeypatch.setattr(chat_handlers, "KiroCrewConfig", SimpleNamespace(load=lambda: cfg))
    monkeypatch.setattr(chat_handlers, "schedule_eager_spawn", lambda *args, **kwargs: None)
    before = (set(state._slots), set(state._restricted_keys), set(state._ephemeral_keys))
    return state, cfg, before


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_SHAPES))
@pytest.mark.parametrize("step", ["pin_private_agent_store", "resolve_agent_bindings"])
async def test_a_create_cancelled_after_the_mint_leaves_no_session(
    tmp_path, monkeypatch, step, shape
) -> None:
    """A cancellation is a ``BaseException``: the assignment's ``except
    Exception`` lets it through, and the block's exit frame must still not list
    the newborn."""
    state, _cfg, before = _member_state(tmp_path, monkeypatch)
    minted: list[str] = []
    real_resolve = chat_handlers.resolve_agent_bindings

    async def pin(state_arg, session_key, agent, config, **kwargs):
        if step == "pin_private_agent_store":
            minted.append(session_key.removeprefix("dashboard:"))
            raise asyncio.CancelledError()
        return ""

    def resolve(config, agent, project=None, **kwargs):
        # The handler also resolves once before the mint; only the post-mint
        # call has a newborn to lose.
        key = _newborn_key(state, before[0])
        if key is not None:
            minted.append(key)
            raise asyncio.CancelledError()
        return real_resolve(config, agent, project, **kwargs)

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)
    monkeypatch.setattr(chat_handlers, "resolve_agent_bindings", resolve)
    frames = _record_frames(state)

    status = await _post_create(state, _SHAPES[shape])

    assert status in (None, 500)
    # Positive control: the cancellation landed after the mint.
    assert len(minted) == 1
    _assert_nothing_left(state, frames, before, minted[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", ["persistent", "incognito"])
async def test_an_unexpected_error_after_the_mint_leaves_no_session(
    tmp_path, monkeypatch, shape
) -> None:
    """A per-chat session folder that fails with anything but
    ``SessionFolderError`` escapes the handler as a 500."""
    state, cfg, before = _member_state(tmp_path, monkeypatch)
    cfg.dashboard.session_folder_root = str(tmp_path / "sessions")
    minted: list[str] = []

    def explode(root: str) -> str:
        minted.append(_newborn_key(state, before[0]) or "")
        raise RuntimeError("disk went away")

    monkeypatch.setattr(chat_handlers, "create_session_folder", explode)
    frames = _record_frames(state)

    status = await _post_create(state, _SHAPES[shape])

    assert status == 500
    assert len(minted) == 1 and minted[0]
    _assert_nothing_left(state, frames, before, minted[0])


@pytest.mark.asyncio
async def test_an_error_after_the_pick_is_recorded_undoes_the_pick(tmp_path, monkeypatch) -> None:
    """A titled create saves the slot after the member assignment recorded its
    pick. A failure there takes the pick back with the slot."""
    state, _cfg, before = _member_state(tmp_path, monkeypatch)
    minted: list[str] = []

    async def pin(state_arg, session_key, agent, config, **kwargs):
        return ""

    async def save(state_arg, slot, **kwargs):
        from kiro_crew.session_agent_selection import session_agent_selection_name

        # Positive control: the pick is on disk when the failure lands.
        assert session_agent_selection_name(f"dashboard:{slot.key}") is not None
        minted.append(slot.key)
        raise RuntimeError("history store went away")

    monkeypatch.setattr(chat_handlers, "pin_private_agent_store", pin)
    monkeypatch.setattr(chat_handlers, "save_slot_off_loop", save)
    frames = _record_frames(state)

    status = await _post_create(state, {"agent": "worker", "title": "Plan"})

    assert status == 500
    assert len(minted) == 1
    _assert_nothing_left(state, frames, before, minted[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("shape", sorted(_SHAPES))
async def test_an_ownership_refusal_of_a_newborn_leaves_no_session(
    tmp_path, monkeypatch, shape
) -> None:
    """The post-mint ownership check answers 404 for a newborn that
    ``get_or_create_slot`` linked to a session the caller may not reach."""
    state, _cfg, before = _member_state(tmp_path, monkeypatch)
    minted: list[str] = []

    def deny(request_app, slot, key, route):
        minted.append(key)
        return web.json_response({"error": "not found"}, status=404)

    monkeypatch.setattr(chat_handlers, "deny_app_slot_session_access", deny)
    frames = _record_frames(state)

    status = await _post_create(state, _SHAPES[shape])

    assert status == 404
    assert len(minted) == 1
    _assert_nothing_left(state, frames, before, minted[0])
