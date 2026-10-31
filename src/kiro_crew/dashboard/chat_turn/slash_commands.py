"""The dashboard's local slash commands and the turn's slash gate (``_turn_slash_gate``, which
answers a local slash command before a session is claimed): /workflow, /agent and /goal, the
backend a slash command would run on, the refusal of a slash command the dashboard does not
offer, the answer to a slash command no channel can take, and the answer to a /compact the
session did not compact for (``_turn_compact_command``)."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kiro_crew.agent_sdk.backends import ACP_BACKEND_KAS
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.agent_sdk.provider_identity import is_claude_code
from kiro_crew.agent_sdk.tool_gate import label_for
from kiro_crew.agent_switch_command import agent_switch_target, switch_announcement
from kiro_crew.autonudge import FINISHED_LOOP_REASONS, reason_in
from kiro_crew.autonudge_authz import normalize_banner
from kiro_crew.dashboard.chat_utils import (
    _KIRO_ONLY_BLOCKED_SLASH_COMMANDS,
    _append_compaction_notice,
    is_harness_slash_command,
    parse_workflow_command,
)
from kiro_crew.dashboard.kas_local_commands import (
    KAS_LOCAL_INSPECT_COMMANDS,
    answer_kas_inspect_command,
)
from kiro_crew.dashboard.session_directive_apply import _reset_conversation
from kiro_crew.dashboard.state import (
    HOOK_CONTINUATION_RECOVERY_PREFIX,
    MONITOR_WAKE_PREFIX,
    SUBAGENT_SYNTHESIS_PREFIX,
)
from kiro_crew.messaging.commands import compact_unsupported_backend, compact_unsupported_reply
from kiro_crew.security import redact

if TYPE_CHECKING:
    from kiro_crew.dashboard.chat_runner import (
        _BLOCKED_SLASH_COMMANDS,
        ACP_BACKEND_KIRO,
        ACP_BACKENDS_COMPACT,
        ACP_BACKENDS_LOCAL_CLEAR,
        ACP_BACKENDS_LOCAL_HARNESS_COMMANDS,
        MAX_PROMPT_BYTES,
        DashboardState,
        KiroCrewConfig,
        RepeatLoopTracker,
        _ChatSlot,
        _list_aim_prompts,
        _run_chat,
        capabilities_of,
        data_home,
        get_instance,
        logger,
        sel,
        select_provider_backend,
    )
    from kiro_crew.dashboard.chat_turn.eager_spawn import schedule_eager_spawn
    from kiro_crew.dashboard.chat_turn.prompt_assembly import _expand_prompt_mention_off_loop
    from kiro_crew.dashboard.chat_turn.queue_cycle import _send_chat_done
    from kiro_crew.dashboard.chat_turn.recovery import (
        _COMPACT_FAIL_REASON_MAX_CHARS,
        COMPACT_FRESH_SESSION_NOTICE,
        COMPACT_REPLAY_PENDING_NOTICE,
    )
    from kiro_crew.dashboard.chat_turn.regenerate_swap import note_regenerate_reply
    from kiro_crew.dashboard.chat_turn.segment_flush import (
        _append_command_reply,
        _cleared_segment_text,
    )
    from kiro_crew.dashboard.chat_turn.session_reset import _consume_pending_reset
    from kiro_crew.dashboard.chat_turn.turn_context import _restore_skills_context_after_compaction
    from kiro_crew.dashboard.chat_turn.turn_locals import _ChatTurn, _Flow
    from kiro_crew.dashboard.chat_turn.turn_reply_seams import _drop_wire_tail
    from kiro_crew.dashboard.chat_turn.turn_stats import _context_usage_payload


async def _slash_command_backend(state: DashboardState, session_key: str) -> str:
    """The backend a slash command on *session_key* would reach.

    A positively live provider answers for itself (peeked, never created);
    otherwise the factory's member-aware selection does, so a dead row cannot
    answer for its successor.
    """
    provider = state.sessions.get_provider(session_key)
    if provider is not None and await state.sessions.is_provider_alive(session_key) is True:
        return capabilities_of(provider).backend
    from kiro_crew.crewmate_harness import CrewmateHarnessUnavailable, crewmate_backend_pin

    cfg = KiroCrewConfig.load()
    agent_cfg = cfg.agent
    try:
        backend = select_provider_backend(
            session_key,
            getattr(agent_cfg, "member_acp_backend", ""),
            getattr(agent_cfg, "acp_backend", ""),
            crew_backend=crewmate_backend_pin(cfg, session_key),
        )
    except CrewmateHarnessUnavailable:
        # Only a pre-check: the turn goes on to the session start, which refuses
        # the unstartable pin inside the turn's error handling, with its reason.
        return ACP_BACKEND_KIRO
    return backend if isinstance(backend, str) else ACP_BACKEND_KIRO


async def _answer_slash_without_channel(
    state: DashboardState, slot: _ChatSlot, session_key: str, command: str, message: str = ""
) -> str:
    """Answer a harness command the backend cannot run, on a KAS session.

    The KAS spelling of :func:`_answer_slash_for_harness`, kept for the callers that
    answer only ``/clear`` or are KAS by construction.
    """
    return await _answer_slash_for_harness(
        state, slot, session_key, command, ACP_BACKEND_KAS, message
    )


async def _answer_slash_for_harness(
    state: DashboardState,
    slot: _ChatSlot,
    session_key: str,
    command: str,
    backend: str,
    message: str = "",
) -> str:
    """Answer a harness command the backend cannot run, as a local command.

    ``/clear`` queues the discard ``reset_conversation`` queues and applies it
    through the same consumer, so the next turn starts with no memory of this
    conversation. The transcript stays, as it does for that tool. A session that
    refuses the discard (busy, sub-agents attached) keeps it queued for a later
    turn boundary. Any other command gets a notice naming *backend*'s harness as a
    person reads it.

    On KAS, ``/model``, ``/tools`` and ``/mcp`` are answered from the dashboard's own
    state (``kas_local_commands``, which reads KAS's own registration); *message* is
    the whole command line, for ``/model <id>``. Another harness gets the notice for
    them.

    Returns the outcome it records: ``discarded``, ``queued``, ``shown``,
    ``switched``, ``error``, ``no_session`` or ``unsupported_backend``.
    """
    if backend == ACP_BACKEND_KAS and command.lower() in KAS_LOCAL_INSPECT_COMMANDS:
        text, outcome = await answer_kas_inspect_command(
            state, slot, session_key, message or command
        )
    elif command.lower() == "/clear":
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
        harness = "KAS" if backend == ACP_BACKEND_KAS else label_for(backend)
        text = f"⚠ `{command}` doesn't work with {harness}. It was not sent to the agent."
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
    # Recorded like every command answer: a Regenerate of this command
    # replaces its previous reply with this row (``regenerate_swap``).
    note_regenerate_reply(
        slot,
        slot.append(
            "assistant",
            f"⚠️ `{first_word}` is not available in the dashboard.",
            "msg msg-a",
        ),
    )
    state.push_slots_update()


async def _turn_slash_gate(turn: _ChatTurn) -> _Flow | None:
    """Answer the local slash commands before a session is claimed."""
    _commands_off = turn._commands_off
    _directive_channel_origin = turn._directive_channel_origin
    _directive_self_wake = turn._directive_self_wake
    _directive_user_origin = turn._directive_user_origin
    _prompt_depth = turn._prompt_depth
    _responsive_human = turn._responsive_human
    _responsive_protected = turn._responsive_protected
    _synthetic_payload = turn._synthetic_payload
    _turn_actor = turn._turn_actor
    message = turn.message
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    turn_exit = turn.turn_exit
    # Same call, same result, again and again: told once per call (repeat_loop).
    _repeat_loop = RepeatLoopTracker()
    turn._repeat_loop = _repeat_loop
    # Whether this turn already said "will compact after this turn" (once per turn).
    _compact_after_turn_noticed = False
    turn._compact_after_turn_noticed = _compact_after_turn_noticed
    # Whether the current denied batch's cascade already steered its one
    # cause-specific notice. One notice covers the whole cascaded remainder, so
    # this stops the second and later members from repeating it; re-armed each
    # time ``slot._batch_rejected`` is set, so a second batch denied later in
    # the same turn gets its own notice.
    _batch_cascade_steered = False
    turn._batch_cascade_steered = _batch_cascade_steered
    # Track how deep an unbroken hook-continuation run is, so the Stop hook can
    # see it: each consecutive hook continuation is one deeper; any other turn
    # (a real user message, a refusal recovery) breaks the run and resets it.
    # Gate on synthetic provenance, not the marker text alone: a real dequeued
    # continuation carries _synthetic_payload, but a user who types the marker
    # verbatim is ordinary speech and must not inflate the depth a gate hook
    # sees (would let a spoofed message drive the hook's self-limit).
    if _synthetic_payload and message.startswith(HOOK_CONTINUATION_RECOVERY_PREFIX):
        slot._hook_continuation_depth += 1
    else:
        slot._hook_continuation_depth = 0
    # Runner-authored continuations are orchestration, not user input, and the
    # post-fan-out synthesis prompt is one too: never mirror either to linked
    # surfaces (Slack/Telegram) as if the user typed it — only the assistant reply
    # is delivered. A recovery that replays the user's own message is NOT covered,
    # because that text is the user's; the queue entry distinguishes the two so a
    # user who types a marker verbatim still counts as ordinary user speech.
    _is_synthetic = _synthetic_payload or message.startswith(SUBAGENT_SYNTHESIS_PREFIX)
    turn._is_synthetic = _is_synthetic

    # ── Slash commands: detect early, before session acquisition ──
    # Every command branch keys on ``first_word``: _run_chat's harness forward
    # (``is_slash``), the blocked set and the local commands below (/goal, /workflow,
    # /prompts, /compact). With commands off the word is not read at all, so a
    # hand-off entry reaches the model as the text it is, whatever it opens with.
    first_word = message.split()[0] if message.strip() and not _commands_off else ""
    turn.first_word = first_word
    _cfg_agent = KiroCrewConfig.load().agent
    _is_cc_provider = is_claude_code(_cfg_agent.provider)
    # Named rather than inlined so the quick-prompt exception is one testable rule
    # instead of a condition only reachable by driving the whole of _run_chat: a macro
    # must NOT be forwarded to the harness as a command.
    is_slash = is_harness_slash_command(first_word, cc_provider=_is_cc_provider)
    turn.is_slash = is_slash
    _this_turn_is_clear = is_slash and first_word.lower() == "/clear"
    turn._this_turn_is_clear = _this_turn_is_clear

    # Commands that are impossible on every harness stay a true pre-dispatch
    # gate: refusing them creates and consumes no session. The harness-specific
    # subtraction is decided only after get_or_create in _turn_admit returns the exact
    # provider that would execute it. A registry peek is not authoritative:
    # get_or_create probes liveness and can evict a dead row, then build the
    # opposite harness from the current factory defaults.
    if first_word in (_BLOCKED_SLASH_COMMANDS - _KIRO_ONLY_BLOCKED_SLASH_COMMANDS):
        _refuse_blocked_slash(turn)
        return _Flow.END

    # ── /goal: arm / clear a goal-driven self-verdict loop (v0) ──
    # v0 rides AutoNudgeService unchanged: the nudge instructs the agent to
    # self-check its Definition of Done each cycle and call autonudge_stop when
    # met.
    if first_word == "/goal":
        turn_exit.local_command = first_word
        await _handle_goal_command(state, slot, message)
        return _Flow.END

    if first_word == "/workflow":
        turn_exit.local_command = first_word
        await _handle_workflow_command(state, slot, message, session_key)
        return _Flow.END

    # ── /agent <name>: Crew's own agent switch, never kiro-cli's ──
    # Every surface that reaches a slot through this runner (the split pane,
    # a Slack thread linked to a dashboard chat) gets the switch the agent
    # picker makes. The main composer already sends it there before the
    # message is ever posted.
    _agent_target = agent_switch_target(message) if first_word == "/agent" else None
    if _agent_target is not None:
        turn_exit.local_command = first_word
        await _handle_agent_command(state, slot, _agent_target, session_key)
        return _Flow.END

    # ── /prompts: handle locally instead of forwarding to kiro-cli ──
    if first_word == "/prompts":
        turn_exit.local_command = first_word

        args = message.split(None, 2)  # /prompts [get] [name]
        sub = args[1] if len(args) > 1 else ""

        if sub == "get" and len(args) > 2:
            # /prompts get <name> — invoke the prompt in this chat
            name = args[2]
            # Off the loop for the same reason as the @mention path in _turn_admit.
            expanded, status = await _expand_prompt_mention_off_loop(f"@{name}", state, slot)
            if status == "ok":
                sel().log_tool_invocation(
                    session_key="",
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="ok",
                    metadata={"mention": f"@{name}", "slot": slot.key, "via": "/prompts get"},
                )
                # Re-enter _run_chat with the expanded message (depth=1, no further
                # expansion). The actor travels with it: the nested turn has the same
                # author as the one that carried the mention, and the resolver's
                # fallback is ``user``, so dropping it here would record a person
                # who never typed anything.
                # The nested call is a whole turn with its own exit guard, so the
                # tail is its to run, however it ends.
                turn_exit.handed_off = True
                await _run_chat(
                    state,
                    slot,
                    expanded,
                    _prompt_depth=1,
                    _responsive_human=_responsive_human,
                    _responsive_protected=_responsive_protected,
                    _directive_user_origin=_directive_user_origin,
                    _directive_self_wake=_directive_self_wake,
                    _directive_channel_origin=_directive_channel_origin,
                    _turn_actor=_turn_actor,
                )
            elif status == "blocked":
                sel().log_tool_invocation(
                    session_key="",
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="blocked",
                    metadata={"mention": f"@{name}", "slot": slot.key, "via": "/prompts get"},
                )
                _append_command_reply(slot, f"🔒 Prompt `{name}` blocked — sensitive path.")
                state.push_slots_update()
            elif status == "too_large":
                sel().log_tool_invocation(
                    session_key="",
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="too_large",
                    metadata={"mention": f"@{name}", "slot": slot.key, "via": "/prompts get"},
                )
                _append_command_reply(
                    slot, f"⚠️ Prompt `{name}` exceeds size limit ({MAX_PROMPT_BYTES // 1000}KB)."
                )
                state.push_slots_update()
            else:
                sel().log_tool_invocation(
                    session_key="",
                    agent=slot.agent or "kirocrew",
                    source="dashboard",
                    tool_name="prompt_expansion",
                    tool_kind="prompt",
                    outcome="not_found",
                    metadata={"mention": f"@{name}", "slot": slot.key, "via": "/prompts get"},
                )
                _append_command_reply(slot, f"❌ Prompt `{name}` not found.")
                state.push_slots_update()
            return _Flow.END

        # /prompts or /prompts list — show available prompts
        try:
            # _list_aim_prompts walks the (possibly large or edition-supplied)
            # prompt_source_roots() with rglob + file reads; keep it off the
            # event loop so a slow/network-backed root can't stall the gateway.
            # Pass THIS slot's project so its local prompts are listed per-slot
            # (fail-closed to global-only when the slot has no project).
            project_dir = Path(slot.project) if slot.project else None
            prompts = await asyncio.to_thread(_list_aim_prompts, project_dir)
        except Exception:
            prompts = []
        if not prompts:
            note_regenerate_reply(
                slot,
                slot.append(
                    "assistant",
                    "No prompts found. Create prompts in `~/.kiro/prompts/` "
                    "(or `~/.kiro/crew/prompts/`).",
                    "msg msg-a",
                ),
            )
            sel().log_tool_invocation(
                session_key="",
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name="prompt_list",
                tool_kind="prompt",
                outcome="empty",
                metadata={"count": 0, "slot": slot.key, "via": "/prompts"},
            )
            state.push_slots_update()
            return _Flow.END
        lines = ["**Available Prompts** — type `@name` to invoke\n"]
        by_source: dict[str, list] = {}
        for p in prompts:
            (by_source.setdefault(p["source"], [])).append(p)
        for src, items in sorted(by_source.items()):
            label = "User Prompts" if src in ("aim", "package") else f"User Prompts ({src})"
            turn.label = label
            lines.append(f"\n**{label}:**")
            for p in items:
                desc = f" — {p['description']}" if p["description"] else ""
                lines.append(f"- `@{p['fullName']}`{desc}")
        text = "\n".join(lines)
        _append_command_reply(slot, text)
        sel().log_tool_invocation(
            session_key="",
            agent=slot.agent or "kirocrew",
            source="dashboard",
            tool_name="prompt_list",
            tool_kind="prompt",
            outcome="ok",
            metadata={"count": len(prompts), "slot": slot.key, "via": "/prompts"},
        )
        state.push_slots_update()
        return _Flow.END

    # Refuse local-only commands before a claim can spawn or wait on a provider.
    # A positively live row answers for itself; otherwise use allocation's
    # member-aware successor selection. Acquisition still confirms support,
    # because the provider can be replaced between this probe and the claim.
    if first_word in _KIRO_ONLY_BLOCKED_SLASH_COMMANDS:
        _todos_backend = await _slash_command_backend(state, session_key)
        if not capabilities_for(_todos_backend).supports_native_todos:
            _refuse_blocked_slash(turn)
            return _Flow.END

    # ── Manual /compact capability gate ──
    # KAS never answers the /compact prompt with a compaction status (its
    # summarization_* frames fire only for KAS-initiated auto-summarization),
    # so dispatching the command would strand the deferred
    # wait_for_compaction() for the full COMPACT_WAIT_TIMEOUT_SECS. KAS manages
    # compaction itself — the same relationship the cc_managed decline encodes
    # for Claude-Code sessions — so answer informationally, as a LOCAL command.
    #
    # Placed HERE, ahead of _turn_admit's OPTIONS-expiry boundary and before any
    # session is acquired, so a refused /compact behaves as if the turn never
    # started: no pending Slack OPTIONS control is struck through, no session is
    # created, and no one-shot first-turn state (history replay, resume sid,
    # session-map binding, compaction override) can be consumed or destroyed.
    # The live session's provider is authoritative when one exists (peeked,
    # never created); otherwise use the factory's member-aware backend
    # selection, so a dead row cannot answer for its successor.
    if first_word == "/compact":
        _live_provider = state.sessions.get_provider(session_key)
        _live_provider_alive = (
            await state.sessions.is_provider_alive(session_key)
            if _live_provider is not None
            else None
        )
        if _live_provider is not None and _live_provider_alive is True:
            # The same predicate every channel's /compact gate asks.
            _compact_unsupported = compact_unsupported_backend(_live_provider)
        elif _is_cc_provider:
            # Claude Code compacts natively in-prompt (cc_managed).
            _compact_unsupported = None
        else:
            # A dead registry row is not the harness dispatch would reach:
            # get_or_create evicts it and uses this same member-aware selector.
            from kiro_crew.crewmate_harness import (
                CrewmateHarnessUnavailable,
                crewmate_backend_pin,
            )

            _cfg = KiroCrewConfig.load()
            _agent_cfg = _cfg.agent
            try:
                _cfg_backend = select_provider_backend(
                    session_key,
                    getattr(_agent_cfg, "member_acp_backend", ""),
                    getattr(_agent_cfg, "acp_backend", ""),
                    crew_backend=crewmate_backend_pin(_cfg, session_key),
                )
            except CrewmateHarnessUnavailable:
                # As in _slash_command_backend: the session start reports it.
                _cfg_backend = None
            _compact_unsupported = (
                _cfg_backend
                if isinstance(_cfg_backend, str) and _cfg_backend not in ACP_BACKENDS_COMPACT
                else None
            )
        # The isinstance guard means only a positively named unsupported
        # backend id is refused — a mocked provider's truthy attribute never
        # reads as one.
        if isinstance(_compact_unsupported, str) and _compact_unsupported:
            sel().log_tool_invocation(
                session_key=session_key,
                agent=slot.agent or "kirocrew",
                source="dashboard",
                tool_name=first_word,
                tool_kind="slash_command",
                outcome="auto_managed_backend",
                metadata={"backend": _compact_unsupported, "slot": slot.key},
            )
            # The shared three-arm reply, not a sentence of this surface's own.
            # An inline copy here said the harness self-manages for EVERY backend
            # outside ``ACP_BACKENDS_COMPACT``, which is true of one of them --
            # and this is the dashboard, where the claim is most visible. The
            # markdown register matches what that helper already emits.
            note_regenerate_reply(
                slot,
                slot.append(
                    "assistant",
                    compact_unsupported_reply(_compact_unsupported),
                    "msg msg-a",
                ),
            )
            state.push_slots_update()
            return _Flow.END

    # ── Harness commands on KAS, which has no command channel ──
    # (``ACP_BACKENDS_LOCAL_HARNESS_COMMANDS``; GET /api/slash-commands reads the
    # same set to leave these commands out of the / menu.)
    # The session handle runs a command natively only on kiro-cli; KAS gets it
    # as session/prompt text, and KAS has no built-in /clear there, so the
    # model reads "/clear" as a request and the conversation stays. KAS also
    # refuses _kiro.dev/commands/execute itself, so that verb is no way out.
    # Answer here instead, before any session work: /clear takes the same discard
    # reset_conversation queues, /model, /tools and /mcp are read from the
    # dashboard's own state, and every other command gets a notice rather than
    # reaching the model. /compact passes through: on KAS the session handle
    # sends it as the engine's own compaction verb, not as prompt text. A /todos
    # that got past its own gate is on a harness that runs it natively.
    # The other harnesses in ACP_BACKENDS_LOCAL_CLEAR have no /clear of their own
    # either, so /clear alone is answered here for them too; their other commands
    # keep the prompt path, where those harnesses serve the ones they know.
    if (
        is_slash
        and not _is_cc_provider
        and first_word != "/compact"
        and first_word not in _KIRO_ONLY_BLOCKED_SLASH_COMMANDS
    ):
        _command_backend = await _slash_command_backend(state, session_key)
        if _command_backend in ACP_BACKENDS_LOCAL_HARNESS_COMMANDS or (
            _this_turn_is_clear and _command_backend in ACP_BACKENDS_LOCAL_CLEAR
        ):
            turn_exit.local_command = first_word
            await _answer_slash_for_harness(
                state, slot, session_key, first_word, _command_backend, message
            )
            return _Flow.END

    _is_monitor_wake = message.startswith(MONITOR_WAKE_PREFIX)
    turn._is_monitor_wake = _is_monitor_wake
    _acquired = False
    turn._acquired = _acquired
    # Whether this turn holds the session-switch lock (see the acquire in _turn_admit);
    # read by _turn_record_outcome (chat_turn/queue_cycle.py), from _run_chat's finally, so
    # an exit between acquire and release never leaks it.
    turn._dispatch_lock_held = False
    _mirror_stream_ts: str = ""
    turn._mirror_stream_ts = _mirror_stream_ts
    # The task stream opens lazily on the first mirrored tool call; one attempt
    # per turn, so a stream Slack refuses is not re-requested on every tool.
    turn._mirror_stream_tried = False
    # ts of the mirror's "Thinking…" placeholder; "" once it has been deleted.
    turn._mirror_thinking_ts = ""
    # Whether this turn posted its prompt echo; a recovery requeue reads it.
    turn._mirror_echo_posted = False
    _mirror_chan: str | None = ""
    turn._mirror_chan = _mirror_chan
    _mirror_active_task = ""
    turn._mirror_active_task = _mirror_active_task
    _mirror_active_task_title = ""
    turn._mirror_active_task_title = _mirror_active_task_title
    _mirror_thread: str | None = ""
    turn._mirror_thread = _mirror_thread
    _mirror_task_counter = 0
    turn._mirror_task_counter = _mirror_task_counter
    _memory_preparation_admitted = False
    turn._memory_preparation_admitted = _memory_preparation_admitted
    # Zero until _turn_admit writes the marker, so a cancellation that lands during
    # startup admission has nothing to clear.
    _local_turn_marker_generation = 0
    turn._local_turn_marker_generation = _local_turn_marker_generation
    # Bound before _run_chat's main try because cancellation may land while this turn
    # waits for shared memory preparation, before any provider is allocated.
    client: Any = None
    turn.client = client
    # The pre-acquisition gate accepted this command. Keep its deferred OPTIONS
    # drain owned by _turn_release_session (chat_turn/session_reset.py), from _run_chat's
    # finally, until acquisition either refuses or drains it.
    _deferred_options_expiry = (
        _prompt_depth == 0 and first_word in _KIRO_ONLY_BLOCKED_SLASH_COMMANDS
    )
    turn._deferred_options_expiry = _deferred_options_expiry
    return None


async def _turn_compact_command(turn: _ChatTurn) -> None:
    """Answer a /compact the session did not compact for."""
    _replay_pending = turn._replay_pending
    client = turn.client
    first_word = turn.first_word
    saw_compaction = turn.saw_compaction
    session_key = turn.session_key
    slot = turn.slot
    state = turn.state
    # /compact acknowledged but compaction deferred — send a lightweight
    # follow-up that triggers the actual compaction so the user doesn't have to.
    logger.debug("Compaction check: first_word=%r saw_compaction=%s", first_word, saw_compaction)
    if first_word == "/compact" and not saw_compaction:
        # Clear streamed "Compacting conversation..." text from kiro-cli.
        # claude-agent-acp streams its own "Compacting..." notice, which the
        # ACP layer recognises and reports as a status event WITHOUT deleting
        # the chunk (`parse_claude_compaction_notice`) — so on that backend
        # the notice is cleared by this same purge rather than by suppression
        # upstream.
        slot.purge_chunks()
        _drop_wire_tail(turn)
        turn.assistant_text = _cleared_segment_text(slot)
        turn._produced_visible_output = True
        await _send_chat_done(state, slot, continuing=True, ends_turn=False)

        # claude-agent-acp performs /compact synchronously inside session/prompt;
        # there is no out-of-band _kiro.dev/compaction/status notification, so
        # EVENT_COMPLETE is the done signal. Skip the kiro-only async wait.
        #
        # This arm is now a FALLBACK, not the normal path: the ACP layer
        # translates the adapter's own "Compacting..." / "Compacting
        # completed." notices into EVENT_COMPACTION_STATUS, so a manual
        # /compact on this backend normally leaves `saw_compaction` True and
        # never reaches here. It still earns its place — if the adapter
        # reworks those literals the translation stops matching, and this
        # keeps `/compact` acknowledged instead of silent.
        #
        # Note: the success message is hardcoded so no redaction pass is
        # needed today. If claude-agent-acp ever returns a compaction
        # summary (e.g. via EVENT_COMPLETE payload growing a `summary`
        # field), pipe it through redact_credentials + redact_exfiltration_urls
        # before interpolation — matching the kiro-cli path below.
        #
        # Gated on `SessionCapabilities.compacts_inline` -- does this backend
        # finish the compaction inside the prompt turn? -- rather than on which
        # harness it is. The two arms are not interchangeable: acknowledging a
        # backend that reports asynchronously loses the notice, and awaiting one
        # that already finished strands the waiter for its whole timeout.
        if _replay_pending:
            # A slash command never delivers the replay, so the lease is
            # still armed and the provider session is empty: kiro-cli ends
            # the turn with "Conversation too short to compact." and sends
            # no compaction status. Awaiting one would hold the chat for the
            # whole compaction timeout. Nothing was compacted, so skills
            # context is not re-armed and the lease stays for the next prompt.
            _append_compaction_notice(state, slot, COMPACT_REPLAY_PENDING_NOTICE)
        elif (
            not capabilities_of(client).compacts_inline
            and state.sessions.first_turn_history_owed_pending(session_key) is True
        ):
            # A FRESH provider session (a brand-new chat) that no ordinary
            # prompt has reached yet: the first-turn history debt is armed at
            # admission and only a kept non-slash turn settles it, so the
            # native session holds no conversation. kiro-cli ends this turn
            # with "Conversation too short to compact." and sends no
            # compaction status, the same shape as the replay case
            # above. Answer now rather than wait out the compaction timeout;
            # nothing was compacted, so the debt and skills context stay as
            # they are for the next prompt.
            _append_compaction_notice(state, slot, COMPACT_FRESH_SESSION_NOTICE)
        elif capabilities_of(client).compacts_inline:
            _restore_skills_context_after_compaction(turn)
            msg = "✅ Conversation compacted."
            _append_compaction_notice(state, slot, msg)
            state.broadcast_context_usage(slot.key, _context_usage_payload(slot.key, client))
        else:
            # Tell frontend to show compacting state and disable input
            logger.info("Deferred compaction: waiting for compaction result")
            state.broadcast_ws(
                "chat_message",
                {"slot": slot.key, "role": "compacting", "content": ""},
            )
            # kiro-cli fires compaction asynchronously after EVENT_COMPLETE —
            # just wait for the result without sending another prompt.
            compaction_result = await client.wait_for_compaction(
                timeout=state.sessions.compact_wait_budget_secs()
            )
            logger.info("Deferred compaction result: %s", compaction_result)
            if compaction_result["type"] == "completed":
                _restore_skills_context_after_compaction(turn)
                summary = redact(compaction_result.get("summary", ""))
                msg = (
                    f"✅ Conversation compacted: {summary}"
                    if summary
                    else "✅ Conversation compacted."
                )
            elif compaction_result["type"] == "failed":
                # The provider ships the reason on `failed` too, and every
                # other surface already tells the user what it was — Slack,
                # Telegram, Discord, and this dashboard's own AUTO-compact
                # notice (see chat_utils._compaction_notice_text). Dropping
                # it here left the one path a user takes deliberately as the
                # only one that says nothing, so a `/compact` that fails
                # because the conversation is too large is indistinguishable
                # from one that failed because the backend was unreachable —
                # and the user's next move differs in those two cases.
                #
                # Redacted with the same pair as the completed branch above:
                # the text is backend-echoed, so it is not trusted to be
                # free of credentials or exfiltration URLs even though the
                # provider already redacts once at its own boundary.
                error = redact(compaction_result.get("summary", ""))
                error = error.strip()
                if len(error) > _COMPACT_FAIL_REASON_MAX_CHARS:
                    # A notice is a one-line receipt, not a log: a provider
                    # that echoes a wall of text (a stack trace, a dumped
                    # payload) would otherwise push the whole transcript out
                    # of the reader's view.
                    error = error[:_COMPACT_FAIL_REASON_MAX_CHARS].rstrip() + "…"
                msg = f"❌ Compaction failed: {error}" if error else "❌ Compaction failed."
            else:
                msg = "⚠️ Compaction timed out."
            _append_compaction_notice(state, slot, msg)
            # Update the context meter from the provider's post-compaction
            # state. On success the provider has dropped its stale counts by
            # the time the completed status arrives (reset_after_compaction),
            # and wait_for_compaction grace-drains for kiro's fresh
            # post-compaction metadata (~1s after the status), which
            # re-derives REAL numbers against the kept served window.
            # _context_usage_payload ships those accurate counts when the
            # metadata has landed, and otherwise a `reset` frame (used == 0)
            # carrying whatever pct the provider currently reports, so the
            # frontend drops its stale counts and the meter self-corrects on
            # the next turn. On failure/timeout `used` is unchanged and still
            # valid, so the same call re-sends the real counts as-is.
            state.broadcast_context_usage(slot.key, _context_usage_payload(slot.key, client))
