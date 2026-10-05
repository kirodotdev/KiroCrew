"""The native Slack path's command surface: ``_handle_slash_command``'s dispatch over one
``_bang_<name>`` coroutine per owner and allowed-user ``!`` command, the sender gate and
``!compact`` routing ``handle_message`` calls, ``!compact`` itself, the ``sessions``
keyword predicate, and the ``spawn`` / ``run`` / ``cron`` keyword replies (whose text
lives in ``messaging.commands``).

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        _BANG_TO_SLASH,
        SESSIONS_INCLUDE_ENDED_ARGS,
        STOP_DECLINED_COMPACTING_TEXT,
        ConversationLog,
        CronService,
        KiroCrewConfig,
        SessionManager,
        SlackClientOps,
        SubagentManager,
        TaskRunner,
        _add_phase_reaction,
        _dashboard_state,
        _discover_project_agents,
        _get_default_agent,
        _is_slack_restricted,
        _list_all_agent_names,
        _mark_titled,
        _persist_channel_config,
        _reload_orch_cfg,
        _resolve_agent_name,
        _set_default_agent,
        _thread_agents,
        _thread_projects,
        _vc,
        build_timing_footer,
        compact_unsupported_backend,
        compact_unsupported_reply,
        compaction_in_flight,
        consume_stop_declined,
        cron_command_reply,
        decline_stop,
        deprecation_warning_block,
        describe_grant_lifetime,
        describe_new_grant,
        disable_yolo,
        get_dashboard_state,
        goal_actions,
        is_allowed_user,
        is_owner,
        is_sensitive_path,
        is_yolo_mode,
        logger,
        note_user_stop,
        redact_credentials,
        redact_exfiltration_urls,
        run_config_write,
        safety_override,
        sel,
        spawn_command_reply,
        task_command_reply,
        yolo_policy_permits,
    )


async def _handle_slash_command(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None = None,
) -> str | None:
    """Dispatch owner-only ``!commands``.  Returns a string (even empty) if handled, None if not."""

    cmd = cmd_text.split()[0].lower()

    # ── Deprecation warning for all bang commands ──
    slash_equiv = _BANG_TO_SLASH.get(cmd)
    if slash_equiv:
        logger.warning("Deprecated bang command %s used — suggest %s", cmd, slash_equiv)
        warn_block = deprecation_warning_block(cmd, slash_equiv)
        await slack.post_blocks(channel, [warn_block], f"{cmd} is deprecated", reply_ts)

    handler = {
        "!yolo": _bang_yolo,
        "!stop": _bang_stop,
        "!voice": _bang_voice,
        "!agent": _bang_agent,
        "!dashboard": _bang_dashboard,
        "!link-to-dashboard": _bang_link_to_dashboard,
        "!ta": _bang_thread_agent,
        "!project": _bang_project,
        "!allowlist": _bang_allowlist,
        "!channel": _bang_channel,
        "!title": _bang_title,
    }.get(cmd)
    if handler is not None:
        return await handler(
            cmd_text,
            slack,
            sessions,
            channel,
            reply_ts,
            msg_ts,
            session_key,
            user_id,
            conversation_log,
        )

    # Catch-all: unrecognized ! command — post error instead of falling through to LLM
    await slack.post_message(
        channel,
        f"❌ Unknown command `{cmd}`. Type `/kirocrew help` for available commands.",
        reply_ts,
    )
    return ""


# ── !yolo on / !yolo off / !yolo renew ──
async def _bang_yolo(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    parts = cmd_text.split()
    yolo_active = is_yolo_mode()
    if len(parts) >= 2 and parts[1].lower() == "off":
        # ``has_grant()``, NOT ``yolo_active``. The two ask different questions
        # and only one of them belongs here: ``is_yolo_mode`` is policy-filtered
        # ("may a tool be auto-approved"), while an explicit off asks "is there
        # something to tear down". While the governance verdict is momentarily
        # unknown the filtered answer is False, so this branch reported "already
        # off" and never called ``disable_yolo()`` -- and the retained grant then
        # resumed once the refresh settled, silently undoing the operator's
        # revocation. ``disable_yolo`` was already corrected to read
        # ``has_grant``; this is its CALLER, which was still gating it out.
        if safety_override().has_grant():
            disable_yolo()
            sel().log_api_access(
                caller=user_id,
                operation="slack.yolo_mode",
                outcome="allowed",
                source="slack",
                resources="yolo_off",
            )
            await slack.post_message(channel, "🔒 YOLO mode disabled.", reply_ts)
        else:
            await slack.post_message(channel, "YOLO mode is already off.", reply_ts)
    elif len(parts) >= 2 and parts[1].lower() == "on":
        if not yolo_active:
            # Off-loop like the sibling renew() below: activate() writes a
            # SEL event, and that filesystem I/O must not run on the loop.
            _result = await asyncio.to_thread(safety_override().activate, "slack")
            if not _result.active:
                # Arming can now be REFUSED -- an ``approval_modes`` deny of
                # ``yolo`` turns the mode off entirely. Reporting "enabled" over
                # a refused arm would tell the operator auto-approve is on while
                # every tool still stops to ask, and would audit it as allowed.
                #
                # NAME THE ACTUAL CAUSE. ``activate`` refuses for two different
                # reasons and they send the operator to two different places, so a
                # single message is wrong for one of them:
                #
                # | verdict   | why the arm failed          | where to look     |
                # |-----------|-----------------------------|-------------------|
                # | denied    | an admin's policy forbids   | the org's policy  |
                # | permitted | the fail-closed SEL audit   | the audit system  |
                #
                # Posting the policy line unconditionally would tell a solo
                # operator with no policy at all that a phantom organization had
                # blocked them, sending them hunting a file that does not exist
                # while the real fault went unnamed. The verdict is a memory read
                # (pushed when the ceiling was installed), so no thread.
                if not yolo_policy_permits():
                    _outcome, _error = "approval_mode_denied_by_policy", ("mode_disabled_by_policy")
                    _msg = "🔒 YOLO mode is disabled by your organization's policy."
                else:
                    _outcome, _error = "activation_failed", "audit_unavailable"
                    _msg = "❌ Failed to activate YOLO mode (audit system unavailable)."
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome=_outcome,
                    source="slack",
                    resources="yolo_on",
                    error=_error,
                )
                await slack.post_message(channel, _msg, reply_ts)
            else:
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.yolo_mode",
                    outcome="allowed",
                    source="slack",
                    resources="yolo_on",
                )
                await slack.post_message(
                    channel,
                    f"🔓 YOLO mode enabled ({describe_new_grant(_result.ttl)}).",
                    reply_ts,
                )
        else:
            await slack.post_message(
                channel, f"YOLO mode is already on ({describe_grant_lifetime()}).", reply_ts
            )
    elif len(parts) >= 2 and parts[1].lower() == "renew":
        # renew() audits fail-closed with a synchronous SEL write; keep
        # that filesystem I/O off the event loop.
        result = await asyncio.to_thread(safety_override().renew, "slack")
        if result.renewed:
            sel().log_api_access(
                caller=user_id,
                operation="slack.yolo_mode",
                outcome="renewed",
                source="slack",
                resources="yolo_renew",
            )
            await slack.post_message(
                channel,
                f"🔓 YOLO mode renewed (auto-expires in {result.ttl // 60}min).",
                reply_ts,
            )
        else:
            await slack.post_message(
                channel, "YOLO mode is not active. Use `!yolo on` to activate.", reply_ts
            )
    else:
        if yolo_active:
            status = f"ON 🔓 ({describe_grant_lifetime()})"
        else:
            status = "OFF 🔒"
        await slack.post_message(
            channel,
            f"YOLO mode: *{status}*. Use `!yolo on` / `!yolo off` / `!yolo renew`.",
            reply_ts,
        )
    return ""


# ── !stop — defensive fallback (normally intercepted in events.py
#    _route_message before handle_message is called) ──
async def _bang_stop(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    # Against the thread's OWNING session, where a linked thread's turns run.
    control_key = sessions.get_session_for_thread(reply_ts) or session_key
    goal_state = get_dashboard_state()

    def stop_reply(text: str) -> str:
        warning = goal_actions.goal_pause_warning(control_key, state=goal_state)
        return f"{text}\n\n{warning}" if warning else text

    force_stop = False
    if compaction_in_flight(sessions, control_key):
        force_stop = consume_stop_declined(control_key, user_id)
    if compaction_in_flight(sessions, control_key) and not force_stop:
        # Declined before the Stop is recorded: see slack/events.py. A repeat
        # within the window by the SAME presser is the second press and forces.
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!stop",
            tool_kind="command",
            outcome="compacting",
            metadata={"user": user_id, "channel": channel},
        )
        # Posted before the marker is armed: an undelivered warning plus an
        # armed escalation is a retry that hard-resets the session with this
        # user never told that it would. The post hands back the ts of what
        # landed, so a falsy one is a reply the user never saw.

        async def _say_declined() -> bool:
            return bool(await slack.post_message(channel, STOP_DECLINED_COMPACTING_TEXT, reply_ts))

        await decline_stop(control_key, user_id, _say_declined)
        return ""
    note_user_stop(sessions, control_key)
    has_session = sessions.has_session(control_key)
    if not has_session:
        await goal_actions.pause_session_goal(control_key, state=goal_state)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!stop",
            tool_kind="command",
            outcome="no_session",
            metadata={"user": user_id, "channel": channel},
        )
        await slack.post_message(channel, stop_reply("Nothing running."), reply_ts)
        return ""

    # Post ephemeral "Stopping…" block with Kill Now button
    from kiro_crew.slack.blocks import build_stopping_blocks

    await slack.post_ephemeral(
        channel,
        user_id,
        "Stopping…",
        blocks=build_stopping_blocks(control_key),
        thread_ts=reply_ts,
    )

    async def _on_soft() -> None:
        await slack.post_message(channel, stop_reply("⏹ Execution stopped."), reply_ts)

    async def _on_hard() -> None:
        await slack.post_message(
            channel, stop_reply("⛔ Execution stopped — session reset."), reply_ts
        )

    # ``preserve_queue`` with the force: the hard reset pops the session and
    # its queue, which in a shared thread holds co-tenants' messages;
    # ``stop_turn`` parks them for the successor instead.
    _kw = {"force": True, "preserve_queue": True} if force_stop else {}
    outcome = await sessions.stop_turn(
        control_key, on_soft=_on_soft, on_hard=_on_hard, goal_state=goal_state, **_kw
    )
    # If stop_turn returned "idle" (no active turn), neither callback
    # fired — dismiss the stale "Stopping…" ephemeral explicitly.
    if outcome == "idle":
        await slack.post_message(channel, stop_reply("Nothing running."), reply_ts)
    elif outcome == "compacting":
        # The race decline arms the marker too: the reply promises that a
        # repeat forces, so the repeat must find one -- after the reply
        # landed, never before it, and only when the post returns the ts of
        # a message that really landed.

        async def _say_declined_race() -> bool:
            return bool(await slack.post_message(channel, STOP_DECLINED_COMPACTING_TEXT, reply_ts))

        await decline_stop(control_key, user_id, _say_declined_race)
    sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="!stop",
        tool_kind="command",
        outcome=outcome,
        metadata={"user": user_id, "channel": channel},
    )
    return ""


# ── !voice on/off/global/<name> | engine/speed/pitch controls ──
async def _bang_voice(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    from kiro_crew.voice_reply import VALID_ENGINES, _validate_pitch, _validate_rate

    parts = cmd_text.split()
    arg = parts[1].lower() if len(parts) >= 2 else ""
    val = parts[2] if len(parts) >= 3 else ""
    if arg == "on":
        _vc.sessions.add(session_key)
        v = _vc.voices.get(session_key, _vc.default_voice)
        e = _vc.engines.get(session_key, _vc.default_engine)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!voice",
            tool_kind="command",
            outcome="voice_on",
            metadata={"user": user_id, "channel": channel},
        )
        await slack.post_message(channel, f"\U0001f50a Voice ON — *{v}* ({e})", reply_ts)
    elif arg == "off":
        _vc.sessions.discard(session_key)
        for d in (_vc.voices, _vc.engines, _vc.rates, _vc.pitches):
            d.pop(session_key, None)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!voice",
            tool_kind="command",
            outcome="voice_off",
            metadata={"user": user_id, "channel": channel},
        )
        await slack.post_message(channel, "\U0001f507 Voice OFF.", reply_ts)
    elif arg == "global":
        _vc.global_enabled = not _vc.global_enabled
        state = "ON \U0001f50a" if _vc.global_enabled else "OFF \U0001f507"
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!voice",
            tool_kind="command",
            outcome="voice_global_" + ("on" if _vc.global_enabled else "off"),
            metadata={"user": user_id, "channel": channel},
        )
        await slack.post_message(channel, f"Voice global: *{state}*", reply_ts)
    elif arg == "engine" and val:
        eng = val.lower()
        if eng not in VALID_ENGINES:
            await slack.post_message(
                channel,
                f"\u274c Invalid engine. Use: {', '.join(sorted(VALID_ENGINES))}",
                reply_ts,
            )
        else:
            _vc.engines[session_key] = eng
            _vc.sessions.add(session_key)
            await slack.post_message(channel, f"\U0001f50a Engine set to *{eng}*.", reply_ts)
    elif arg == "speed" and val:
        validated = _validate_rate(val)
        _vc.rates[session_key] = validated
        _vc.sessions.add(session_key)
        await slack.post_message(channel, f"\U0001f50a Speed set to *{validated}*.", reply_ts)
    elif arg == "pitch" and val:
        validated = _validate_pitch(val)
        _vc.pitches[session_key] = validated
        _vc.sessions.add(session_key)
        await slack.post_message(channel, f"\U0001f50a Pitch set to *{validated}*.", reply_ts)
    elif arg and arg not in ("engine", "speed", "pitch"):
        voice_name = parts[1]  # preserve original case
        _vc.sessions.add(session_key)
        _vc.voices[session_key] = voice_name
        await slack.post_message(channel, f"\U0001f50a Voice set to *{voice_name}*.", reply_ts)
    else:
        on = session_key in _vc.sessions or _vc.global_enabled
        v = _vc.voices.get(session_key, _vc.default_voice)
        e = _vc.engines.get(session_key, _vc.default_engine)
        r = _vc.rates.get(session_key, _vc.default_rate)
        p = _vc.pitches.get(session_key, _vc.default_pitch)
        await slack.post_message(
            channel,
            f"\U0001f50a Voice: *{'ON' if on else 'OFF'}*\n"
            f"\u2022 Voice: *{v}* | Engine: *{e}*\n"
            f"\u2022 Speed: *{r}* | Pitch: *{p}*\n"
            "`!voice <name>` `!voice engine <neural|generative|long-form>` "
            "`!voice speed <80%>` `!voice pitch <+10%>`",
            reply_ts,
        )
    await _add_phase_reaction(slack, channel, msg_ts, "done")
    return ""


# ── !agent <name> / !agent off — always global ──
async def _bang_agent(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    parts = cmd_text.split()
    if len(parts) == 1:
        name = _get_default_agent() or "kirocrew"
        await slack.post_message(
            channel,
            f"Current agent: *{name}*. Usage: `!agent <name>` or `!agent off`",
            reply_ts,
        )
        return ""
    if len(parts) != 2:
        await slack.post_message(channel, "Usage: `!agent <name>` or `!agent off`", reply_ts)
        return ""
    agent_name = parts[1]
    if agent_name.lower() in ("default", "off"):
        try:
            await run_config_write(_set_default_agent, "")
        except ValueError as e:
            await slack.post_message(channel, f"❌ {e}", reply_ts)
            return ""
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!agent",
            tool_kind="command",
            outcome="agent_reset",
            metadata={"user": user_id, "channel": channel},
        )
        await sessions.remove(session_key)
        await slack.post_message(channel, "🔄 Reset to default agent.", reply_ts)
        await _add_phase_reaction(slack, channel, msg_ts, "done")
        return ""
    resolved = await asyncio.to_thread(
        _resolve_agent_name, agent_name, _thread_projects.get(session_key)
    )
    if not resolved:
        names = await asyncio.to_thread(_list_all_agent_names)
        await slack.post_message(
            channel, f"❌ Unknown agent `{agent_name}`. Available: {names}", reply_ts
        )
        return ""
    try:
        await run_config_write(_set_default_agent, resolved)
    except ValueError as e:
        await slack.post_message(channel, f"❌ {e}", reply_ts)
        return ""
    sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="!agent",
        tool_kind="command",
        outcome="agent_switch",
        metadata={"agent": resolved, "user": user_id, "channel": channel},
    )
    await sessions.remove(session_key)
    await slack.post_message(channel, f"🔄 Switched to agent: *{resolved}*", reply_ts)
    await _add_phase_reaction(slack, channel, msg_ts, "done")
    return ""


# ── !dashboard [duration] ──
async def _bang_dashboard(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    from kiro_crew.dashboard.token_auth import parse_duration
    from kiro_crew.slack.allowlist import send_dashboard_link

    parts = cmd_text.split()
    ttl = 3600
    if len(parts) >= 2:
        parsed = parse_duration(parts[1])
        if parsed is None:
            await slack.post_message(
                channel,
                "Usage: `!dashboard [<N>h|<N>m]` — e.g. `!dashboard 2h`, `!dashboard 30m`",
                reply_ts,
            )
            return ""
        ttl = parsed

    url = await send_dashboard_link(slack, user_id, ttl)
    if url:
        await slack.post_message(channel, "🔗 Dashboard link sent via DM.", reply_ts)
    else:
        await slack.post_message(channel, "❌ Failed to send dashboard link.", reply_ts)
    return ""


# ── !link-to-dashboard -- import Slack thread into dashboard ──
async def _bang_link_to_dashboard(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    if not is_allowed_user(user_id):
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="slack",
            tool_name="link_to_dashboard",
            tool_kind="command",
            outcome="denied",
            metadata={"user_id": user_id, "channel": channel, "reason": "not_allowed_user"},
        )
        await slack.post_message(channel, "Not authorized.", reply_ts)
        return ""
    if not _dashboard_state or not hasattr(_dashboard_state, "get_or_create_slot"):
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="slack",
            tool_name="link_to_dashboard",
            tool_kind="command",
            outcome="failure",
            metadata={"user_id": user_id, "channel": channel, "reason": "no_dashboard"},
        )
        await slack.post_message(channel, "Dashboard not available.", reply_ts)
        return ""
    if reply_ts == msg_ts:
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="slack",
            tool_name="link_to_dashboard",
            tool_kind="command",
            outcome="failure",
            metadata={"user_id": user_id, "channel": channel, "reason": "not_in_thread"},
        )
        await slack.post_message(
            channel, "Use this command inside a thread to import it.", reply_ts
        )
        return ""
    # Fetch thread history and import to dashboard
    from kiro_crew.slack.interactions import _import_thread_to_slot

    slot = await _import_thread_to_slot(slack, _dashboard_state, channel, reply_ts)
    if not slot:
        sel().log_tool_invocation(
            session_key="",
            agent="kirocrew",
            source="slack",
            tool_name="link_to_dashboard",
            tool_kind="command",
            outcome="failure",
            metadata={"channel": channel, "thread_ts": reply_ts, "reason": "empty_thread"},
        )
        await slack.post_message(channel, "Could not fetch thread history.", reply_ts)
        return ""
    sel().log_tool_invocation(
        session_key=slot.key,
        agent="kirocrew",
        source="slack",
        tool_name="link_to_dashboard",
        tool_kind="command",
        outcome="success",
        metadata={
            "slot": slot.key,
            "channel": channel,
            "thread_ts": reply_ts,
            "msg_count": len(slot.messages),
        },
    )
    await slack.post_message(
        channel,
        f"Imported {len(slot.messages)} messages to dashboard session *{slot.key}*. Thread is now linked.",
        reply_ts,
    )
    return ""


# ── !ta <name> / !ta off — thread-scoped agent ──
async def _bang_thread_agent(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    parts = cmd_text.split()
    if len(parts) < 2:
        current = _thread_agents.get(session_key, "")
        if current:
            await slack.post_message(
                channel,
                f"Thread agent: *{current}*. `!ta off` to reset.",
                reply_ts,
            )
        else:
            await slack.post_message(
                channel,
                "No thread agent set. Usage: `!ta <name>` or `!ta off`",
                reply_ts,
            )
        return ""
    agent_name = parts[1]
    if agent_name.lower() in ("default", "off"):
        _thread_agents.pop(session_key, None)
        if conversation_log:
            try:
                await asyncio.to_thread(
                    conversation_log.update_metadata, session_key, {"agent": ""}
                )
            except Exception:
                logger.debug("Failed to clear agent in conversation log", exc_info=True)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!ta",
            tool_kind="command",
            outcome="agent_reset",
            metadata={"user": user_id, "channel": channel, "scope": "thread"},
        )
        await sessions.remove(session_key)
        await slack.post_message(channel, "🔄 Thread agent reset.", reply_ts)
        await _add_phase_reaction(slack, channel, msg_ts, "done")
        return ""
    resolved = await asyncio.to_thread(
        _resolve_agent_name, agent_name, _thread_projects.get(session_key)
    )
    if not resolved:
        names = await asyncio.to_thread(_list_all_agent_names)
        await slack.post_message(
            channel, f"❌ Unknown agent `{agent_name}`. Available: {names}", reply_ts
        )
        return ""
    _thread_agents[session_key] = resolved
    if conversation_log:
        try:
            await asyncio.to_thread(
                conversation_log.update_metadata, session_key, {"agent": resolved}
            )
        except Exception:
            logger.debug("Failed to persist agent to conversation log", exc_info=True)
    sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="!ta",
        tool_kind="command",
        outcome="agent_switch",
        metadata={"agent": resolved, "user": user_id, "channel": channel, "scope": "thread"},
    )
    await sessions.remove(session_key)
    await slack.post_message(channel, f"🔄 Thread agent: *{resolved}*", reply_ts)
    await _add_phase_reaction(slack, channel, msg_ts, "done")
    return ""


# ── !project <path> / !project off — thread-scoped agent-discovery dir ──
# NOTE: this only scopes which project-local .kiro agents are discoverable
# for !ta in this thread; it does NOT change the agent's working directory
# (cwd). Provider cwd plumbing is out of scope for this CR.
async def _bang_project(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    parts = cmd_text.split(maxsplit=1)
    if len(parts) < 2:
        current = _thread_projects.get(session_key, "")
        msg = (
            f"Thread agent-discovery project: `{current}`"
            if current
            else "No project set. Usage: `!project <path>` or `!project off`\n"
            "Scopes which project-local `.kiro` agents `!ta` can find — "
            "does not change the working directory."
        )
        await slack.post_message(channel, msg, reply_ts)
        return ""
    raw_path = parts[1].strip()
    if raw_path.lower() in ("off", "clear", "reset"):
        _thread_projects.pop(session_key, None)
        if conversation_log:
            try:
                await asyncio.to_thread(
                    conversation_log.update_metadata, session_key, {"project": ""}
                )
            except Exception:
                logger.debug("Failed to clear project in conversation log", exc_info=True)
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!project",
            tool_kind="command",
            outcome="project_cleared",
            metadata={"user": user_id, "channel": channel},
        )
        await sessions.remove(session_key)
        await slack.post_message(channel, "Thread project cleared.", reply_ts)
        return ""
    resolved = os.path.realpath(os.path.expanduser(raw_path))
    if is_sensitive_path(resolved):
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!project",
            tool_kind="command",
            outcome="project_denied_sensitive",
            metadata={"user": user_id, "channel": channel, "project": resolved},
        )
        await slack.post_message(
            channel, "Cannot use sensitive path as project directory.", reply_ts
        )
        return ""
    if not os.path.isdir(resolved):
        sel().log_tool_invocation(
            session_key=session_key,
            source="slack",
            tool_name="!project",
            tool_kind="command",
            outcome="project_denied_invalid",
            metadata={"user": user_id, "channel": channel, "project": resolved},
        )
        await slack.post_message(channel, f"Not a directory: `{resolved}`", reply_ts)
        return ""
    _thread_projects[session_key] = resolved
    if conversation_log:
        try:
            await asyncio.to_thread(
                conversation_log.update_metadata, session_key, {"project": resolved}
            )
        except Exception:
            logger.debug("Failed to persist project to conversation log", exc_info=True)
    sel().log_tool_invocation(
        session_key=session_key,
        source="slack",
        tool_name="!project",
        tool_kind="command",
        outcome="project_set",
        metadata={"user": user_id, "channel": channel, "project": resolved},
    )
    await sessions.remove(session_key)
    # Discover project-local agents: a directory listing of the checkout,
    # so off the loop like the metadata write above.
    project_agents = await asyncio.to_thread(
        _discover_project_agents, resolved, operation="slack_list_agents"
    )
    agent_info = ""
    if project_agents:
        names = ", ".join(
            f"`{s.stem.replace('.agent-spec', '') if '.agent-spec' in s.name else s.stem}`"
            for s in project_agents
        )
        agent_info = f"\nAgents found: {names} — use `!ta <name>` to switch"
    await slack.post_message(
        channel,
        f"Thread agent-discovery project: `{resolved}` "
        f"(scopes `!ta` agent lookup, not the working directory){agent_info}",
        reply_ts,
    )
    return ""


# ── !allowlist — multi-user access disabled ──
async def _bang_allowlist(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    await slack.post_message(
        channel,
        "⛔ Multi-user access is disabled for security. Only the owner can use Kiro Crew via Slack.",
        reply_ts,
    )
    return ""


# ── !channel always|mention|observe|off / !channel agent <name> (owner-only) ──
async def _bang_channel(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    if not is_owner(user_id):
        sel().log_api_access(
            caller=user_id,
            operation="slack.channel_config",
            outcome="denied",
            source="slack",
            resources=channel,
            error="not owner",
        )
        await slack.post_message(channel, "⛔ Only the bot owner can use `!channel`.", reply_ts)
        return ""
    from kiro_crew.config.loader import _VALID_ACTIVATIONS

    parts = cmd_text.split()
    if len(parts) == 1:
        cfg = KiroCrewConfig.load()
        ch_cfg = cfg.channel_config(channel)
        agent_info = f", agent=*{ch_cfg.agent}*" if ch_cfg.agent else ""
        await slack.post_message(
            channel,
            f"Channel `{channel}` activation: *{ch_cfg.activation}*{agent_info}\n"
            f"Usage: `!channel always|mention|observe|off` or `!channel agent <name|off>`",
            reply_ts,
        )
        return ""

    subcmd = parts[1].lower()

    # !channel agent <name|off>
    if subcmd == "agent":
        if len(parts) < 3:
            await slack.post_message(
                channel, "Usage: `!channel agent <name>` or `!channel agent off`", reply_ts
            )
            return ""
        agent_name = parts[2]
        if agent_name.lower() == "off":
            agent_name = ""
        else:
            resolved = await asyncio.to_thread(
                _resolve_agent_name, agent_name, _thread_projects.get(session_key)
            )
            if not resolved:
                names = await asyncio.to_thread(_list_all_agent_names)
                await slack.post_message(
                    channel,
                    f"Unknown agent `{agent_name}`. Available: {names}",
                    reply_ts,
                )
                return ""
            agent_name = resolved
        await run_config_write(_persist_channel_config, channel, agent=agent_name)
        _reload_orch_cfg()
        sel().log_api_access(
            caller=user_id,
            operation="slack.channel_agent",
            outcome="allowed",
            source="slack",
            resources=f"{channel}:{agent_name or 'default'}",
        )
        label = f"*{agent_name}*" if agent_name else "default"
        await slack.post_message(channel, f"Agent for this channel: {label}", reply_ts)
        return ""

    # !channel always|mention|observe|off
    if subcmd not in _VALID_ACTIVATIONS:
        await slack.post_message(
            channel,
            f"Invalid mode `{subcmd}`. Use: `always`, `mention`, `observe`, or `off`.",
            reply_ts,
        )
        return ""

    await run_config_write(_persist_channel_config, channel, activation=subcmd)
    _reload_orch_cfg()
    sel().log_api_access(
        caller=user_id,
        operation="slack.channel_activation",
        outcome="allowed",
        source="slack",
        resources=f"{channel}:{subcmd}",
    )
    await slack.post_message(channel, f"Channel activation set to *{subcmd}*.", reply_ts)
    return ""


# ── !title — set/generate Slack thread title ──
async def _bang_title(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> str:
    parts = cmd_text.split()
    title_text = " ".join(parts[1:]).strip() if len(parts) > 1 else ""
    if title_text:
        title_text, _ = redact_exfiltration_urls(title_text)
        title_text, _ = redact_credentials(title_text)
        await slack.set_thread_title(channel, session_key, title_text[:80])
        _mark_titled(session_key, "manual")
        if conversation_log and not _is_slack_restricted(session_key):
            try:
                await asyncio.to_thread(conversation_log.set_title, session_key, title_text[:80])
            except Exception:
                logger.debug(
                    "Failed to set conversation log title for %s", session_key, exc_info=True
                )
        sel().log_api_access(
            caller=user_id,
            operation="slack.thread_title",
            outcome="allowed",
            source="slack",
            resources=f"{channel}:{session_key}",
        )
        await _add_phase_reaction(slack, channel, msg_ts, "done")
    else:
        await slack.post_message(
            channel, "Usage: `!title <text>` — set a title for this thread.", reply_ts
        )
    return ""


async def _handle_compact_command(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
) -> None:
    """Trigger in-place ACP ``/compact`` on the current thread's session."""
    # Atomically take the turn semaphore for the WHOLE compaction, or refuse.
    # Slack dispatches each message as its own task (asyncio.create_task), so a
    # bare get_provider() + compact() would race a normal turn that holds the
    # session and interleave two prompts on one stdio channel — corrupting
    # session state (the reason Discord/Telegram guard the same way). Because
    # /compact routes through session/prompt, that collision surfaces
    # as "turn already active" and the except path would destroy a healthy
    # session; try_acquire() serializes against the in-flight turn and the
    # finally always releases.
    if not await sessions.try_acquire(session_key):
        if sessions.has_session(session_key):
            await slack.post_message(
                channel,
                "⏳ Still working on your last message — try `!compact` once it finishes.",
                reply_ts,
            )
        else:
            await slack.post_message(channel, "No active session to compact.", reply_ts)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="no_session",
            )
        return
    try:
        provider = sessions.get_provider(session_key)
        if not provider:
            await slack.post_message(channel, "No active session to compact.", reply_ts)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="no_session",
            )
            return

        # Capability gate, mirroring the dashboard's own gate: a
        # backend that cannot serve a manual /compact treats the prompt as
        # ordinary text and never answers, so dispatching would strand the
        # 120s wait below. Informational, never an error.
        unsupported = compact_unsupported_backend(provider)
        if unsupported:
            await slack.post_message(channel, compact_unsupported_reply(unsupported), reply_ts)
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="auto_managed_backend",
                metadata={"backend": unsupported},
            )
            return

        _t0 = time.monotonic()

        # --- Phase 1: Pre-compaction UI (cosmetic — log failures, don't abort) ---
        try:
            await slack.add_reaction(channel, msg_ts, "recycle")
            await slack.post_message(channel, "🔄 Compacting context…", reply_ts)
        except Exception:
            logger.debug("Pre-compact UI failed for %s", session_key, exc_info=True)

        # --- Phase 2: Actual compaction (failures warrant error + session teardown) ---
        result_text: str | None = None
        outcome = "unknown"
        try:
            # Compaction runs over the prompt transport:
            # provider.compact() drives /compact via session/prompt (the
            # commands/execute path does NOT run compaction — it returns with
            # no status). Bound compact()'s prompt turn here,
            # then let wait_for_compaction() own its OWN deadline for a status
            # emitted async after end_turn — it must NOT be nested inside
            # another timeout, or the graceful "timed out" branch is
            # unreachable and a slow-but-healthy session gets destroyed.
            await asyncio.wait_for(provider.compact(), timeout=120)
            cr = await provider.wait_for_compaction(timeout=sessions.compact_wait_budget_secs())
            if cr["type"] == "completed":
                # ``summary`` is model-facing compacted context, not a
                # user-facing receipt. Never publish its orchestration text.
                result_text = "✅ Context compacted."
                outcome = "completed"
            elif cr["type"] == "failed":
                error = cr.get("summary", "")
                result_text = f"❌ Compaction failed: {error}" if error else "❌ Compaction failed."
                outcome = "failed"
            else:
                result_text = "⚠️ Compaction timed out."
                outcome = "timeout"
        except Exception:
            logger.warning("Compact command failed for %s", session_key, exc_info=True)
            try:
                await slack.post_message(channel, "❌ Compaction failed unexpectedly.", reply_ts)
            except Exception:
                logger.debug("Failed to post compact error for %s", session_key, exc_info=True)
            # Drop the wedged native conversation, NOT the session's channel
            # identity: the map entry carries the thread linkage that
            # ``get_session_for_thread`` routes every later reply through, so a
            # full ``destroy`` would fork this thread into a fresh session with
            # none of its context. Housekeeping never unlinks (see
            # ``SessionMap.prune`` and ``SessionManager._recycle_held``).
            try:
                await sessions.discard_conversation(session_key)
            except Exception:
                logger.warning(
                    "Failed to discard conversation %s after compact failure",
                    session_key,
                    exc_info=True,
                )
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="failed",
                error="exception",
            )
            try:
                await slack.remove_reaction(channel, msg_ts, "recycle")
                await _add_phase_reaction(slack, channel, msg_ts, "done")
            except Exception:
                pass
            return

        # --- Phase 3: Post-compaction reporting (log failures, don't mislead) ---
        try:
            result_text, _ = redact_exfiltration_urls(result_text)
            result_text, _ = redact_credentials(result_text)
            await slack.post_message(channel, result_text, reply_ts)

            elapsed = time.monotonic() - _t0
            footer_blocks, footer_text = build_timing_footer(elapsed)
            await slack.post_blocks(channel, footer_blocks, footer_text, reply_ts)
        except Exception:
            logger.debug("Post-compact reporting failed for %s", session_key, exc_info=True)

        try:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome=outcome,
            )
        except Exception:
            logger.debug("Failed to log compact outcome for %s", session_key, exc_info=True)
        try:
            await slack.remove_reaction(channel, msg_ts, "recycle")
            await _add_phase_reaction(slack, channel, msg_ts, "done")
        except Exception:
            pass
    finally:
        sessions.release(session_key)


def _is_sessions_keyword(text: str) -> bool:
    """True when the whole stripped, lower-cased message is the ``sessions``
    keyword, on its own or with its one argument.

    The ONE predicate shared by the native ``handle_message`` branch, the
    transport ``maybe_handle_keyword_command`` branch, and the linked-thread
    fall-through in ``maybe_route_linked_thread`` — keeping all three sites on
    one helper guarantees the intercept matches exactly what the keyword
    branches match, so the keyword cannot be swallowed by a linked thread.

    The argument is matched here as well as in
    :func:`kiro_crew.slack.sessions_view.sessions_include_ended`, and it has to
    be: a message this predicate rejects is never routed to the sessions
    handler at all, so ``sessions all`` would reach the agent as ordinary chat
    and the opt-in would have no way to be typed.
    """
    words = text.strip().lower().split()
    if not words or words[0] != "sessions":
        return False
    if len(words) == 1:
        return True
    return len(words) == 2 and words[1] in SESSIONS_INCLUDE_ENDED_ARGS


async def _handle_spawn_command(
    text: str, manager: SubagentManager, session_key: str = ""
) -> str | None:
    """Intercept spawn/bg keyword commands. Returns reply or None.

    Async so the accept runs through ``spawn_async`` on the task store's writer
    thread instead of taking ``BEGIN IMMEDIATE`` on the Slack gateway's loop.
    """
    return await spawn_command_reply(text, manager, session_key)


async def _handle_cron_command(
    text: str, cron_service: CronService, channel: str, thread_ts: str, user_id: str = ""
) -> str | None:
    """Handle cron keyword commands. Returns reply or None.

    Async so the store mutators (remove/pause/resume) run through the
    event-loop-safe ``*_async`` variants instead of parking the Slack gateway
    loop on the store lock; a contended store yields a "busy, retry" reply
    rather than a stall.

    ``user_id`` is the Slack caller, threaded through so the destructive
    branches can attribute their SEL audit events to the human who issued
    the command (per-caller identity, matching the dashboard/MCP/CLI paths).
    """
    # ``source``/``caller`` carry that attribution into the shared remove-all
    # audit, which is where the event is emitted.
    return await cron_command_reply(text, cron_service, source="slack", caller=user_id)


async def _handle_run_command(
    text: str,
    runner: TaskRunner,
    slack: SlackClientOps,
    channel: str,
    thread_ts: str,
    *,
    session_key: str = "",
) -> str | None:
    """Intercept 'run <path>' keyword commands. Returns reply or None.

    ``slack`` / ``channel`` / ``thread_ts`` are unused and were already unused
    before the reply text was hoisted; they stay because this is the positional
    shape ``maybe_handle_keyword_command`` and several suites call.

    ``session_key`` is what lets a task that later blocks on an approval report back
    to the conversation the operator is watching, instead of only to the owner DM.
    Keyword-only with a default so the ~25 existing positional call sites are
    unchanged; omitting it reproduces the old owner-DM-only behaviour exactly.
    """
    return await task_command_reply(text, runner, session_key=session_key)


async def _route_bang_command(
    cmd_text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None,
) -> bool:
    """Run ``!compact`` or a ``!`` command, with the sender gate each one carries.

    *cmd_text* is the message with any leading bot mention and privacy modifier
    already stripped. Returns ``True`` when the message was a command the caller must
    not hand to the model -- run, denied, or answered -- and ``False`` when it falls
    through to the rest of ``handle_message`` (no ``!`` prefix, or a command the
    dispatcher did not claim).
    """
    if cmd_text.strip().lower() == "!compact":
        if is_owner(user_id) or is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.compact_command",
                outcome="allowed",
                source="slack",
                resources=channel,
            )
            await _handle_compact_command(slack, sessions, channel, reply_ts, msg_ts, session_key)
            return True
        else:
            sel().log_tool_invocation(
                session_key=session_key,
                source="slack",
                tool_name="compact",
                tool_kind="command",
                outcome="denied",
                error=f"unauthorized user {user_id}",
            )
            await slack.post_message(channel, "⛔ Not authorized to compact.", reply_ts)
            return True  # deny-by-default: do not fall through

    # ── Owner commands: all "!" prefixed messages are reserved for owner ──
    if cmd_text.startswith("!"):
        # !dashboard and !stop are available to any allowed user
        _cmd_word = cmd_text.split()[0]
        if _cmd_word in ("!dashboard", "!stop", "!title"):
            if is_owner(user_id) or is_allowed_user(user_id):
                reply = await _handle_slash_command(
                    cmd_text,
                    slack,
                    sessions,
                    channel,
                    reply_ts,
                    msg_ts,
                    session_key,
                    user_id,
                    conversation_log=conversation_log,
                )
                if reply is not None:
                    return True
            else:
                sel().log_api_access(
                    caller=user_id,
                    operation="slack.allowed_command",
                    outcome="denied",
                    source="slack",
                    resources=_cmd_word,
                    error="unauthorized sender",
                )
                await slack.post_message(channel, "⛔ Not authorized.", reply_ts)
                return True
        # All other ! commands are owner-only
        elif not is_owner(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.owner_command",
                outcome="denied",
                source="slack",
                resources=_cmd_word,
                error="unauthorized sender",
            )
            await slack.post_message(channel, "⛔ Owner-only command.", reply_ts)
            return True
        else:
            reply = await _handle_slash_command(
                cmd_text,
                slack,
                sessions,
                channel,
                reply_ts,
                msg_ts,
                session_key,
                user_id,
                conversation_log=conversation_log,
            )
            if reply is not None:
                return True
    return False
