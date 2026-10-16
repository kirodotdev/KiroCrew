"""The dashboard's local slash commands: the backend a slash command would run on and the answer
to one no channel can take."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        ACP_BACKEND_KIRO,
        DashboardState,
        KiroCrewConfig,
        _ChatSlot,
        _reset_conversation,
        capabilities_of,
        schedule_eager_spawn,
        sel,
        select_provider_backend,
    )
    from kiro_crew.dashboard.chat_turn.segment_flush import _append_command_reply
    from kiro_crew.dashboard.chat_turn.session_reset import _consume_pending_reset


async def _slash_command_backend(state: DashboardState, session_key: str) -> str:
    """The backend a slash command on *session_key* would reach.

    A positively live provider answers for itself (peeked, never created);
    otherwise the factory's member-aware selection does, so a dead row cannot
    answer for its successor.
    """
    provider = state.sessions.get_provider(session_key)
    if provider is not None and await state.sessions.is_provider_alive(session_key) is True:
        return capabilities_of(provider).backend
    agent_cfg = KiroCrewConfig.load().agent
    backend = select_provider_backend(
        session_key,
        getattr(agent_cfg, "member_acp_backend", ""),
        getattr(agent_cfg, "acp_backend", ""),
    )
    return backend if isinstance(backend, str) else ACP_BACKEND_KIRO


async def _answer_slash_without_channel(
    state: DashboardState, slot: _ChatSlot, session_key: str, command: str
) -> str:
    """Answer a harness command the backend cannot run, as a local command.

    ``/clear`` queues the discard ``reset_conversation`` queues and applies it
    through the same consumer, so the next turn starts with no memory of this
    conversation. The transcript stays, as it does for that tool. A session that
    refuses the discard (busy, sub-agents attached) keeps it queued for a later
    turn boundary.

    Returns the outcome it records: ``discarded``, ``queued`` or
    ``unsupported_backend``.
    """
    if command.lower() == "/clear":
        await _reset_conversation(slot, session_key, {})
        torn_down = await _consume_pending_reset(state, slot, allow_discard=True)
        # The consumer's True also covers a queued project reset; only a spent
        # discard flag means THIS conversation was dropped.
        cleared = torn_down and not slot._pending_discard_conversation_key
        outcome = "discarded" if cleared else "queued"
        if torn_down:
            schedule_eager_spawn(state, slot)
        if cleared:
            # The plan goes with the conversation, as on kiro's native /clear:
            # a kept pill would be rebuilt into the fresh one at its cold start.
            if slot.set_todo(None):
                state.broadcast_ws("todo_update", {"slot": slot.key, "todo": None})
            text = (
                "🗑 Conversation cleared. Earlier messages stay visible, but the agent "
                "no longer remembers them."
            )
        else:
            text = (
                "🗑 Clear queued. The agent is busy right now, so your next reply "
                "may still remember this conversation."
            )
    else:
        outcome = "unsupported_backend"
        text = f"⚠ `{command}` doesn't work with KAS. It was not sent to the agent."
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name=command,
        tool_kind="slash_command",
        outcome=outcome,
        metadata={"slot": slot.key},
    )
    _append_command_reply(slot, text)
    state.push_slots_update()
    return outcome
