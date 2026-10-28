"""A revocation reaches sub-agents that are already running.

A child reads its parent's stored ``approval_policy`` once, when its run
starts. Revoking the parent's trust clears that stored value, but a child that
read ``"auto"`` before the revocation must not keep auto-approving its tool
calls. The session store therefore counts revocations per session key, and the
child's ``parent_policy=auto`` grant re-checks that count on every request.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_helpers import _mock_sessions

from kiro_crew.execution_context import execution_for_store

PARENT = "dashboard:default"


def _store(*keys: str):
    """A real ``SessionManager`` holding one resident session per key, each trusted."""
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.session import SessionManager, _Session

    store = SessionManager(KiroCrewConfig())
    for key in keys:
        store._sessions[key] = _Session(provider=SimpleNamespace(session_id=key), agent="")
        store.set_approval_policy(key, "auto")
    return store


@pytest.fixture(autouse=True)
def _quiet_sel():
    with patch("kiro_crew.session.sel"):
        yield


class TestRevocationGeneration:
    def test_leaving_auto_counts_one_revocation(self) -> None:
        store = _store(PARENT)
        assert store.approval_revocation_generation(PARENT) == 0

        store.set_approval_policy(PARENT, "")

        assert store.approval_revocation_generation(PARENT) == 1

    def test_writes_that_do_not_leave_auto_count_nothing(self) -> None:
        store = _store(PARENT)
        store.set_approval_policy(PARENT, "auto")
        store.set_approval_policy(PARENT, "")
        store.set_approval_policy(PARENT, "")
        store.set_approval_policy(PARENT, "auto")

        assert store.approval_revocation_generation(PARENT) == 1

    def test_an_unknown_key_reads_zero(self) -> None:
        assert _store().approval_revocation_generation("dashboard:nobody") == 0

    def test_a_revocation_after_eviction_still_counts(self) -> None:
        """A child can outlive its parent's resident session; a revoke then must count."""
        store = _store(PARENT)
        del store._sessions[PARENT]

        store.set_approval_policy(PARENT, "")

        assert store.approval_revocation_generation(PARENT) == 1


def _permission(request_id: int):
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent

    return LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id=request_id, title="read")


def _manager(store, parent_key: str, revoke_key: str, agent_id: str = "kid01"):
    """A child of *parent_key* whose stream revokes *revoke_key* between two requests."""
    from kiro_crew.hooks import ToolHookResult
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    sessions = _mock_sessions()
    sessions.get_approval_policy = store.get_approval_policy
    sessions.approval_revocation_generation = store.approval_revocation_generation
    provider = sessions.get_or_create.return_value[0]

    async def _stream(*_a, **_kw):
        yield _permission(1)
        store.set_approval_policy(revoke_key, "")
        yield _permission(2)

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()

    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("msg", None))
    # ALLOW, not auto-approve: any approval has to come from parent_policy=auto.
    ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult.allow())
    ctx.hooks.auto_approve_subagent_tools = False

    manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=10)
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id=agent_id,
        task="t",
        parent_session_key=parent_key,
    )
    manager._log_spawned(info)
    manager._agents[agent_id] = info
    return manager, info, provider


async def _run(manager, info) -> None:
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state"),
        patch("kiro_crew.subagent.create_agent_folder", MagicMock(), create=True),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")


class TestRunningChildSeesRevocation:
    @pytest.mark.asyncio
    async def test_child_stops_auto_approving_once_the_parent_is_revoked(self) -> None:
        store = _store(PARENT)
        manager, info, provider = _manager(store, PARENT, revoke_key=PARENT)

        await _run(manager, info)

        provider.approve_tool.assert_awaited_once_with(1)
        provider.reject_tool.assert_awaited_once_with(2)

    @pytest.mark.asyncio
    async def test_child_keeps_auto_when_nothing_is_revoked(self) -> None:
        """Counterfactual: the same stream revoking an unrelated session."""
        store = _store(PARENT, "dashboard:other")
        manager, info, provider = _manager(store, PARENT, revoke_key="dashboard:other")

        await _run(manager, info)

        assert [c.args for c in provider.approve_tool.await_args_list] == [(1,), (2,)]
        provider.reject_tool.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_grandchild_stops_once_the_root_is_revoked(self) -> None:
        """A grandchild's parent is the child's session, whose stored ``"auto"`` the
        root's revocation never touches; the grandchild re-checks the root too."""
        from kiro_crew.subagent import SubagentInfo

        store = _store(PARENT, "subagent:kid01")
        manager, info, provider = _manager(
            store, "subagent:kid01", revoke_key=PARENT, agent_id="grand01"
        )
        child = SubagentInfo(
            execution_context=execution_for_store(""),
            id="kid01",
            task="t",
            parent_session_key=PARENT,
        )
        child._approval_lineage = ((PARENT, store.approval_revocation_generation(PARENT)),)
        manager._agents["kid01"] = child

        await _run(manager, info)

        provider.approve_tool.assert_awaited_once_with(1)
        provider.reject_tool.assert_awaited_once_with(2)

    @pytest.mark.asyncio
    async def test_grandchild_of_a_continuation_stops_once_the_root_is_revoked(self) -> None:
        """A ``spawn_continue`` run has a new id but runs on the original run's
        conversation key, and the original record may be evicted; the grandchild
        still finds the continuation and inherits its root record."""
        from kiro_crew.subagent import SubagentInfo

        store = _store(PARENT, "subagent:kid01")
        manager, info, provider = _manager(
            store, "subagent:kid01", revoke_key=PARENT, agent_id="grand01"
        )
        continuation = SubagentInfo(
            execution_context=execution_for_store(""),
            id="cont01",
            task="t",
            parent_session_key=PARENT,
            conversation_key="subagent:kid01",
        )
        continuation._approval_lineage = ((PARENT, store.approval_revocation_generation(PARENT)),)
        manager._agents["cont01"] = continuation
        assert "kid01" not in manager._agents

        await _run(manager, info)

        provider.approve_tool.assert_awaited_once_with(1)
        provider.reject_tool.assert_awaited_once_with(2)

    @pytest.mark.asyncio
    async def test_grandchild_with_no_known_parent_run_gets_no_auto(self) -> None:
        """No run owns the parent conversation, so its ancestry is unreadable."""
        store = _store(PARENT, "subagent:kid01")
        manager, info, provider = _manager(
            store, "subagent:kid01", revoke_key="dashboard:unrelated", agent_id="grand01"
        )

        await _run(manager, info)

        provider.approve_tool.assert_not_awaited()
        assert [c.args for c in provider.reject_tool.await_args_list] == [(1,), (2,)]

    @pytest.mark.asyncio
    async def test_grandchild_follows_the_newest_run_on_its_parent_key(self) -> None:
        """Root A spawned the original run; root B continued it and the continuation's
        turn ended before the grandchild started. The grandchild must follow B."""
        from kiro_crew.subagent import SubagentInfo

        root_b = "dashboard:other"
        store = _store(PARENT, root_b, "subagent:kid01")
        manager, info, provider = _manager(
            store, "subagent:kid01", revoke_key=root_b, agent_id="grand01"
        )
        original = SubagentInfo(
            execution_context=execution_for_store(""),
            id="kid01",
            task="t",
            parent_session_key=PARENT,
            started=100.0,
        )
        original.done = True
        original._approval_lineage = ((PARENT, 0),)
        continuation = SubagentInfo(
            execution_context=execution_for_store(""),
            id="cont01",
            task="t",
            parent_session_key=root_b,
            conversation_key="subagent:kid01",
            started=200.0,
        )
        continuation.done = True
        continuation._approval_lineage = ((root_b, 0),)
        manager._agents["kid01"] = original
        manager._agents["cont01"] = continuation

        await _run(manager, info)

        provider.approve_tool.assert_awaited_once_with(1)
        provider.reject_tool.assert_awaited_once_with(2)
