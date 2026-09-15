"""Private managed memory calls retain their canonical caller across a restart."""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import MagicMock, patch

import pytest
from member_memory_helpers import env as _member_env
from member_memory_helpers import make_request, patch_private_memory_supported
from test_subagent_continuable import _manager, _mock_sessions

from kiro_crew import context, member_memory_auth, platform_compat, subagent_persistence
from kiro_crew.context import ContextBuilder
from kiro_crew.dashboard.handlers import cron, memory_member
from kiro_crew.dashboard.token_auth import token_auth_middleware

env = _member_env
pytestmark = pytest.mark.usefixtures("healthy_host_memory")


def _continuation_sessions(resumed: bool = False) -> MagicMock:
    sessions = _mock_sessions(resumed=resumed)
    provider = sessions.get_or_create.return_value[0]
    provider.context_window_tokens = MagicMock(return_value=0)
    provider.context_used_tokens = MagicMock(return_value=0)
    provider.cwd = ""
    return sessions


@pytest.mark.asyncio
@pytest.mark.parametrize("restore", ["live", "evicted", "restart"])
async def test_app_continuations_preserve_scope_through_two_runs(monkeypatch, restore) -> None:
    from test_internal_secret_app_identity_3690 import _grant

    from kiro_crew.messaging.identity import publish_turn_identity

    monkeypatch.setattr("kiro_crew.subagent._validate_agent", lambda name, cwd: (name, "", ""))
    governance = MagicMock(return_value="")
    monkeypatch.setattr("kiro_crew.subagent._vet_spawn_governance", governance)
    sessions = _continuation_sessions()
    manager = _manager(sessions)
    manager._spawn_stagger_secs = 0
    manager._memory_mode_for_session = lambda key: "incognito"
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch(
            "kiro_crew.messaging.identity.publish_turn_identity", wraps=publish_turn_identity
        ) as publisher,
    ):
        original = manager.spawn("initial task", agent="example-worker", app="example-app")
        assert original is not None and not original.error
        await asyncio.wait_for(manager._tasks[original.id], timeout=10)
        assert not original.error
        assert original.memory_mode == "incognito"
        publisher.assert_awaited_once_with(sessions, f"subagent:{original.id}")
        previous = original
        for requested_mode in ("temporary", "persistent"):
            if restore == "restart":
                sessions = _continuation_sessions(resumed=True)
                manager = _manager(sessions)
                manager._spawn_stagger_secs = 0
            elif restore == "evicted":
                manager._agents.pop(previous.id)
            manager._memory_mode_for_session = lambda key, mode=requested_mode: mode
            provider = sessions.get_or_create.return_value[0]
            sessions.get_or_create.return_value = (provider, True, True)
            claims = []
            key = f"subagent:{previous.id}"

            async def stream(*args, **kwargs):
                claims.append(await _grant(key, {}, subagents=manager._agents))
                if False:
                    yield

            provider.stream.side_effect = stream
            governance.reset_mock()
            publisher.reset_mock()
            followup = manager.continue_conversation(previous.id, "continue the task")
            assert followup is not None and not followup.error
            await asyncio.wait_for(manager._tasks[followup.id], timeout=10)
            assert not followup.error
            publisher.assert_awaited_once_with(sessions, followup.conversation_key)
            assert followup.app == "example-app"
            assert followup.memory_mode == "temporary" and followup._memory_mode_ready
            for run_id in (previous.id, followup.id):
                assert (
                    await asyncio.to_thread(subagent_persistence.read_run_memory_mode, run_id)
                    == "temporary"
                )
                assert (
                    await asyncio.to_thread(subagent_persistence.read_run_app, run_id)
                    == "example-app"
                )
            assert manager._ctx_builder.build_message.call_args.kwargs["blocks_reads"] is True
            assert claims and all(claim["app"] == "example-app" for claim in claims)
            assert governance.called
            assert all(call.kwargs["app"] == "example-app" for call in governance.call_args_list)
            previous = followup


@pytest.mark.asyncio
@pytest.mark.parametrize("agent", ["", "explicit-worker"])
async def test_unknown_app_ownership_cannot_resume_as_the_user(monkeypatch, agent) -> None:
    from kiro_crew.dashboard.token_auth import caller_record_is_missing

    monkeypatch.setattr("kiro_crew.subagent._validate_agent", lambda name, cwd: (name, "", ""))
    await asyncio.to_thread(subagent_persistence.create_agent_folder, "legacy-app")
    path = subagent_persistence._run_memory_identity_path("legacy-app")
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["app"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    # An agent-writable claim and an explicit template cannot reconstruct the
    # owner that older gateways never persisted.
    await asyncio.to_thread(subagent_persistence.update_state, "legacy-app", app="")
    sessions = _continuation_sessions(resumed=True)
    manager = _manager(sessions)
    with patch("kiro_crew.subagent.sel"):
        followup = manager.continue_conversation("legacy-app", "follow-up", agent=agent)
    assert followup is not None and followup.done
    assert followup.error.startswith("resume_failed: protected app ownership unavailable")
    sessions.get_or_create.assert_not_called()
    assert caller_record_is_missing("subagent:legacy-app", subagents=manager._agents)
    assert not manager._conversations


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_override", [True, False])
async def test_native_chained_continuations_record_the_project(
    monkeypatch, tmp_path, initial_override
) -> None:
    project = tmp_path / "chess"
    project.mkdir()
    monkeypatch.setattr("kiro_crew.subagent.validate_cwd", lambda cwd, roots: (cwd, ""))
    previous = None
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        for _ in range(3):
            sessions = _continuation_sessions(resumed=previous is not None)
            sessions._pool_cwd = str(project if previous is None else tmp_path / "other-project")
            provider = sessions.get_or_create.return_value[0]
            provider.cwd = str(project)
            provider.session_id = "retained-native-session"
            manager = _manager(sessions)
            # Match the REST caller: the resumed cwd comes from the completed
            # run on disk, after the old manager has been discarded.
            cwd = (
                await asyncio.to_thread(manager.recorded_cwd, previous.id)
                if previous is not None
                else str(project) if initial_override else ""
            )
            if previous is None:
                run = manager.spawn("audit the project", cwd=cwd)
            else:
                run = manager.continue_conversation(previous.id, "audit again", cwd=cwd)
            assert run is not None and not run.error
            await asyncio.wait_for(manager._tasks[run.id], timeout=10)
            assert not run.error
            if previous is not None or initial_override:
                assert sessions.get_or_create.call_args.kwargs["cwd"] == str(project)
            state = await asyncio.to_thread(subagent_persistence.read_state, run.id)
            assert state["cwd"] == str(project)
            assert state["session_id"] == "retained-native-session"
            previous = run


@pytest.mark.asyncio
async def test_restart_continuation_keeps_canonical_memory_caller(env, monkeypatch) -> None:
    patch_private_memory_supported(monkeypatch)
    monkeypatch.setattr(platform_compat, "get_process_start_id", lambda pid: f"test-start-{pid}")
    monkeypatch.setattr("kiro_crew.session_pid_sig._load_hmac_key", lambda: b"test-key" * 4)
    monkeypatch.setattr("kiro_crew.subagent._validate_agent", lambda name, cwd: (name, "", ""))
    monkeypatch.setattr(context, "_vector_stores", dict(env.tiers))
    monkeypatch.setattr(context, "_memory_stores", {})
    monkeypatch.setattr(context, "_lesson_stores", {})
    store = "member-alice"
    sid = "session-original"
    first_sessions = _mock_sessions()
    first_sessions.get_pid.return_value = os.getpid()
    first_provider = first_sessions.get_or_create.return_value[0]
    first_provider.session_id = sid
    first_provider.context_window_tokens = MagicMock(return_value=0)
    first_provider.context_used_tokens = MagicMock(return_value=0)
    first = _manager(first_sessions)
    first._ctx_builder.conversation_log = env.history
    first._ctx_builder.ensure_store = ContextBuilder.ensure_store

    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        original = first.spawn(
            "remember the audit",
            agent="worker",
            memory_store=store,
            parent_session_key="dashboard:owner",
            keep=True,
        )
        assert original is not None and not original.error
        await asyncio.wait_for(first._tasks[original.id], timeout=10)
    assert not original.error
    key = f"subagent:{original.id}"
    assert await asyncio.to_thread(subagent_persistence.read_run_memory_store, original.id) == store
    assert await asyncio.to_thread(member_memory_auth.read_private_session_store, key) == store

    # A new gateway has no original run in its process-local registry.
    sessions = _mock_sessions(resumed=True)
    sessions.get_pid.return_value = os.getpid()
    sessions.get_agent.return_value = "conductor"
    provider = sessions.get_or_create.return_value[0]
    provider.session_id = sid
    provider.context_window_tokens = MagicMock(return_value=0)
    provider.context_used_tokens = MagicMock(return_value=0)
    sessions.resumable_sid.return_value = sid
    live_keys = set()

    def allocate_live(session_key, **kwargs):
        live_keys.add(session_key)
        return sessions.get_or_create.return_value

    sessions.get_or_create.side_effect = allocate_live
    sessions.has_session.side_effect = live_keys.__contains__
    sessions.reset.side_effect = live_keys.discard
    restored = _manager(sessions)
    restored._ctx_builder.conversation_log = env.history
    restored._ctx_builder.ensure_store = ContextBuilder.ensure_store
    env.state.subagents = restored
    env.state.sessions = sessions
    env.state._slots = {}
    env.state.push_refresh = MagicMock()
    responses = []
    claims = []
    secret = "continuation-test-secret"
    middleware = token_auth_middleware(
        internal_paths=frozenset({"/api/lessons", "/api/memory/recall"}),
        internal_secret=secret,
    )

    async def call(path, handler, *, body=None, proof="", session=key):
        request = make_request(
            env.state,
            path,
            body=body,
            query={"q": "retained audit"} if path == "/api/memory/recall" else None,
            session=session,
            proof=proof,
        )
        request = request.clone(
            headers={**request.headers, "X-Internal-Secret": secret}, remote="127.0.0.1"
        )
        response = await middleware(request, handler)
        claims.append(request.get("app"))
        return response

    async def stream(*args, **kwargs):
        proof = await asyncio.to_thread(
            member_memory_auth.issue_member_session_proof, key, os.getpid()
        )
        assert proof
        responses.append(
            await call(
                "/api/lessons",
                cron.api_lessons_create,
                body={"rule": "the retained audit passed", "category": "knowledge"},
                proof=proof,
            )
        )
        responses.append(
            await call("/api/memory/recall", memory_member.api_memory_recall, proof=proof)
        )
        # A matching live run is not private-memory authority by itself.
        responses.append(
            await call("/api/memory/recall", memory_member.api_memory_recall, proof="invalid-proof")
        )
        if False:
            yield

    provider.stream.side_effect = stream
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        followup = restored.continue_conversation(
            original.id, "verify the audit after restart", parent_session_key="dashboard:owner"
        )
        assert followup is not None and not followup.error
        await asyncio.wait_for(restored._tasks[followup.id], timeout=10)
    assert not followup.error
    assert not live_keys
    assert original.id not in restored._agents
    assert list(restored._agents) == [followup.id]
    assert followup.conversation_key == key
    assert followup.memory_store == store
    allocation = sessions.get_or_create.call_args
    assert allocation.args[0] == key
    assert allocation.kwargs["agent"] == "worker"
    assert provider.session_id == sid
    assert await asyncio.to_thread(subagent_persistence.read_run_memory_store, followup.id) == store
    assert await asyncio.to_thread(member_memory_auth.read_private_session_store, key) == store
    assert (
        await asyncio.to_thread(
            member_memory_auth.read_private_session_store, f"subagent:{followup.id}"
        )
        is None
    )
    assert len(responses) == 3
    assert [response.status for response in responses] == [200, 200, 403], [
        response.text for response in responses
    ]
    assert "the retained audit passed" in responses[1].text
    assert json.loads(responses[2].text)["code"] == "member_session_unverified"
    assert all(claim is None for claim in claims)
    assert [json.loads(row["value_json"])["rule"] for row in env.tiers[store].get_lessons()] == [
        "the retained audit passed"
    ]
    assert env.tiers[""].get_lessons() == []
    assert env.tiers["member-bob"].get_lessons() == []

    restored._agents.pop(followup.id)
    proof = await asyncio.to_thread(member_memory_auth.issue_member_session_proof, key, os.getpid())
    denied = await call("/api/memory/recall", memory_member.api_memory_recall, proof=proof)
    assert denied.status == 403
    assert json.loads(denied.text)["code"] == "caller_record_missing"
