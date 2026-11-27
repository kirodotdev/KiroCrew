"""The session-start notice naming a custom agent's ``file://`` prompt file the session
runs without (``_notice_unread_prompt_file``), in the wording of KAS's own row, on a
backend whose prompt arrives only in the ``[AGENT SYSTEM PROMPT]`` block."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_files import is_kirocrew_default_agent
from kiro_crew.agent_sdk.agent_specs import prompt_only_in_context_block, prompt_unread_notice
from kiro_crew.dashboard.session_notice_marks import _session_notice_is_new

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        DashboardState,
        _ChatSlot,
        append_and_surface,
        logger,
    )
    from kiro_crew.dashboard.chat_turn.deny_chokepoints import _redact_display_text
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn


def _unread_prompt_files(
    turn: "_ChatTurn", slot: "_ChatSlot", client: Any, agent: str
) -> list[Path]:
    """The prompt files the session's ``[AGENT SYSTEM PROMPT]`` read cannot use. Blocking.

    Read with what ``build_message`` hands ``_resolve_agent_prompt`` for this turn:
    its project (else the session's work dir), the owner template of a private
    crewmate's chat, whose prompt arrives through essentials, and the pinned spec.
    The owner comes from the turn's execution record, as ``build_message`` reads it.
    """
    from kiro_crew.context import ContextBuilder
    from kiro_crew.context_assembly.member import resolve_turn_owner
    from kiro_crew.member_essential_context import member_context_identity
    from kiro_crew.skill_scope_pin import pinned_session_skill_spec

    desk = slot.agent if slot.mode == "member" and slot.agent else ""
    execution, _, member, _, _ = resolve_turn_owner(
        turn.execution_context, turn.session_key, desk, None, False
    )
    owner, _ = member_context_identity(member, member_is_id=bool(execution and execution.member_id))
    unread: list[Path] = []
    ContextBuilder._load_agent_prompt(
        agent,
        turn._turn_project or getattr(client, "cwd", "") or None,
        owner_template=agent if owner else "",
        spec_path=pinned_session_skill_spec(turn.session_key),
        unread_out=unread,
    )
    return unread


async def _notice_unread_prompt_file(
    state: "DashboardState", slot: "_ChatSlot", client: Any, turn: "_ChatTurn"
) -> None:
    """One notice row naming the custom agent's ``file://`` prompt file *client*'s new
    session runs without, in KAS's wording (``prompt_unread_notice``).

    Only where the prompt reaches the harness in the block alone
    (``prompt_only_in_context_block``), and not on a resumed session. The block's
    own read logs the WARNING.
    Best-effort: a spec that cannot be read renders no row.
    """
    agent = turn.kiro_agent or slot.agent or ""
    if turn.resumed or not agent or is_kirocrew_default_agent(agent):
        return
    if not prompt_only_in_context_block(client):
        return
    try:
        unread = await asyncio.to_thread(_unread_prompt_files, turn, slot, client, agent)
    except Exception:  # noqa: BLE001 - an informational row must never fail a turn
        logger.debug("agent prompt file for %r could not be read", agent, exc_info=True)
        return
    text = _redact_display_text(prompt_unread_notice(agent, unread))
    if text and _session_notice_is_new(slot, text):
        append_and_surface(state, slot, "notice", text, "msg msg-info")
