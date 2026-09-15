"""Headless recognition uses live ownership, never a key prefix or saved run."""

import asyncio
import json
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request
from test_session import _alive_provider_factory
from test_subagent_turn_identity import _child

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.config import KiroCrewConfig
from kiro_crew.dashboard.handlers import cron, memory_member
from kiro_crew.history import is_incognito_transcript
from kiro_crew.member_memory_auth import issue_member_session_proof
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.session import SessionManager
from kiro_crew.subagent import SubagentInfo, SubagentManager

env = _member_env


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["subagent:private-child", "subagent:original-child", "wf-pool:wf_900002:0"]
)
async def test_registered_child_private_memory_and_teardown(env, tmp_path, monkeypatch, key):
    """Real process publisher/proof and manager lifecycle; no sandbox proof mock."""
    from kiro_crew import context

    monkeypatch.setattr(context, "_vector_stores", dict(env.tiers))
    monkeypatch.setattr(context, "_memory_stores", {})
    env.state.push_refresh = lambda _resource: None
    with ExitStack() as stack:
        child = _child(stack, tmp_path)
        factory = _alive_provider_factory()

        def provider_factory(session_key, **kwargs):
            provider = factory(session_key, **kwargs)
            provider.client = SimpleNamespace(_pid=child.pid)
            # Model transport is a stub, not evidence of kernel isolation.
            # The actual PID publication and private proof below are unpatched.
            provider._private_memory = True
            return provider

        sessions = SessionManager(KiroCrewConfig(), provider_factory=provider_factory)
        env.state.sessions = sessions
        manager = SubagentManager(sessions=sessions, ctx_builder=SimpleNamespace())
        env.state.subagents = manager
        if key.startswith("subagent:"):
            from kiro_crew.subagent_persistence import create_agent_folder

            info = SubagentInfo(
                id=key.split(":", 1)[1], task="test", parent_session_key="dashboard:alice"
            )
            manager._agents[info.id] = info
            await asyncio.to_thread(create_agent_folder, info.id, memory_store="member-alice")
        else:
            from kiro_crew.workflow_memory import publish_binding

            # Seed the admitted run identity; kernel admission is exercised in E2E.
            await asyncio.to_thread(publish_binding, "wf_900002", "member-alice", "dashboard:alice")
        await asyncio.to_thread(env.bind_session, key, "member-alice")
        try:
            await sessions.get_or_create(key)
            await publish_turn_identity(sessions, key)
            proof = issue_member_session_proof(key, child.pid)
            assert proof
            assert sessions.has_session(key)
            assert not sessions.has_session(key.split(":", 1)[1])
            response = await cron.api_lessons_create(
                make_request(
                    env.state,
                    "/api/lessons",
                    body={"rule": "child private lesson", "category": "knowledge"},
                    session=key,
                    internal=True,
                    proof=proof,
                )
            )
            assert response.status == 200, response.text
            response = await memory_member.api_memory_recall(
                make_request(
                    env.state,
                    "/api/memory/recall",
                    query={"q": "child private lesson"},
                    session=key,
                    internal=True,
                    proof=proof,
                )
            )
            assert response.status == 200, response.text
            assert "child private lesson" in response.text
            # Recognized does not mean authorized: a key alone is not private proof.
            response = await memory_member.api_memory_recall(
                make_request(
                    env.state,
                    "/api/memory/recall",
                    query={"q": "child private lesson"},
                    session=key,
                    internal=True,
                )
            )
            assert response.status == 403
            assert json.loads(response.text)["code"] == "member_session_unverified"
            sessions.release(key, cleanup=False)
            await sessions.destroy(key)
            assert not sessions.has_session(key)
            response = await cron._recognize_session(
                env.state, key, "test", blocks_persisted_mode=is_incognito_transcript
            )
            assert response is not None and response.status == 400
            assert json.loads(response.text)["code"] == "unknown_session"
            # A retry/continuation reuses its full original key, but only a new
            # live allocation restores recognition; the binding alone did not.
            await sessions.get_or_create(key)
            await publish_turn_identity(sessions, key)
            response = await cron._recognize_session(
                env.state, key, "test", blocks_persisted_mode=is_incognito_transcript
            )
            assert response is None
            sessions.release(key, cleanup=False)
            foreign = await memory_member.api_memory_recall(
                make_request(
                    env.state,
                    "/api/memory/recall",
                    query={"q": "child private lesson"},
                    session="dashboard:alice",
                    internal=True,
                    proof=proof,
                )
            )
            assert foreign.status == 403
            assert json.loads(foreign.text)["code"] == "member_session_unverified"
        finally:
            await sessions.close_all()


@pytest.mark.asyncio
async def test_shared_handle_recognition_ends_at_unregister(tmp_path):
    """Use the real runtime registry/handle destructor without launching a model."""
    sessions = SessionManager(KiroCrewConfig())
    manager = SubagentManager(sessions=sessions, ctx_builder=SimpleNamespace())
    key = "subagent:shared-child"
    runtime = AcpRuntime(work_dir=tmp_path)
    # Only process transport is substituted; registration/destruction stay real.
    runtime._process = SimpleNamespace(returncode=None)
    runtime._dead = False
    runtime.terminate_session = AsyncMock(wraps=runtime.terminate_session)
    runtime.send_notification = AsyncMock()
    runtime._send_and_await = AsyncMock(return_value={})
    queue = asyncio.Queue()
    runtime._session_queues["native-child"] = queue
    handle = AcpSessionHandle("native-child", queue, runtime)
    handle.keep_transcript = True
    runtime.create_session = AsyncMock(return_value=handle)
    sessions.get_subagent_runtime = AsyncMock(return_value=runtime)
    info = SubagentInfo(
        id="shared-child", task="test", parent_session_key="dashboard:parent", cwd=str(tmp_path)
    )
    manager._agents[info.id] = info
    provider = await manager._create_shared_session(info, key, "kirocrew")
    runtime.create_session.assert_awaited_once_with(
        cwd=str(tmp_path), agent="kirocrew", session_key=key
    )
    state = SimpleNamespace(
        sessions=sessions,
        subagents=manager,
        _slots={"parent": SimpleNamespace(is_restricted=False, blocks_reads=False)},
        _restricted_keys=set(),
    )
    try:
        assert not sessions.has_session(key), "Shared handles are not SessionManager entries"
        response = await cron._recognize_session(
            state, key, "test", blocks_persisted_mode=is_incognito_transcript
        )
        assert response is None
        for mode in ("incognito", "temporary"):
            info.memory_mode = mode
            denied = await cron._recognize_session(
                state, key, "learn_add", blocks_persisted_mode=is_incognito_transcript
            )
            assert denied is not None and denied.status == 403
            reads = await cron._recognize_session(
                state,
                key,
                "memory.recall",
                blocks_persisted_mode=lambda value: value == "temporary",
            )
            assert (reads is not None) == (mode == "temporary")
        info.memory_mode = "persistent"
        assert not manager.has_live_shared_session("subagent:forged")
        assert not manager.has_live_shared_session(info.parent_session_key)
        info.done = True
        assert not manager.has_live_shared_session(key)
        info.done = False
        info.reaped = True
        assert not manager.has_live_shared_session(key)
        info.reaped = False
        runtime._dead = True
        assert not manager.has_live_shared_session(key)
        runtime._dead = False
        runtime._session_queues[handle.session_id] = asyncio.Queue()
        assert not manager.has_live_shared_session(key)
        runtime._session_queues[handle.session_id] = queue
        assert manager.has_live_shared_session(key)
        # Runtime liveness alone cannot keep a destroyed child authorized.
        await provider.shutdown()
        assert runtime.is_alive()
        assert "native-child" not in runtime._session_queues
        response = await cron._recognize_session(
            state, key, "test", blocks_persisted_mode=is_incognito_transcript
        )
        assert response is not None and response.status == 400
    finally:
        runtime.unregister_session("native-child")
        await sessions.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key", ["subagent:forged", "wf:forged", "wf-pool:forged:0", "unregistered"]
)
async def test_unknown_headless_keys_refuse(tmp_path, key):
    state = SimpleNamespace(
        sessions=SessionManager(KiroCrewConfig()), _slots={}, _restricted_keys=set()
    )
    response = await cron._recognize_session(
        state, key, "test", blocks_persisted_mode=is_incognito_transcript
    )
    assert response is not None and response.status == 400
    assert json.loads(response.text)["code"] == "unknown_session"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_live_registration_does_not_override_restricted_policy(env, mode):
    key = "subagent:restricted-child"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_alive_provider_factory())
    env.state.sessions = sessions
    env.state._restricted_keys.add(key)
    env.state._slots["restricted-child"] = SimpleNamespace(
        is_restricted=True, blocks_reads=mode == "temporary"
    )
    try:
        await sessions.get_or_create(key)
        response = await cron.api_lessons_create(
            make_request(
                env.state,
                "/api/lessons",
                body={"rule": "must not save", "category": "knowledge"},
                session=key,
            )
        )
        assert response.status == 403
        assert "not allowed" in json.loads(response.text)["error"]
    finally:
        sessions.release(key, cleanup=False)
        await sessions.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_live_archived_dashboard_still_checks_persisted_mode(env, mode):
    key = "dashboard:archived"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_alive_provider_factory())
    env.state.sessions = sessions
    await asyncio.to_thread(env.history.update_metadata, key, {"memory_mode": mode})
    try:
        await sessions.get_or_create(key)
        response = await cron._recognize_session(
            env.state, key, "test", blocks_persisted_mode=is_incognito_transcript
        )
        assert response is not None and response.status == 403
        assert json.loads(response.text)["code"] == "restricted_session"
    finally:
        sessions.release(key, cleanup=False)
        await sessions.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary", "persistent"])
@pytest.mark.parametrize("kind", ["subagent", "continued", "nested", "workflow"])
async def test_live_child_checks_origin_privacy_without_child_markers(env, mode, kind):
    """A real live allocation and its originating run must not erase parent policy."""
    from kiro_crew.workflow_memory import WorkflowScope

    parent = "dashboard:parent"
    env.state._slots["parent"] = SimpleNamespace(
        is_restricted=mode != "persistent", blocks_reads=mode == "temporary"
    )
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_alive_provider_factory())
    manager = SubagentManager(sessions=sessions, ctx_builder=SimpleNamespace())
    env.state.sessions = sessions
    env.state.subagents = manager
    if kind != "workflow":
        info = SubagentInfo(
            id="privacy-child", task="test", parent_session_key=parent, memory_mode=mode
        )
        if kind == "continued":
            info.conversation_key = "subagent:original-conversation"
        elif kind == "nested":
            ancestor = SubagentInfo(
                id="finished-parent", task="test", parent_session_key=parent, done=True
            )
            manager._agents[ancestor.id] = ancestor
            info.parent_session_key = f"subagent:{ancestor.id}"
        manager._agents[info.id] = info
        key = info.conversation_key or f"subagent:{info.id}"
    else:
        from kiro_crew.dashboard.handlers._shared import resolve_session_memory_mode

        admitted_context = SimpleNamespace(
            _session_memory_modes={},
            memory_mode_for_session=lambda key: resolve_session_memory_mode(env.state, key),
        )
        scope = await WorkflowScope.admit("wf_900001", admitted_context, parent)
        key = scope.worker_key("privacy-child")
    assert key not in env.state._restricted_keys
    assert key.split(":", 1)[1] not in env.state._slots
    try:
        await sessions.get_or_create(key)
        for read_only in (False, True):
            response = await cron._recognize_session(
                env.state,
                key,
                "memory.recall" if read_only else "learn_add",
                blocks_persisted_mode=(
                    (lambda value: value == "temporary") if read_only else is_incognito_transcript
                ),
            )
            blocked = mode == "temporary" or (mode == "incognito" and not read_only)
            if blocked:
                assert response is not None and response.status == 403
                assert json.loads(response.text)["code"] == "restricted_session"
                if not read_only:
                    written = await cron.api_lessons_create(
                        make_request(
                            env.state,
                            "/api/lessons",
                            body={"rule": "private child rule must not persist"},
                            session=key,
                        )
                    )
                    assert written.status == 403
            else:
                assert response is None
    finally:
        sessions.release(key, cleanup=False)
        await sessions.close_all()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin_state", ["missing", "corrupt", "archived", "parentless"])
async def test_headless_birth_authority_survives_origin_loss(env, origin_state):
    parent = "dashboard:archived-parent"
    sessions = SessionManager(KiroCrewConfig(), provider_factory=_alive_provider_factory())
    manager = SubagentManager(sessions=sessions, ctx_builder=SimpleNamespace())
    env.state.sessions = sessions
    env.state.subagents = manager
    run_id = f"origin-check-{origin_state}"
    key = f"subagent:{run_id}"
    if origin_state != "missing":
        from kiro_crew.subagent_persistence import _run_memory_identity_path, create_agent_folder

        await asyncio.to_thread(
            create_agent_folder,
            run_id,
            memory_mode="persistent" if origin_state == "parentless" else "temporary",
        )
        if origin_state == "archived":
            await asyncio.to_thread(
                env.history.update_metadata, parent, {"memory_mode": "persistent"}
            )
    try:
        await sessions.get_or_create(key)
        if origin_state == "corrupt":
            _run_memory_identity_path(run_id).write_text("not json", encoding="utf-8")
        response = await cron._recognize_session(
            env.state, key, "learn_add", blocks_persisted_mode=is_incognito_transcript
        )
        if origin_state == "parentless":
            assert response is None
        else:
            assert response is not None and response.status == 403
            assert json.loads(response.text)["code"] == "restricted_session"
    finally:
        sessions.release(key, cleanup=False)
        await sessions.close_all()


@pytest.mark.asyncio
@pytest.mark.usefixtures("healthy_host_memory")
@pytest.mark.parametrize("mode", ["persistent", "incognito", "temporary"])
@pytest.mark.parametrize("parent_change", ["replace", "close"])
async def test_gateway_spawn_freezes_mode_and_enforces_it_after_parent_change(
    env, mode, parent_change
):
    from kiro_crew.dashboard.handlers import messaging
    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.subagent_persistence import read_run_memory_mode

    sessions = SessionManager(KiroCrewConfig(), provider_factory=_alive_provider_factory())
    manager = SubagentManager(sessions=sessions, ctx_builder=None, is_yolo=lambda: True)
    state = DashboardState(sessions, None, None, 0, subagents=manager, conversation_log=env.history)
    parent = "dashboard:parent"
    state._slots["parent"] = SimpleNamespace(
        is_restricted=mode != "persistent", blocks_reads=mode == "temporary"
    )
    ready, finish = asyncio.Event(), asyncio.Event()

    async def model_transport(info):
        key = f"subagent:{info.id}"
        await sessions.get_or_create(key)
        ready.set()
        await finish.wait()

    manager._run = model_transport
    try:
        response = await messaging.api_spawn(
            make_request(
                state,
                "/api/spawn",
                body={
                    "task": "mode evidence",
                    "parent_session": parent,
                    "_memory_mode": "persistent",
                },
                session=parent,
            )
        )
        assert response.status == 200, response.text
        await asyncio.wait_for(ready.wait(), timeout=5)
        run_id = json.loads(response.text)["id"]
        key = f"subagent:{run_id}"
        assert read_run_memory_mode(run_id) == mode
        if parent_change == "replace":
            state._slots["parent"] = SimpleNamespace(is_restricted=False, blocks_reads=False)
        else:
            del state._slots["parent"]
        refusal = await cron._recognize_session(
            state, key, "learn_add", blocks_persisted_mode=is_incognito_transcript
        )
        assert (refusal is not None) == (mode != "persistent")
        if mode != "persistent":
            written = await cron.api_lessons_create(
                make_request(
                    state,
                    "/api/lessons",
                    body={"rule": "must not persist"},
                    session=key,
                )
            )
            assert written.status == 403
        read_refusal = await cron._recognize_session(
            state, key, "memory.recall", blocks_persisted_mode=lambda value: value == "temporary"
        )
        assert (read_refusal is not None) == (mode == "temporary")
    finally:
        finish.set()
        await asyncio.wait_for(
            asyncio.gather(*manager._tasks.values(), return_exceptions=True), timeout=5
        )
        await sessions.close_all()
