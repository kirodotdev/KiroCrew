"""Dashboard member grants validate current ownership before publication."""

from __future__ import annotations

import asyncio
import copy
import os
import threading

import pytest
from chat_test_helpers import _make_state

from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
from kiro_crew.dashboard import chat_persistence
from kiro_crew.execution_context import read_session_execution
from kiro_crew.member_memory_auth import read_private_session_store
from kiro_crew.memory_stores import UnknownMemoryStore, provision_member_memory


@pytest.fixture
def member_stores():
    cfg = KiroCrewConfig.load()
    cfg.agents["writer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    cfg.agents["reviewer"] = KiroCrewAgentConfig(kiro_agent="kirocrew")
    writer = provision_member_memory(cfg, "writer")
    reviewer = provision_member_memory(cfg, "reviewer")
    cfg.save()
    return writer, reviewer


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["deleted", "recreated", "reassigned"])
async def test_dashboard_pin_refuses_stale_member_generation(
    tmp_path, member_stores, monkeypatch, change
):
    """A grant uses current membership, not its request-entry snapshot."""
    writer, reviewer = member_stores
    stale = KiroCrewConfig.load()
    current = copy.deepcopy(stale)
    if change == "deleted":
        del current.agents["writer"]
    elif change == "recreated":
        replacement = f"{writer}-recreated"
        current.agents["writer"].memory_store = replacement
        current.memory_stores[replacement] = copy.deepcopy(current.memory_stores[writer])
    else:
        current.agents["writer"].memory_store = reviewer

    called = False

    def assignment(*args, **kwargs):
        nonlocal called
        called = True
        return writer

    monkeypatch.setattr(chat_persistence.KiroCrewConfig, "load", lambda: current)
    monkeypatch.setattr(chat_persistence, "_pin_private_agent_assignment", assignment)
    state = _make_state(tmp_path)
    with pytest.raises(UnknownMemoryStore, match="changed during private memory assignment"):
        await chat_persistence.pin_private_agent_store(
            state, f"dashboard:stale-{change}", "writer", stale
        )
    assert not called


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["became_private", "became_non_private"])
async def test_dashboard_pin_refuses_privateness_changed_during_wait(
    tmp_path, member_stores, monkeypatch, direction
):
    """The private/non-private dispatch is decided from current config, not entry.

    Deciding it from the request-entry snapshot is the same stale-snapshot
    defect in the other direction: a member granted a private store during the
    wait would be pinned non-private, writing its content to the shared store,
    and one whose store was retired would be pinned private on authority the
    caller never validated.
    """
    writer, _ = member_stores
    private = KiroCrewConfig.load()
    non_private = copy.deepcopy(private)
    non_private.agents["writer"].memory_store = ""
    stale, current = (
        (non_private, private) if direction == "became_private" else (private, non_private)
    )

    called = False

    def assignment(*args, **kwargs):
        nonlocal called
        called = True
        return writer

    monkeypatch.setattr(chat_persistence.KiroCrewConfig, "load", lambda: current)
    monkeypatch.setattr(chat_persistence, "_pin_private_agent_assignment", assignment)
    state = _make_state(tmp_path)
    with pytest.raises(UnknownMemoryStore, match="changed during private memory assignment"):
        await chat_persistence.pin_private_agent_store(
            state, f"dashboard:privateness-{direction}", "writer", stale
        )
    assert not called
    assert (
        await asyncio.to_thread(read_private_session_store, f"dashboard:privateness-{direction}")
        is None
    )


@pytest.mark.asyncio
async def test_dashboard_pin_holds_namespace_lock_through_publication(
    tmp_path, member_stores, monkeypatch
):
    """Deletion cannot retire a store between its final check and binding write."""
    from kiro_crew import execution_context, memory_stores, platform_compat

    writer, _ = member_stores
    state = _make_state(tmp_path)
    key = "dashboard:namespace-locked-pin"
    checked = threading.Event()
    release = threading.Event()
    real_bind = execution_context.bind_session_execution

    def pause_before_publication(session_key, execution, **kwargs):
        if session_key == key:
            checked.set()
            assert release.wait(timeout=5), "private binding write was not released"
        return real_bind(session_key, execution, **kwargs)

    monkeypatch.setattr(execution_context, "bind_session_execution", pause_before_publication)
    pin = asyncio.create_task(
        chat_persistence.pin_private_agent_store(state, key, "writer", KiroCrewConfig.load())
    )
    try:
        assert await asyncio.wait_for(asyncio.to_thread(checked.wait, 5), timeout=6)
        lock_path = (
            memory_stores.memory_stores_root().resolve()
            / memory_stores.MEMBER_BACKUPS_DIR_NAME
            / ".namespace.lock"
        )
        fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            with pytest.raises(BlockingIOError):
                with platform_compat.file_lock(fd, exclusive=True, required=True, wait=False):
                    pass
        finally:
            os.close(fd)
    finally:
        release.set()
        await asyncio.wait_for(pin, timeout=5)
    assert pin.result() == writer
    assert await asyncio.to_thread(read_private_session_store, key) == writer


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("agent", "expected"),
    [("writer", "private"), ("default", "none"), ("legacy", "none")],
)
@pytest.mark.parametrize("memory_mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("validate_only", [False, True])
async def test_dashboard_pin_preserves_private_and_non_private_controls(
    tmp_path, member_stores, agent, expected, memory_mode, validate_only
):
    writer, _ = member_stores
    cfg = KiroCrewConfig.load()
    if agent == "legacy":
        cfg.agents[agent] = KiroCrewAgentConfig(kiro_agent="kirocrew")
        cfg.save()
    state = _make_state(tmp_path)
    key = f"dashboard:pin-control-{agent}"
    assigned = await chat_persistence.pin_private_agent_store(
        state, key, agent, cfg, memory_mode=memory_mode, validate_only=validate_only
    )
    assert assigned == (writer if expected == "private" else "")
    published = expected == "private" and not validate_only
    assert await asyncio.to_thread(read_private_session_store, key) == (
        writer if published else None
    )
    execution = await asyncio.to_thread(read_session_execution, key)
    if published:
        assert execution is not None
        assert execution.memory_mode == memory_mode
    else:
        assert execution is None
