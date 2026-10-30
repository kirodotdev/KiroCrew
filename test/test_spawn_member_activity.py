"""A crewmate's delegated run lands in its activity log, once per run conversation."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
import subagent_continuable_helpers

from kiro_crew import members
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers
continuation_runtime = subagent_continuable_helpers.continuation_runtime
_stop_reason = subagent_continuable_helpers._stop_reason


def _spawn_entries(member: str) -> list[dict]:
    rows = members.read_activity(members.member_slug(member))
    return [r for r in rows if r.get("member") == member and r.get("via") == "spawn"]


async def _close_all(sessions: Any) -> None:
    await await_until(
        asyncio.create_task(sessions.close_all(drain_timeout=0)), what="the sessions closing"
    )


@pytest.mark.asyncio
async def test_a_crew_run_and_its_continuation_record_one_spawn_entry(continuation_runtime):
    world = continuation_runtime
    _, store = await asyncio.to_thread(world.enroll_collision)
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn(
                "member task", crew="worker", memory_store=store, keep=True, cwd=world.project
            )
            assert child is not None and not child.error
            await await_until(manager._tasks[child.id], what="the child run finishing")
            assert not child.error
            entries = await asyncio.to_thread(_spawn_entries, "worker")
            assert [e.get("session") for e in entries] == [f"subagent:{child.id}"]
            assert entries[0].get("project") == world.project

            followup = manager.continue_conversation(child.id, "continue", cwd=world.project)
            assert followup is not None and not followup.error
            await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
            assert not followup.error
            assert await asyncio.to_thread(_spawn_entries, "worker") == entries
    finally:
        await _close_all(sessions)


@pytest.mark.asyncio
async def test_a_template_run_records_no_spawn_entry(continuation_runtime):
    world = continuation_runtime
    await asyncio.to_thread(world.enroll_collision)
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn("template task", agent="worker", cwd=world.project)
            assert child is not None and not child.error
            await await_until(manager._tasks[child.id], what="the child run finishing")
            assert not child.error
        assert await asyncio.to_thread(_spawn_entries, "worker") == []
    finally:
        await _close_all(sessions)


@pytest.mark.asyncio
async def test_a_crew_run_on_an_explicit_template_still_records_the_member(continuation_runtime):
    world = continuation_runtime
    _, store = await asyncio.to_thread(world.enroll_collision)
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn(
                "member task",
                crew="worker",
                agent="worker",
                memory_store=store,
                cwd=world.project,
            )
            assert child is not None and not child.error
            await await_until(manager._tasks[child.id], what="the child run finishing")
            assert not child.error
        entries = await asyncio.to_thread(_spawn_entries, "worker")
        assert [e.get("session") for e in entries] == [f"subagent:{child.id}"]
    finally:
        await _close_all(sessions)


@pytest.mark.asyncio
async def test_a_helper_inheriting_a_member_is_not_that_members_run(continuation_runtime):
    from kiro_crew import subagent_persistence as sp
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.execution_context import bind_session_execution, resolve_member_execution

    world = continuation_runtime
    await asyncio.to_thread(world.enroll_collision)
    parent = "dashboard:member-worker"
    bind_session_execution(parent, resolve_member_execution(KiroCrewConfig.load(), "worker"))
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn("helper task", parent_session_key=parent, cwd=world.project)
            assert child is not None and not child.error
            await await_until(manager._tasks[child.id], what="the child run finishing")
            assert not child.error
        selection = await asyncio.to_thread(sp.read_run_agent_selection, child.id)
        assert selection == ("member", "worker")
        assert await asyncio.to_thread(_spawn_entries, "worker") == []
    finally:
        await _close_all(sessions)


@pytest.mark.asyncio
async def test_a_crew_run_refused_at_startup_records_nothing(continuation_runtime, monkeypatch):
    from kiro_crew.memory_startup import MemoryStartupUnavailable

    world = continuation_runtime
    _, store = await asyncio.to_thread(world.enroll_collision)
    monkeypatch.setattr(
        "kiro_crew.context.prepare_store_vectors",
        AsyncMock(side_effect=MemoryStartupUnavailable("memory is still preparing")),
    )
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn(
                "member task", crew="worker", memory_store=store, cwd=world.project
            )
            assert child is not None and not child.error
            await await_until(manager._tasks[child.id], what="the child run finishing")
            assert child.error
        assert await asyncio.to_thread(_spawn_entries, "worker") == []
    finally:
        await _close_all(sessions)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_private_crew_run_records_nothing(continuation_runtime, mode):
    world = continuation_runtime
    _, store = await asyncio.to_thread(world.enroll_collision)
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn(
                "member task",
                crew="worker",
                memory_store=store,
                cwd=world.project,
                _memory_mode=mode,
            )
            assert child is not None and not child.error
            await await_until(manager._tasks[child.id], what="the child run finishing")
            assert not child.error
        assert await asyncio.to_thread(_spawn_entries, "worker") == []
    finally:
        await _close_all(sessions)


@pytest.mark.asyncio
async def test_a_respawned_crew_run_keeps_one_spawn_entry(continuation_runtime, monkeypatch):
    import kiro_crew.subagent as subagent_mod

    world = continuation_runtime
    _, store = await asyncio.to_thread(world.enroll_collision)
    real_build = subagent_mod.run_in_embed_pool
    first_build = asyncio.Event()
    calls = 0

    async def build(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_build.set()
            await asyncio.Event().wait()
        return await real_build(*args, **kwargs)

    monkeypatch.setattr(subagent_mod, "run_in_embed_pool", build)
    sessions, manager = world.new_manager()
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = manager.spawn(
                "member task", crew="worker", memory_store=store, cwd=world.project
            )
            assert child is not None and not child.error
            await await_until(first_build, what="the first prompt build")
            manager._tasks[child.id].cancel()
            await async_wait_until(lambda: child.done, describe=lambda: _stop_reason(child))
            assert child._cancel_retry_used and not child.error
        assert calls == 2
        entries = await asyncio.to_thread(_spawn_entries, "worker")
        assert [e.get("session") for e in entries] == [f"subagent:{child.id}"]
    finally:
        await _close_all(sessions)
