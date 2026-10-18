"""A continuation runs under the template namespace its run was allocated in.

The parent's selection snapshot, the allocation namespace and the agent template a
continuation resolves stay those of the opening run, against a real allocation
(``continuation_runtime``); and a channel spawn carries the member store it verified.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import subagent_continuable_helpers
from subagent_continuable_helpers import _manager, _mock_sessions

from kiro_crew.testing.wait import async_wait_until, await_until

# ``SubagentManager.spawn`` refuses -- registering no task -- while the host
# looks short of memory, which is the runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

# pytest resolves a fixture in the requesting test's module, so each shared
# fixture this module uses is bound here by name.
_close_subagent_managers = subagent_continuable_helpers._close_subagent_managers
continuation_runtime = subagent_continuable_helpers.continuation_runtime


async def _close_all(sessions: Any) -> None:
    """Close every session with no drain, under the lost-run bound (a hang fails by name)."""
    await await_until(
        asyncio.create_task(sessions.close_all(drain_timeout=0)), what="the sessions closing"
    )


class TestContinuationTemplateNamespace:
    def test_parent_selection_snapshot_keeps_allocation_namespace(self):
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig
        from kiro_crew.session import SessionManager, _Session

        cfg = KiroCrewConfig()
        sessions = SessionManager(cfg)
        assert sessions.get_agent_selection("absent") == ("template", "")
        session = _Session(provider=MagicMock(), agent="worker")
        sessions._sessions["parent"] = session
        template = sessions.get_agent_selection("parent")
        cfg.agents["worker"] = KiroCrewAgentConfig(kiro_agent="different-template")
        assert sessions.get_agent_selection("parent") == template == ("template", "worker")
        session.capability_member = "member-alias"
        member = sessions.get_agent_selection("parent")
        session.agent = "a-new-generation"
        assert member == sessions.get_agent_selection("parent") == ("member", "member-alias")
        assert template == ("template", "worker")

    @pytest.mark.parametrize("agent,member", [(None, ""), ("worker", None), ("worker", [])])
    def test_present_parent_with_unavailable_selection_refuses(self, agent, member):
        from types import SimpleNamespace

        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.session import SessionManager

        sessions = SessionManager(KiroCrewConfig())
        sessions._sessions["parent"] = SimpleNamespace(agent=agent, capability_member=member)
        with pytest.raises(ValueError, match="parent agent selection unavailable"):
            sessions.get_agent_selection("parent")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("restart", [False, True], ids=["live-manager", "restart"])
    async def test_retained_template_survives_same_named_member_enrollment(
        self, continuation_runtime, restart
    ):
        from kiro_crew import subagent_persistence as sp
        from kiro_crew.member_memory_auth import private_memory_store_for_session

        world = continuation_runtime
        sessions, manager = world.new_manager()
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                original = manager.spawn(
                    "original task",
                    parent_session_key=world.parent,
                    agent="worker",
                    memory_store=world.store,
                    keep=True,
                    cwd=world.project,
                )
                assert original is not None and not original.error
                await await_until(manager._tasks[original.id], what="the original run finishing")
                assert not original.error
                key = f"subagent:{original.id}"
                first = world.made[-1]
                assert first.template == "worker" and first.messages
                sid = first.session_id
                assert sessions.resumable_sid(key) == sid
                assert (await asyncio.to_thread(sp.read_run_agent_selection, original.id))[
                    1
                ] == "worker"
                generation, peer_store = await asyncio.to_thread(world.enroll_collision)
                assert peer_store != world.store
                if restart:
                    await _close_all(sessions)
                    sessions, manager = world.new_manager()
                    assert not manager._agents
                followup = manager.continue_conversation(
                    original.id,
                    "continue the task",
                    parent_session_key=world.parent,
                    cwd=world.project,
                )
                assert followup is not None and not followup.error
                await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
                assert not followup.error
                resumed = world.made[-1]
                assert resumed is not first and resumed.messages
                assert resumed.key == key
                assert resumed.resume_sid == sid == resumed.session_id
                assert followup.conversation_key == key
                assert sessions.resumable_sid(key) == sid
                assert (await asyncio.to_thread(sp.read_run_agent_selection, followup.id))[
                    1
                ] == "worker"
                assert await asyncio.to_thread(sp.read_run_memory_store, followup.id) == world.store
                assert await asyncio.to_thread(private_memory_store_for_session, key) == world.store
                assert resumed.template == "worker", (
                    f"retained template was replaced by member generation {generation}: "
                    f"{resumed.template}; crew_agent={resumed.crew_agent!r}"
                )
        finally:
            await _close_all(sessions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fallback", [False, True], ids=["dedicated", "shared-fallback"])
    async def test_explicit_template_is_not_a_same_named_enrolled_member(
        self, continuation_runtime, monkeypatch, fallback
    ):
        world = continuation_runtime
        generation, _ = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        shared = AsyncMock(side_effect=RuntimeError("parent runtime unavailable"))
        if fallback:
            # Model an eligible global parent's native runtime failing to create
            # a shared handle. Dedicated fallback still uses the real allocator.
            monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: True)
            monkeypatch.setattr(manager, "_create_shared_session", shared)
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                child = manager.spawn(
                    "use the explicit template",
                    agent="worker",
                    cwd=world.project,
                    keep=not fallback,
                )
                assert child is not None and not child.error
                await await_until(manager._tasks[child.id], what="the child run finishing")
                assert not child.error
            if fallback:
                shared.assert_awaited_once()
            provider = world.made[-1]
            assert provider.key == f"subagent:{child.id}"
            assert provider.messages and not provider._private_memory
            assert provider.template == "worker", (
                f"explicit template was replaced by member generation {generation}: "
                f"{provider.template}; crew_agent={provider.crew_agent!r}"
            )
        finally:
            await _close_all(sessions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", ["", "worker"])
    @pytest.mark.parametrize("queued", [False, True])
    async def test_named_crew_keeps_member_identity(self, continuation_runtime, override, queued):
        from kiro_crew import subagent_persistence as sp

        world = continuation_runtime
        generation, store = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                if queued:
                    manager._running_count = manager.max_concurrent
                child = manager.spawn(
                    "named member task",
                    crew="worker",
                    agent=override,
                    memory_store=store,
                    keep=True,
                    cwd=world.project,
                )
                assert child is not None and not child.error
                if queued:
                    assert child.queued
                    assert manager._queue[0]["crew"] == "worker"
                    manager._running_count = 0
                    manager._drain_queue()
                    await async_wait_until(
                        lambda: child.id in manager._tasks,
                        describe=lambda: f"queue={manager._queue!r}",
                    )
                    child = manager._agents[child.id]
                await await_until(manager._tasks[child.id], what="the child run finishing")
                assert not child.error
                provider = world.made[-1]
                assert provider.template == (override or generation)
                assert provider.crew_agent == ("" if override else "worker")
                assert await asyncio.to_thread(sp.read_run_agent_selection, child.id) == (
                    "member",
                    "worker",
                )
                await _close_all(sessions)
                sessions, manager = world.new_manager()
                followup = manager.continue_conversation(
                    child.id,
                    "continue member task",
                    cwd=world.project,
                )
                assert followup is not None and not followup.error
                await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
                assert not followup.error
                assert world.made[-1].crew_agent == "worker"
                assert world.made[-1].template == generation
                assert await asyncio.to_thread(sp.read_run_agent_selection, followup.id) == (
                    "member",
                    "worker",
                )
        finally:
            manager._running_count = 0
            await _close_all(sessions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", ["", "worker"])
    async def test_http_named_crew_reaches_member_allocation(self, continuation_runtime, override):
        import json

        from test_handlers_messaging_coverage import _Req, _state

        from kiro_crew import subagent_persistence as sp
        from kiro_crew.config.loader import KiroCrewConfig
        from kiro_crew.dashboard.handlers.messaging import api_spawn

        world = continuation_runtime
        generation, store = await asyncio.to_thread(world.enroll_collision)

        def enable_delegation():
            cfg = KiroCrewConfig.load()
            cfg.agents["worker"].triggers = "work"
            cfg.save()

        await asyncio.to_thread(enable_delegation)
        sessions, manager = world.new_manager()
        state = _state(subagents=manager, sessions=sessions, conversation_log=world.history)
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                response = await api_spawn(
                    _Req(
                        state,
                        {
                            "task": "named member task",
                            "crew": "worker",
                            "agent": override,
                            "cwd": world.project,
                            "keep": True,
                        },
                    )
                )
                assert response.status == 200, response.text
                child = manager._agents[json.loads(response.text)["id"]]
                await await_until(manager._tasks[child.id], what="the child run finishing")
                assert not child.error
                assert child.memory_store == store
                assert world.made[-1].template == (override or generation)
                assert world.made[-1].crew_agent == ("" if override else "worker")
                assert await asyncio.to_thread(sp.read_run_agent_selection, child.id) == (
                    "member",
                    "worker",
                )
        finally:
            await _close_all(sessions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", ["", "worker"])
    @pytest.mark.parametrize("fault", ["removed", "rebound", "governance"])
    async def test_queued_named_crew_revalidates_identity(
        self, continuation_runtime, monkeypatch, override, fault
    ):
        from kiro_crew.config.loader import KiroCrewConfig

        world = continuation_runtime
        _generation, store = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                manager._running_count = manager.max_concurrent
                queued = manager.spawn(
                    "queued member task",
                    crew="worker",
                    agent=override,
                    memory_store=store,
                    cwd=world.project,
                )
                assert queued is not None and queued.queued and not queued.error
                params = manager._queue.pop(0)
                if fault == "governance":
                    monkeypatch.setattr(
                        "kiro_crew.subagent._vet_spawn_governance",
                        lambda parent, agent, **kw: "member denied" if agent == "worker" else "",
                    )
                else:

                    def change_member():
                        cfg = KiroCrewConfig.load()
                        if fault == "removed":
                            del cfg.agents["worker"]
                        else:
                            cfg.agents["worker"].memory_store = world.store
                        cfg.save()

                    await asyncio.to_thread(change_member)
                manager._running_count = 0
                child = manager.spawn(**params, _from_queue=True)
                assert child is not None
                if child.id in manager._tasks:
                    await await_until(manager._tasks[child.id], what="the child run finishing")
                if fault == "governance" or (fault == "removed" and not override):
                    assert child.error
                    assert not world.made, "ordinary policy refusal must precede allocation"
                else:
                    assert not child.error
                    assert child.memory_store == store
                    assert child.execution_context.store.store_id == store
                    assert world.made[-1].template == (override or _generation)
        finally:
            manager._running_count = 0
            await _close_all(sessions)

    @pytest.mark.asyncio
    async def test_initial_implicit_parent_keeps_crew_alias_resolution(self, continuation_runtime):
        world = continuation_runtime
        generation, peer_store = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        from kiro_crew.member_memory_auth import bind_private_session_store

        parent = "slack:C123:123.456"
        await asyncio.to_thread(bind_private_session_store, parent, peer_store)
        await asyncio.to_thread(world.history.update_metadata, parent, {"memory_store": peer_store})
        try:
            await await_until(
                asyncio.create_task(
                    sessions.get_or_create(parent, agent="worker", cwd=world.project)
                ),
                what="the parent session starting",
            )
            sessions.release(parent)
            assert sessions.get_agent(parent) == "worker"
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                child = manager.spawn(
                    "inherit the parent",
                    parent_session_key=parent,
                    memory_store=peer_store,
                    keep=True,
                    cwd=world.project,
                )
                assert child is not None and not child.error
                await await_until(manager._tasks[child.id], what="the child run finishing")
                assert not child.error
            assert world.made[-1].template == generation
            assert world.made[-1].crew_agent == "worker"
        finally:
            await _close_all(sessions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fallback", [False, True], ids=["dedicated", "shared-fallback"])
    @pytest.mark.parametrize("template_model", ["", "template-model"])
    @pytest.mark.parametrize("override", [None, "caller-model"])
    async def test_literal_template_does_not_borrow_member_model(
        self, continuation_runtime, monkeypatch, fallback, template_model, override
    ):
        import json

        from kiro_crew.config.loader import KiroCrewConfig

        world = continuation_runtime
        await asyncio.to_thread(world.enroll_collision)

        def configure_models():
            cfg = KiroCrewConfig.load()
            cfg.agents["worker"].model = "member-model"
            cfg.agent.model = "global-model"
            cfg.save()
            path = world.specs / "worker.json"
            spec = json.loads(path.read_text(encoding="utf-8"))
            spec["model"] = template_model
            path.write_text(json.dumps(spec), encoding="utf-8")
            return cfg

        cfg = await asyncio.to_thread(configure_models)
        sessions, manager = world.new_manager()
        shared = AsyncMock(side_effect=RuntimeError("parent runtime unavailable"))
        if fallback:
            monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: True)
            monkeypatch.setattr(manager, "_create_shared_session", shared)
        try:
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                child = manager.spawn(
                    "use the selected template and its model",
                    agent="worker",
                    model=override or "",
                    cwd=world.project,
                    keep=not fallback,
                )
                assert child is not None and not child.error
                await await_until(manager._tasks[child.id], what="the child run finishing")
                assert not child.error
            if fallback and not override:
                shared.assert_awaited_once()
            else:
                shared.assert_not_awaited()
            provider = world.made[-1]
            assert provider.template == "worker" and provider.crew_agent == ""
            assert provider.messages
            # The session ladder resolves the template's own pin before the factory.
            expected_override = override or template_model or "global-model"
            assert provider.model_override == expected_override
            # Exercise the same remaining model tier used by the real factory.
            resolved = await asyncio.to_thread(
                cfg.acp_effective_model, provider.template, provider.model_override
            )
            assert resolved == (override or template_model or "global-model")
        finally:
            await _close_all(sessions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("restart", [False, True], ids=["live-manager", "restart"])
    async def test_member_continuation_adopts_generation_after_template_override(
        self, continuation_runtime, monkeypatch, restart
    ):
        from kiro_crew import subagent_persistence as sp
        from kiro_crew.member_memory_auth import (
            bind_private_session_store,
            private_memory_store_for_session,
        )
        from kiro_crew.subagent import _vet_spawn_governance

        world = continuation_runtime
        governance = MagicMock(wraps=_vet_spawn_governance)
        monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", governance)
        generation, store = await asyncio.to_thread(world.enroll_collision)
        sessions, manager = world.new_manager()
        parent = "slack:C123:123.456"
        await asyncio.to_thread(bind_private_session_store, parent, store)
        await asyncio.to_thread(world.history.update_metadata, parent, {"memory_store": store})
        try:
            await await_until(
                asyncio.create_task(
                    sessions.get_or_create(parent, agent="worker", cwd=world.project)
                ),
                what="the parent session starting",
            )
            sessions.release(parent)
            assert sessions.get_agent_selection(parent) == ("member", "worker")
            # A member must reach dedicated preparation even if a sharing
            # eligibility result is stale after capability enrollment.
            shared = AsyncMock(side_effect=AssertionError("member bypassed preparation"))
            monkeypatch.setattr(manager, "_should_use_session_sharing", lambda info: True)
            monkeypatch.setattr(manager, "_create_shared_session", shared)
            with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
                original = manager.spawn(
                    "member task", parent_session_key=parent, memory_store=store, cwd=world.project
                )
                assert original is not None and not original.error
                await await_until(manager._tasks[original.id], what="the original run finishing")
                assert not original.error
                shared.assert_not_awaited()
                first = world.made[-1]
                sid = first.session_id
                assert first.template == generation and first.crew_agent == "worker"
                assert first.capability_stamp.member == "worker"
                assert first.capability_stamp.template == generation
                governance.assert_any_call(parent, "worker", app="")
                assert await asyncio.to_thread(sp.read_run_agent_selection, original.id) == (
                    "member",
                    "worker",
                )
                updated = await asyncio.to_thread(world.update_member)
                assert updated != generation
                target = original
                for override in ("worker", ""):
                    if restart:
                        await _close_all(sessions)
                        sessions, manager = world.new_manager()
                    key = f"subagent:{target.id}"
                    followup = manager.continue_conversation(
                        target.id,
                        "member follow-up",
                        parent_session_key=parent,
                        agent=override,
                        cwd=world.project,
                    )
                    assert followup is not None and not followup.error
                    await await_until(
                        manager._tasks[followup.id], what="the follow-up run finishing"
                    )
                    assert not followup.error
                    provider = world.made[-1]
                    assert provider.key == followup.conversation_key == key
                    assert provider.resume_sid == provider.session_id == sid
                    assert provider.template == ("worker" if override else updated)
                    assert provider.crew_agent == ("" if override else "worker")
                    assert provider.messages
                    if override:
                        assert provider.capability_stamp is None
                    else:
                        assert provider.capability_stamp.member == "worker"
                        assert provider.capability_stamp.template == updated
                        assert provider.capability_stamp.session_id == sid
                    # Policy names the selected identity, never its generated
                    # runtime artifact. The explicit override also names worker.
                    scoped_targets = [
                        call.args[1] for call in governance.call_args_list if call.args[1]
                    ]
                    assert scoped_targets and set(scoped_targets) == {"worker"}
                    assert await asyncio.to_thread(sp.read_run_agent_selection, followup.id) == (
                        "member",
                        "worker",
                    )
                    assert await asyncio.to_thread(private_memory_store_for_session, key) == store
                    assert await asyncio.to_thread(sp.read_run_memory_store, followup.id) == store
                    target = followup
        finally:
            await _close_all(sessions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fault", ["removed", "rebound", "governance"])
    async def test_retained_member_cannot_fall_back_or_change_authority(
        self, continuation_runtime, monkeypatch, fault
    ):
        from kiro_crew import subagent_persistence as sp
        from kiro_crew.config.loader import KiroCrewConfig

        world = continuation_runtime
        _, store = await asyncio.to_thread(world.enroll_collision)
        await asyncio.to_thread(sp.create_agent_folder, "member-run", memory_store=store)
        await asyncio.to_thread(sp.write_run_agent, "member-run", "worker", kind="member")
        authority = await asyncio.to_thread(sp.read_run_execution, "member-run")
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        manager._ctx_builder.conversation_log = world.history
        if fault == "governance":
            monkeypatch.setattr(
                "kiro_crew.subagent._vet_spawn_governance",
                lambda parent, agent, app="": "member denied" if agent == "worker" else None,
            )
        else:

            def change_member():
                cfg = KiroCrewConfig.load()
                if fault == "removed":
                    del cfg.agents["worker"]
                else:
                    cfg.agents["worker"].memory_store = world.store
                cfg.save()

            await asyncio.to_thread(change_member)
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            followup = manager.continue_conversation("member-run", "follow-up", cwd=world.project)
            assert followup is not None
            if not followup.done:
                await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
        if fault == "rebound":
            assert not followup.error
            assert followup.memory_store == store
        else:
            assert (
                "member denied" if fault == "governance" else "memory_unavailable:"
            ) in followup.error
            sessions.get_or_create.assert_not_awaited()
        assert await asyncio.to_thread(sp.read_run_execution, "member-run") == authority

    @pytest.mark.asyncio
    async def test_missing_member_never_becomes_same_named_template(self, continuation_runtime):
        from kiro_crew import subagent_persistence as sp

        world = continuation_runtime
        # worker.json exists, but worker is not a configured member.
        from kiro_crew.execution_context import ExecutionContext, MemoryStoreRef

        execution = ExecutionContext(
            None, MemoryStoreRef("default"), "member", "worker", selection_name="worker"
        )
        await asyncio.to_thread(sp.create_agent_folder, "member-run", execution_context=execution)
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        manager._ctx_builder.conversation_log = world.history
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            followup = manager.continue_conversation("member-run", "follow-up", cwd=world.project)
            assert followup is not None and followup.done
        assert "selected member is unavailable" in followup.error
        sessions.get_or_create.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("override", [None, "caller-model"])
    async def test_unenrolled_member_keeps_its_model_pin(self, continuation_runtime, override):
        from kiro_crew.config.loader import KiroCrewAgentConfig, KiroCrewConfig

        world = continuation_runtime

        def configure_member():
            cfg = KiroCrewConfig.load()
            cfg.agents["worker"] = KiroCrewAgentConfig(
                kiro_agent="member-parent", model="member-model"
            )
            cfg.agent.model = "global-model"
            cfg.save()

        await asyncio.to_thread(configure_member)
        sessions, _ = world.new_manager()
        try:
            provider, _, _ = await await_until(
                asyncio.create_task(
                    sessions.get_or_create(
                        "member-session",
                        agent="member-parent",
                        crew_agent="worker",
                        cwd=world.project,
                        model=override,
                    )
                ),
                what="the member session starting",
            )
            assert provider.template == "member-parent"
            assert provider.model_override == (override or "member-model")
            assert sessions.get_agent_selection("member-session") == ("member", "worker")
            sessions.release("member-session")
        finally:
            await _close_all(sessions)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("legacy", ["worker", ""])
    async def test_legacy_namespace_never_becomes_new_template_authority(self, legacy):
        import json

        from kiro_crew import subagent_persistence as sp

        directory = await asyncio.to_thread(sp.create_agent_folder, "legacy", agent=legacy)
        path = directory / "state.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state.pop("execution_context")
        path.write_text(json.dumps(state), encoding="utf-8")
        sessions = _mock_sessions(resumed=True)
        manager = _manager(sessions)
        manager._spawn_stagger_secs = 0
        with (
            patch("kiro_crew.subagent.Stats"),
            patch("kiro_crew.subagent.sel"),
            patch(
                "kiro_crew.subagent._validate_agent", side_effect=lambda name, cwd: (name, "", "")
            ),
        ):
            target = "legacy"
            for override in ("", "other-worker", ""):
                followup = manager.continue_conversation(target, "next turn", agent=override)
                assert followup is not None and not followup.error
                await await_until(manager._tasks[followup.id], what="the follow-up run finishing")
                assert not followup.error
                assert sessions.get_or_create.call_args.kwargs["agent"] == (
                    override or legacy or None
                )
                assert await asyncio.to_thread(sp.read_run_agent_selection, followup.id) == (
                    "template",
                    legacy,
                )
                target = followup.id
                manager = _manager(sessions)
                manager._spawn_stagger_secs = 0


@pytest.mark.asyncio
@pytest.mark.parametrize("valid_metadata", [True, False], ids=["verified", "mismatch"])
async def test_channel_spawn_carries_verified_member_store(continuation_runtime, valid_metadata):
    from kiro_crew.member_memory_auth import bind_private_session_store
    from kiro_crew.messaging.commands import _spawn_off_loop

    world = continuation_runtime
    generation, store = await asyncio.to_thread(world.enroll_collision)
    sessions, manager = world.new_manager()
    parent = "slack:C123:456.789"
    await asyncio.to_thread(bind_private_session_store, parent, store)
    await asyncio.to_thread(world.history.update_metadata, parent, {"memory_store": store})
    try:
        await await_until(
            asyncio.create_task(sessions.get_or_create(parent, agent="worker", cwd=world.project)),
            what="the parent session starting",
        )
        sessions.release(parent)
        assert sessions.get_agent_selection(parent) == ("member", "worker")
        if not valid_metadata:
            await asyncio.to_thread(
                world.history.update_metadata, parent, {"memory_store": "default"}
            )
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            child = await _spawn_off_loop(manager, "delegated task", parent)
            assert child is not None and not child.error
            await await_until(manager._tasks[child.id], what="the child run finishing")
        assert not child.error
        assert child.memory_store == store
        assert world.made[-1].template == generation
        assert world.made[-1].crew_agent == "worker"
        assert world.made[-1]._private_memory
    finally:
        await await_until(
            asyncio.create_task(sessions.close_all(drain_timeout=0)), what="the sessions closing"
        )
