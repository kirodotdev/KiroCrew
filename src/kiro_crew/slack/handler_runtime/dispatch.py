"""What a native Slack message does before a turn opens.

``_dispatch_before_turn`` is ``handle_message``'s dispatch between admitting a message
and opening its turn: the linked-thread hand-off, a hook's canned reply, the ``status``
and ``sessions`` keywords, the privacy modifiers, the ``!`` commands and the keyword
commands both Slack paths share, each of which answers and ends the message without
the model, and then the first expiry of the thread's live OPTIONS control.
``maybe_handle_keyword_command`` (shared with the messaging-transport path) and
``_handle_sessions_command`` are the keyword handlers it calls.

Reads off the ``_SlackTurn``: the message, its thread keys and the services; writes back
the resolved agent and the text as the modifiers left it.

Composed onto :mod:`kiro_crew.slack.handler`; see
:mod:`kiro_crew.slack.handler_runtime`.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from kiro_crew.slack.handler import (
        HOOK_REPLY,
        ConversationLog,
        CronService,
        DashboardState,
        SessionManager,
        SlackClientOps,
        Stats,
        SubagentManager,
        TaskRunner,
        _build_sessions_blocks,
        _collect_recent_sessions_off_loop,
        _dashboard_state,
        _get_default_agent,
        _handle_cron_command,
        _handle_run_command,
        _handle_spawn_command,
        _is_sessions_keyword,
        _is_slack_restricted,
        _message_surface_limit,
        _route_bang_command,
        _SlackTurn,
        _thread_agents,
        await_replay_gap,
        current_context,
        expire_slack_options,
        get_dashboard_state,
        is_allowed_user,
        is_owner,
        logger,
        maybe_apply_privacy_modifiers,
        maybe_route_linked_thread,
        redact_credentials,
        redact_exfiltration_urls,
        save_conversation_turn_off_loop,
        sel,
        sessions_include_ended,
        slack_cfg,
    )


async def _dispatch_before_turn(turn: _SlackTurn) -> bool:
    """Answer a message that needs no turn: True when it goes on to start one.

    What ``handle_message`` does between admitting a message and opening its turn, in
    order: the linked-thread hand-off to a dashboard slot, a hook's canned reply, the
    ``status`` and ``sessions`` keywords, the ``!temporary`` / ``!incognito`` modifiers,
    the ``!`` commands, the ``spawn`` / ``run`` / ``cron`` keywords, and then, below
    every one of those, the first expiry of the thread's live OPTIONS control.
    """
    slack = turn.slack
    sessions = turn.sessions
    channel = turn.channel
    text = turn.text
    thread_ts = turn.thread_ts
    msg_ts = turn.msg_ts
    user_id = turn.user_id
    context_builder = turn.context_builder
    cron_service = turn.cron_service
    conversation_log = turn.conversation_log
    subagent_manager = turn.subagent_manager
    task_runner = turn.task_runner
    channel_agent = turn.channel_agent
    target_slot_name = turn.target_slot_name
    route_pinned = turn.route_pinned
    reply_ts = turn.reply_ts
    session_key = turn.session_key

    # Resolve agent early so ALL persist paths (hook auto-reply, command
    # intercepts, review-mode drafts, main LLM path) can forward it.
    _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None

    # ── Linked thread intercept: route to dashboard slot if linked ──
    # Resolved from the NAME captured when the answer was accepted, not from the
    # thread's current owner: the name survives a link change, a live slot object
    # would not tell us whether it is still the right destination. A pinned name
    # that does not resolve falls through to normal handling rather than
    # inventing a target.
    _target_slot = None
    if route_pinned and target_slot_name and _dashboard_state:
        _target_slot = getattr(_dashboard_state, "_slots", {}).get(target_slot_name)

    if await maybe_route_linked_thread(
        text,
        session_key,
        user_id,
        channel,
        slack,
        reply_ts,
        target_slot=_target_slot,
        route_pinned=route_pinned,
    ):
        return False

    logger.info(
        "🔍 handle_message: thread_ts=%s msg_ts=%s → session_key=%s channel=%s",
        thread_ts,
        msg_ts,
        session_key,
        channel,
    )

    # ── Hook: check for auto-reply before touching ACP ──
    if context_builder:
        hook_result = context_builder.hooks.on_message(text)
        if hook_result.action == HOOK_REPLY:
            await slack.post_message(channel, hook_result.text, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                # After the reply is posted but BEFORE the record is written:
                # an older message in this thread may be between its reset and
                # its compaction replay, and the transcript must show that turn
                # first, as the thread does.
                await await_replay_gap(sessions, session_key)
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    hook_result.text,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return False

    # ── Status keyword: reply with stats summary ──
    if text.strip().lower() == "status":
        # Identity status via the active PlatformContext (Default == OSS no-op
        # stub returning ""; an enterprise companion returns the real SSO line).
        sso_line = await current_context().identity.status_line(prefix=" · sso")
        await slack.post_message(channel, Stats().summary() + sso_line, reply_ts)
        return False

    # ── Sessions keyword: list recent sessions ──
    if _is_sessions_keyword(text):
        if is_owner(user_id) or is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="allowed",
                source="slack",
                resources=channel,
            )
            await _handle_sessions_command(
                text.strip(),
                slack,
                channel,
                reply_ts,
                msg_ts,
                session_key,
                conversation_log,
                sessions=sessions,
            )
        else:
            # Deny-by-default: unauthorized callers must be audited (so the
            # security pipeline can see attempted access) and given an
            # explicit denial — silent return masks the access attempt.
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="denied",
                source="slack",
                resources=channel,
                error="unauthorized caller",
            )
            await slack.post_message(channel, "_Permission denied._", reply_ts)
        return False

    # Strip leading bot mention from app_mention events so the ! prefix is exposed.
    # DM:       "!agent foo"                    → "!agent foo"       (no-op)
    # @mention: "<@UBOT|kirocrew> !agent foo"   → "!agent foo"      (strip prefix)
    _cmd_text = re.sub(r"^<@[A-Z0-9]+(?:\|[^>]*)?>\s*", "", text.strip())

    # ── !temporary / !incognito privacy modifiers (shared with transport) ──
    text, _cmd_text, _only_modifier = await maybe_apply_privacy_modifiers(
        text, _cmd_text, session_key, user_id, channel, slack, sessions, reply_ts
    )
    if _only_modifier:
        return False

    # ── !compact and the owner / allowed-user ``!`` commands ──
    # The routing lives in handler_runtime/commands.py (``_route_bang_command``); a
    # message it answers (run, denied, or replied to) never reaches the model.
    if await _route_bang_command(
        _cmd_text,
        slack,
        sessions,
        channel,
        reply_ts,
        msg_ts,
        session_key,
        user_id,
        conversation_log,
    ):
        return False

    # ── Path-independent keyword commands: spawn/run/cron ──
    # ``sessions`` is deliberately excluded here (handle_sessions=False): the
    # native path keeps its own earlier ``sessions`` block above so that the
    # ``!temporary``/``!incognito`` modifier rewrites can't turn a modified
    # message into a bare ``sessions`` match. The transport path (which has no
    # modifier machinery) handles all four via the same helper.
    if await maybe_handle_keyword_command(
        text,
        slack,
        sessions,
        channel,
        reply_ts,
        msg_ts,
        session_key,
        user_id,
        conversation_log,
        subagent_manager=subagent_manager,
        task_runner=task_runner,
        cron_service=cron_service,
        handle_sessions=False,
        channel_agent=channel_agent,
    ):
        return False

    # A new turn supersedes whatever question the previous one ended on, so any
    # OPTIONS control still live in this thread stops being answerable.
    #
    # Placed HERE, below every short-circuit above, because only a message that
    # actually starts a turn supersedes anything. ``status``, a permission
    # denial, a modifier-only message, a hook's canned reply and the keyword
    # commands all answer and return WITHOUT running the agent, so the
    # conversation has not moved and the pending question is still the one being
    # waited on. Expiring for those spends a LIVE control and leaves valid
    # choices unanswerable — the exact inverse of the stale click this lifecycle
    # exists to prevent. The denial case matters most: an unauthorized caller in
    # the thread must not be able to destroy the owner's pending question.
    # Keeping this at one point below the short-circuits, rather than guarding
    # each of them, means a shortcut added later inherits the right behaviour.
    #
    # Resolve the OWNING session, not the ``slack:<ts>`` key ``handle_message`` derived: the
    # control is recorded under whichever session owns the thread, and for a
    # dashboard-linked thread that is its ``dashboard:chat-N`` key — the same
    # distinction the linked-thread lookup relies on. Expiring under the wrong
    # key silently no-ops and leaves the control clickable.
    await expire_slack_options(
        cast("DashboardState | None", get_dashboard_state()),
        sessions.get_session_for_thread(reply_ts) or session_key,
    )

    # What ``_run_turn`` reads off the turn: the agent resolved here, and the text as
    # the privacy modifiers left it.
    turn._agent = _agent
    turn.text = text
    return True


async def maybe_handle_keyword_command(
    text: str,
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    user_id: str,
    conversation_log: ConversationLog | None = None,
    *,
    subagent_manager: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    cron_service: CronService | None = None,
    handle_sessions: bool = True,
    channel_agent: str | None = None,
) -> bool:
    """Intercept the path-independent keyword commands.

    These are plain (non-``!``) keyword commands that must behave identically
    on both the native ``handle_message`` path and the messaging-transport
    ``handle_message_transport`` path: ``sessions``, ``spawn <task>``,
    ``run <spec>`` and natural-language ``cron`` wakeups.

    Returns ``True`` when the message was handled as a keyword command — the
    caller MUST then ``return`` without starting an LLM turn. Returns ``False``
    when the message is not a keyword command and normal routing continues.

    ``!``-bang commands are intentionally NOT handled here; they stay in
    ``handle_message`` (owner/allowed gating, mention stripping, modifiers) and
    are being deprecated in favour of slash commands. Slash commands are
    already path-independent (handled upstream of the native-vs-transport gate),
    so they need no porting.

    *handle_sessions* lets the native path opt out of the ``sessions`` branch
    (it keeps its own earlier, position-sensitive ``sessions`` block so that
    ``!temporary``/``!incognito`` modifier rewrites cannot turn a modified
    message into a bare ``sessions`` match). The transport path has no such
    modifier machinery, so it uses the default and handles all four commands.
    """
    # Resolve the agent so the command-intercept persists record the real agent
    # name in session metadata (thread override, then channel override, then
    # global default), matching handle_message's main path.
    _agent = _thread_agents.get(session_key) or channel_agent or _get_default_agent() or None
    # ── Sessions keyword: list recent sessions (owner/allowed only) ──
    if handle_sessions and _is_sessions_keyword(text):
        if is_owner(user_id) or is_allowed_user(user_id):
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="allowed",
                source="slack",
                resources=channel,
            )
            await _handle_sessions_command(
                text.strip(),
                slack,
                channel,
                reply_ts,
                msg_ts,
                session_key,
                conversation_log,
                sessions=sessions,
            )
        else:
            # Deny-by-default: unauthorized callers must be audited (so the
            # security pipeline can see attempted access) and given an
            # explicit denial — silent return masks the access attempt.
            sel().log_api_access(
                caller=user_id,
                operation="slack.sessions_command",
                outcome="denied",
                source="slack",
                resources=channel,
                error="unauthorized caller",
            )
            await slack.post_message(channel, "_Permission denied._", reply_ts)
        return True

    # ── Subagent spawn: "spawn <task>" (before cron to avoid NL overlap) ──
    if subagent_manager:
        spawn_reply = await _handle_spawn_command(text, subagent_manager, session_key)
        if spawn_reply:
            await slack.post_message(channel, spawn_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                # Offloaded via the shared choke point -- see
                # save_conversation_turn_off_loop for why every async caller must.
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    spawn_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    # ── Task runner: "run <spec-path>" ──
    if task_runner:
        run_reply = await _handle_run_command(
            text, task_runner, slack, channel, reply_ts, session_key=session_key
        )
        if run_reply:
            await slack.post_message(channel, run_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    run_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    # ── Natural language cron: intercept wakeup patterns ──
    if cron_service:
        cron_reply = await _handle_cron_command(
            text, cron_service, channel, reply_ts, user_id=user_id
        )
        if cron_reply:
            await slack.post_message(channel, cron_reply, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    cron_reply,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
                )
            return True

    return False


async def _handle_sessions_command(
    cmd_text: str,
    slack: SlackClientOps,
    channel: str,
    reply_ts: str,
    msg_ts: str,
    session_key: str,
    conversation_log: ConversationLog | None,
    *,
    sessions: SessionManager | None = None,
) -> None:
    """Handle the ``sessions`` keyword in DMs.

    Delegates to
    :func:`kiro_crew.slack.sessions_view._collect_recent_sessions_off_loop`
    and :func:`kiro_crew.slack.sessions_view._build_sessions_blocks` so the
    keyword, the ``/<command> sessions`` slash command, and the App Home Tab
    all render the same Block Kit content with the same Resume button wiring.

    *cmd_text* is the message as typed; ``sessions all`` / ``sessions ended``
    asks for rows the user has dismissed with End, which are otherwise left out.
    """
    include_ended = sessions_include_ended(cmd_text)
    # Wrap the collector so a transient OSError still produces a SEL audit
    # entry. Without this, an IO failure would skip the audit entirely and
    # the access attempt would be invisible to the security pipeline.
    # Mirrors the slash and Home Tab error-path patterns.
    try:
        rows = await _collect_recent_sessions_off_loop(
            sessions,
            limit=_message_surface_limit(slack_cfg().slack.sessions_limit),
            include_ended=include_ended,
        )
    except Exception as exc:
        # Redact-then-truncate: redact() first so credential / exfil
        # patterns aren't split mid-string by the truncation step.
        redacted_exc, _ = redact_exfiltration_urls(str(exc))
        redacted_exc, _ = redact_credentials(redacted_exc)
        sel().log_api_access(
            caller=session_key,
            operation="slack.sessions_data_access",
            outcome="error",
            source="slack",
            resources="0 sessions read (collector failed)",
            error=redacted_exc[:200],
        )
        logger.exception("sessions keyword: collector failed for session_key %s", session_key)
        await slack.post_message(channel, "_Sessions unavailable._", reply_ts)
        return

    sel().log_api_access(
        caller=session_key,
        operation="slack.sessions_data_access",
        outcome="allowed",
        source="slack",
        resources=f"{len(rows)} sessions read",
    )

    if not rows:
        await slack.post_message(channel, "_No recent sessions._", reply_ts)
        return

    blocks = _build_sessions_blocks(rows)
    await slack.post_blocks(channel, blocks, "Recent sessions:", reply_ts)
