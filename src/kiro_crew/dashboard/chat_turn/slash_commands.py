"""The dashboard's local slash commands: /workflow, /agent and /goal, the backend a slash
command would run on, the refusal of a slash command the dashboard does not offer, and the
answer to a slash command no channel can take."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        ACP_BACKEND_KIRO,
        FINISHED_LOOP_REASONS,
        DashboardState,
        KiroCrewConfig,
        _ChatSlot,
        _reset_conversation,
        capabilities_of,
        data_home,
        get_instance,
        normalize_banner,
        parse_workflow_command,
        reason_in,
        sel,
        select_provider_backend,
        switch_announcement,
    )
    from kiro_crew.dashboard.chat_turn.eager_spawn import schedule_eager_spawn
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


async def _handle_workflow_command(
    state: "DashboardState", slot: "_ChatSlot", message: str, session_key: str
) -> None:
    """List saved workflows or run one exactly from ``/workflow``."""
    parsed = parse_workflow_command(message)
    if parsed is None:
        return
    workflow_ref, input_text = parsed
    workflow_service = getattr(state, "workflow_service", None)
    outcome = "ok"
    if workflow_service is None:
        text = "Saved workflows are not available in this runtime."
        outcome = "unavailable"
    elif not workflow_ref:
        definitions = await asyncio.to_thread(workflow_service.list_definitions)
        if not definitions:
            text = "No saved workflows yet. Create one under Customize > Workflows."
            outcome = "empty"
        else:
            lines = ["**Saved workflows** — run one with `/workflow <name> [input]`\n"]
            for definition in definitions:
                description = definition.get("description") or ""
                suffix = f" — {description}" if description else ""
                lines.append(
                    f"- `/workflow {definition.get('slug')}` "
                    f"(revision {definition.get('revision')}){suffix}"
                )
            text = "\n".join(lines)
    else:
        started = await workflow_service.start_definition(
            workflow_ref,
            input_text=input_text,
            author=session_key,
            session_key=session_key,
        )
        if "run_id" not in started:
            text = str(started.get("error") or "Could not start the saved workflow.")
            outcome = "error"
        else:
            text = (
                f"Started `/workflow {started.get('slug') or workflow_ref}` as "
                f"`{started.get('run_id')}` from revision {started.get('revision')}. "
                "Its result will appear here when it finishes."
            )
    _append_command_reply(slot, text)
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name="/workflow",
        tool_kind="slash_command",
        outcome=outcome,
        metadata={"slot": slot.key, "workflow": workflow_ref},
    )
    state.push_slots_update()


async def _handle_agent_command(
    state: "DashboardState", slot: "_ChatSlot", agent: str, session_key: str
) -> None:
    """Handle ``/agent <name>`` through the same transaction as the agent picker.

    Runs inside the slot's own turn, before it acquires a session, so the
    switch is told it is that turn (``own_turn``) rather than refusing on the
    slot's own ``running`` flag. ``template`` because kiro-cli's ``/agent``
    names agent specs, and a stated kind refuses a name nothing answers rather
    than committing it with the default agent answering the next turn. The
    caller is never treated as the owner: the text can come from any allowed
    channel user.
    """
    # Call-time import: chat_handlers imports this module.
    from kiro_crew.dashboard.chat_handlers import (
        SlotAgentSwitchCaller,
        agent_switch_outcome,
        switch_slot_agent,
    )

    resp = await switch_slot_agent(
        state, slot, slot.key, agent, "template", SlotAgentSwitchCaller.for_turn(slot)
    )
    ok, body = agent_switch_outcome(resp)
    if ok:
        text = switch_announcement(str(body.get("agent") or agent), str(body.get("warning") or ""))
    else:
        reason = str(body.get("error") or "the switch was refused")
        text = f"⚠️ Could not switch to agent `{agent}`: {reason}"
    _append_command_reply(slot, text)
    sel().log_tool_invocation(
        session_key=session_key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name="/agent",
        tool_kind="slash_command",
        outcome="ok" if ok else "error",
        metadata={"slot": slot.key, "agent": agent, "status": resp.status},
    )
    if ok:
        state.broadcast_ws("slot_agent_switch", {"slot": slot.key, "agent": str(slot.agent)})
    state.push_slots_update()


async def _handle_goal_command(state: "DashboardState", slot: "_ChatSlot", message: str) -> None:
    """Handle the ``/goal`` slash command (v0 self-verdict loop).

    Extracted from ``_run_chat`` so it is unit-testable in isolation. Pure glue
    over the async ``AutoNudgeService`` (``add`` / ``get_by_slot`` / ``remove``);
    no autonudge-internals change. Subcommands: ``status`` (default/empty),
    ``clear``, else arm with an optional ``--max N`` budget (default 50, clamped
    1..50).
    """
    _goal_svc = get_instance()
    _parts = message.split(None, 1)
    _rest = _parts[1].strip() if len(_parts) > 1 else ""
    if _goal_svc is None:
        body = (
            "🎯 Goal loops are unavailable (AutoNudge is disabled). "
            "Set `KIROCREW_AUTONUDGE=1` and restart the gateway."
        )
    elif _rest in ("", "status"):
        _loop = _goal_svc.get_by_slot(slot.key)
        if _loop is not None and getattr(_loop, "active", True):
            _cap = _loop.max_cycles or "∞"
            body = f"🎯 Active goal (budget {_cap} turns). " "Use `/goal clear` to stop it."
        elif _loop is not None:
            # The slot holds a record the service KEPT after it stopped -- a goal
            # its stop file finished, a paused one, a bound reached -- which is
            # not an active goal and must not read as one. The reason is the
            # service's own code; ``/goal clear`` removes the record either way.
            _why = getattr(_loop, "stopped_reason", "") or "stopped"
            _ended = "finished" if reason_in(_why, FINISHED_LOOP_REASONS) else "stopped"
            body = (
                f"🎯 Goal {_ended} ({_why}); it is not running. "
                "Use `/goal clear` to clear it, or `/goal <objective>` to set a new one."
            )
        else:
            body = (
                "No active goal. Set one with `/goal <objective>` "
                "(optionally `/goal --max N <objective>`)."
            )
    elif _rest == "clear":
        _loop = _goal_svc.get_by_slot(slot.key)
        if _loop is not None:
            await _goal_svc.remove(_loop.id, stop_reason="goal_cleared")
            body = "🎯 Goal cleared."
        else:
            body = "No active goal to clear."
    else:
        _max_cycles = 50
        _objective = _rest
        _m = re.match(r"--max\s+(\d+)\s+(.*)", _rest, re.DOTALL)
        if _m:
            _max_cycles = max(1, min(50, int(_m.group(1))))
            _objective = _m.group(2).strip()
        elif _rest.startswith("--max"):
            _objective = ""
        if not _objective:
            body = "Usage: `/goal <objective>` or `/goal --max N <objective>`."
        else:
            _slug = re.sub(r"[^A-Za-z0-9._-]", "_", slot.key)
            _sentinel = str(data_home() / "goal-stop" / f"{_slug}.stop")
            Path(_sentinel).unlink(missing_ok=True)
            _nudge = (
                f"Goal: {_objective}\n"
                "Each idle cycle, in order: "
                f'(1) if the file {_sentinel} exists -> autonudge_stop(reason="sentinel") and stop; '
                "(2) if the goal is fully met by concrete evidence (a passing test, a built file, "
                'command output — not a guess) -> autonudge_stop(reason="goal met"), post a one-line '
                "summary citing the evidence, and stop; "
                "(3) else do ONE atomic step (<=5 tool calls) and make the deliverable durable "
                "(write the file / run the check) before claiming progress.\n"
                "Guardrails: never git push; never read credential files. Hard blocker -> state it once and "
                f'autonudge_stop(reason="blocked"). Budget {_max_cycles} cycles (service stops at '
                "the cap). One short progress line per cycle."
            )
            await _goal_svc.add(
                slot.key,
                message=_nudge,
                idle_secs=15,
                max_cycles=_max_cycles,
                stop_sentinel_path=_sentinel,
                # The objective is the reader-facing row; the full instruction
                # above is served by GET /api/autonudge. Routed through the
                # authorizer's ``normalize_banner`` so this producer gets the
                # SAME redaction + cap policy as the REST/MCP paths. ``truncate``
                # (not a pre-slice) because the objective is arbitrarily long:
                # the FULL text is redacted first and only then cut to the cap,
                # so a credential straddling the cap boundary is masked whole
                # rather than sliced into a raw prefix.
                banner=normalize_banner(_objective, absent_ok=True, truncate=True)[0],
                admission_check=lambda: state.get_slot(slot.key) is slot,
            )
            body = (
                f"⊙ Goal set ({_max_cycles}-turn budget): {_objective}\n\n"
                "I'll work toward it across turns and stop when it's met "
                "(verified by evidence) — or run `/goal clear` to stop."
            )
    sel().log_tool_invocation(
        session_key=slot.key,
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name="/goal",
        tool_kind="slash_command",
        outcome="ok",
        metadata={"slot": slot.key},
    )
    _append_command_reply(slot, body)
    state.push_slots_update()


def _refuse_blocked_slash(turn) -> None:
    first_word = turn.first_word
    slot = turn.slot
    state = turn.state
    sel().log_tool_invocation(
        session_key="",
        agent=slot.agent or "kirocrew",
        source="dashboard",
        tool_name=first_word,
        tool_kind="slash_command",
        outcome="blocked",
        metadata={"slot": slot.key},
    )
    slot.append(
        "assistant",
        f"⚠️ `{first_word}` is not available in the dashboard.",
        "msg msg-a",
    )
    state.push_slots_update()
