"""A revocation of the parent's trust reaches sub-agents that are already running.

A child reads its parent's stored ``approval_policy`` once, when its run starts. The
session store stamps a key's outstanding revocation (its last write, when that write is
not ``"auto"``), and the child's ``parent_policy=auto`` grant re-checks the stamp on each
tool request, for its own parent and for every ancestor its parent run recorded.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from subagent_helpers import _mock_sessions

from kiro_crew.execution_context import execution_for_store

ROOT = "dashboard:default"


def _store(*keys: str):
    """A real ``SessionManager`` holding one resident, trusted session per key."""
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


def _child(store, parent_key: str, between):
    """A child of *parent_key* whose stream calls *between* between two tool requests."""
    from kiro_crew.hooks import ToolHookResult
    from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
    from kiro_crew.subagent import SubagentInfo, SubagentManager

    sessions = _mock_sessions()
    sessions.get_approval_policy = store.get_approval_policy
    sessions.approval_revocation = store.approval_revocation
    provider = sessions.get_or_create.return_value[0]

    async def _stream(*_a, **_kw):
        yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id=1, title="read")
        between()
        yield LLMEvent(kind=EVENT_PERMISSION_REQUEST, request_id=2, title="read")

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _stream())
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("msg", None))
    # ALLOW, not auto-approve, and no approver: only parent_policy=auto approves.
    ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult.allow())
    ctx.hooks.auto_approve_subagent_tools = False
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx, default_turn_limit=10)
    info = SubagentInfo(
        execution_context=execution_for_store(""),
        id="grand01" if parent_key.startswith("subagent:") else "kid01",
        task="t",
        parent_session_key=parent_key,
    )
    manager._log_spawned(info)
    manager._agents[info.id] = info
    return manager, info, provider


async def _run(manager, info) -> None:
    with (
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
        patch("kiro_crew.subagent.update_state"),
        patch("kiro_crew.subagent.create_agent_folder", MagicMock(), create=True),
    ):
        await manager._run_inner(info, f"subagent:{info.id}")


def _answers(provider) -> tuple[list[int], list[int]]:
    approved = [c.args[0] for c in provider.approve_tool.await_args_list]
    return approved, [c.args[0] for c in provider.reject_tool.await_args_list]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("between", "expected"),
    [
        ("revoke", ([1], [2])),
        # A re-trust gives the grant back: the yolo ceiling's suspend-and-restore.
        ("revoke_and_retrust", ([1, 2], [])),
        ("revoke_unrelated", ([1, 2], [])),
        # An eviction reads as no policy too, but is no revocation.
        ("evict", ([1, 2], [])),
    ],
)
async def test_a_running_child_loses_auto_once_its_parent_is_revoked(between, expected) -> None:
    store = _store(ROOT, "dashboard:other")
    steps = {
        "revoke": lambda: store.set_approval_policy(ROOT, ""),
        "revoke_and_retrust": lambda: (
            store.set_approval_policy(ROOT, ""),
            store.set_approval_policy(ROOT, "auto"),
        ),
        "revoke_unrelated": lambda: store.set_approval_policy("dashboard:other", ""),
        "evict": lambda: store._sessions.pop(ROOT),
    }
    manager, info, provider = _child(store, ROOT, steps[between])

    await _run(manager, info)

    assert _answers(provider) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("grant", ["approval_mode", "auto_approve_subagent_tools"])
async def test_a_grant_of_its_own_outlives_the_parent_revocation(grant) -> None:
    """The spawn's own ``approval_mode`` or the operator's setting would grant a child
    started after the revoke, so revoking the parent does not take it from one running."""
    store = _store(ROOT)
    manager, info, provider = _child(store, ROOT, lambda: store.set_approval_policy(ROOT, ""))
    if grant == "approval_mode":
        info.approval_mode = "auto"
    else:
        manager._ctx_builder.hooks.auto_approve_subagent_tools = True

    await _run(manager, info)

    assert _answers(provider) == ([1, 2], [])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("child_lineage", "expected"),
    [
        # The child's run read the root's "auto": the grandchild inherits that pair.
        (((ROOT, 0),), ([1], [2])),
        # The run on the child's key holds no "auto" (a continuation started after
        # the revoke), so the "auto" its session was created with is not trust.
        (None, ([], [1, 2])),
    ],
)
async def test_a_grandchild_loses_auto_once_the_root_is_revoked(child_lineage, expected) -> None:
    """The grandchild's parent is the child's session, whose stored ``"auto"`` the root's
    revocation never touches."""
    from kiro_crew.subagent import SubagentInfo

    store = _store(ROOT, "subagent:kid01")
    manager, info, provider = _child(
        store, "subagent:kid01", lambda: store.set_approval_policy(ROOT, "")
    )
    child = SubagentInfo(
        execution_context=execution_for_store(""), id="kid01", task="t", parent_session_key=ROOT
    )
    child._approval_lineage = child_lineage
    manager._agents["kid01"] = child

    await _run(manager, info)

    assert _answers(provider) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("founder", "expected"),
    [
        ("dashboard:founder", ("auto", ROOT, False, True)),
        # A sub-agent founder whose run holds no "auto": what its session stores is not trust.
        ("subagent:fnd01", ("", "subagent:fnd01", False, False)),
    ],
)
async def test_a_continuation_rests_on_its_founders_trust(founder, expected) -> None:
    """A continuation from another session keeps ``"auto"`` only while the session that
    founded the conversation holds it too, so revoking that founder reaches it."""
    from kiro_crew.subagent import SubagentInfo
    from kiro_crew.subagent_manager import turn_session

    store = _store(ROOT, founder)
    manager, info, _ = _child(store, ROOT, lambda: None)
    info.conversation_key = "subagent:orig01"
    manager._agents["fnd01"] = SubagentInfo(
        execution_context=execution_for_store(""), id="fnd01", task="t", parent_session_key=ROOT
    )
    with patch.object(turn_session, "_founding_session", return_value=founder):
        policy, ask_key = await turn_session.resolve_parent_policy(manager._run_events, info)
    revoked = turn_session.parent_trust_revoked(store, info._approval_lineage)
    before = revoked is not None and revoked()

    store.set_approval_policy(founder, "")

    assert (policy, ask_key, before, revoked is not None and revoked()) == expected


def test_a_revocation_is_kept_while_evicted_under_either_slack_spelling() -> None:
    store = _store()

    store.set_approval_policy("1700000000.000100", "")
    assert store.approval_revocation("slack:1700000000.000100") != 0

    store.set_approval_policy("slack:1700000000.000100", "auto")
    assert store.approval_revocation("1700000000.000100") == 0
