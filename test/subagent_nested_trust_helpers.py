"""Shared stand-ins for the nested-subagent trust suites.

``test_subagent_nested_trust.py`` (the spawn gate and run loop resolving the ROOT
chat's policy) and ``test_subagent_nested_trust_routing.py`` (the gateway routing
a nested run's prompt, cards and frames to the root tab) import these by name.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from kiro_crew.execution_context import execution_for_store
from kiro_crew.subagent import SubagentInfo, SubagentManager, contested_root

ROOT = "dashboard:chat-1"
# What conversation ``subagent:A`` resolves to once two chats have claimed it.
CONTESTED_A = contested_root("subagent:A")


def _sessions(trusted: set[str]) -> MagicMock:
    """A SessionManager stand-in whose trust is scoped to *trusted* keys only."""
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0

    async def _empty_stream(*_args: object, **_kwargs: object):  # type: ignore[no-untyped-def]
        return
        yield  # noqa: unreachable -- makes this an async generator

    provider.stream = MagicMock(side_effect=lambda *a, **kw: _empty_stream())
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
    sessions.get_approval_policy = MagicMock(
        side_effect=lambda key: "auto" if key in trusted else ""
    )
    sessions.has_session = MagicMock(side_effect=lambda key: key in trusted)
    sessions.is_session_sharing_eligible = MagicMock(return_value=False)
    return sessions


def _ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = False
    ctx.hooks.auto_approve_subagent_tools = False
    return ctx


def _live(manager: SubagentManager, agent_id: str, parent: str) -> SubagentInfo:
    """Register a running subagent, stamped the way admission stamps a fresh spawn."""
    info = SubagentInfo(
        execution_context=execution_for_store("", template_id=""),
        id=agent_id,
        task=f"task {agent_id}",
        parent_session_key=parent,
    )
    info.root_session_key = manager.root_session_key(parent)
    info.conversation_root_session_key = manager.conversation_root_for_new_run(
        "", info.root_session_key, f"subagent:{agent_id}"
    )
    manager._agents[agent_id] = info
    return info


def _continue(manager: SubagentManager, agent_id: str, parent: str, of_key: str) -> SubagentInfo:
    """Register a continuation of conversation *of_key*, stamped the way admission does."""
    info = SubagentInfo(
        execution_context=execution_for_store("", template_id=""),
        id=agent_id,
        task=f"continue {of_key}",
        parent_session_key=parent,
        conversation_key=of_key,
    )
    info.root_session_key = manager.root_session_key(parent)
    # ``spawn_async`` reads the founder's durable record off the loop and hands
    # the value in; the resolver itself reads nothing.
    info.conversation_root_session_key = manager.conversation_root_for_new_run(
        of_key,
        info.root_session_key,
        f"subagent:{agent_id}",
        durable_root=manager._durable_conversation_root(of_key),
    )
    manager._agents[agent_id] = info
    return info


def _founder_record(root: str | None, *, legacy_parent: str | None = None):  # type: ignore[no-untyped-def]
    """The founder's durable run record, as a continuation reads it once memory is empty.

    ``None`` is no record at all (folder gone). ``""`` is a record written before
    the stamp existed, carrying only ``parent_session`` (*legacy_parent*): a
    nested one (``subagent:``, the default) tells the caller nothing about who
    founded the conversation; a chat parent is the root admission would have
    stamped.
    """
    if root is None:
        state = None
    elif root == "":
        state = {"id": "A", "parent_session": legacy_parent or "subagent:P"}
    else:
        state = {"id": "A", "conversation_root": root}
    return patch("kiro_crew.subagent.read_state", return_value=state)


def _manager(sessions: MagicMock, approval: AsyncMock) -> SubagentManager:
    manager = SubagentManager(
        sessions=sessions,
        ctx_builder=_ctx_builder(),
        on_spawn_approval=approval,
    )
    # The global ``agent.approval_mode`` fallback is a SEPARATE grant that would
    # mask the parent-policy path under test; pin it to interactive.
    manager._global_approval_mode = ""
    return manager


def _gateway(manager: SubagentManager) -> object:
    """A GatewayOrchestrator shell with only what ``_interactive_approval`` reads."""
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gateway = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gateway.sessions = MagicMock()
    gateway.sessions.get_pid = MagicMock(return_value=None)
    gateway.sessions.get_channel = MagicMock(return_value=None)
    gateway.sessions.get_thread = MagicMock(return_value=None)
    gateway.slack = MagicMock()
    gateway.dashboard_state = MagicMock()
    gateway.dashboard_state._yolo = False
    gateway.dashboard_state._slots = {}
    gateway.dashboard_state.request_approval = AsyncMock(return_value=False)
    gateway.dashboard_state.resolve_approval = MagicMock()
    gateway._owner_id = "U000"
    gateway._cfg = MagicMock()
    gateway._cfg.hooks = MagicMock()
    gateway._cfg.hooks.get = MagicMock(return_value=[])
    gateway._cfg.agent.max_subagents = 4
    gateway._approval_mode = None
    gateway.subagent_mgr = manager
    return gateway
