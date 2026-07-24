"""Full new-path dispatch: SlackTransport → TurnDriver → SlackRenderer.

When ``messaging.use_transport`` is True, events.py routes inbound Slack
messages here instead of directly calling ``handle_message``. This exercises
the entire messaging abstraction end-to-end:

  SlackTransport.receive() → authorize → normalize
  → dispatch callback:
      session acquire → context build → TurnDriver.run(provider, renderer)
      → SlackRenderer renders to Slack API
      → conversation log + cleanup

The old ``handle_message`` is completely bypassed. Dashboard link paths are
unaffected (they don't go through events.py Slack dispatch).
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import HOOK_REPLY, TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.llm_helpers import save_conversation_turn
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.messaging.link import canonical_key
from kiro_crew.platform import current_context
from kiro_crew.sel import sel
from kiro_crew.slack.handler import (
    _get_default_agent,
    _hydrate_conv_flags,
    _hydrate_thread_overrides,
    _is_slack_restricted,
    _should_auto_approve_spawn,
    _thread_agents,
    is_slack_session_trusted,
    maybe_apply_privacy_modifiers,
    maybe_handle_keyword_command,
    maybe_route_linked_thread,
)
from kiro_crew.slack.renderer import SlackApprovalDecider, SlackRenderer
from kiro_crew.stats import Stats

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.cron import CronService
    from kiro_crew.history import ConversationLog, HistoryConsolidator
    from kiro_crew.providers.base import LLMProvider
    from kiro_crew.session import SessionManager
    from kiro_crew.slack.client import SlackClientOps
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.taskrunner import TaskRunner

logger = logging.getLogger(__name__)

#: Canonical kiro-cli agent name for KiroCrew. Used as the final agent
#: fallback so the transport session loads the same MCP surface (incl. the
#: kirocrew-core server that provides ``spawn_run``) as the dashboard/native
#: path. Without it, an empty ``agent.default_agent`` config makes kiro-cli
#: launch under its bare built-in default (no kirocrew-core -> no spawn_run).
#: Mirrors the native path's fallback (``handler.py``: ``_get_default_agent()
#: or "kirocrew"``) and the many ``agent="kirocrew"`` call sites.
_DEFAULT_KIROCREW_AGENT = "kirocrew"


async def handle_message_transport(
    slack: SlackClientOps,
    sessions: SessionManager,
    channel: str,
    text: str,
    thread_ts: str | None,
    msg_ts: str,
    user_id: str,
    *,
    context_builder: ContextBuilder | None = None,
    conversation_log: ConversationLog | None = None,
    approval_mode: str = APPROVAL_INTERACTIVE,
    agent_override: str | None = None,
    subagent_manager: SubagentManager | None = None,
    task_runner: TaskRunner | None = None,
    cron_service: CronService | None = None,
    reactions_enabled: bool = True,
    show_thinking: bool = True,
    consolidator: HistoryConsolidator | None = None,
    user_display_name: str | None = None,
) -> None:
    """Drive a Slack message through the new transport path end-to-end.

    This replaces handle_message when the feature flag is on. It uses
    TurnDriver + SlackRenderer instead of the inline stream loop.
    """
    Stats().inc_message_received()
    _t0 = time.monotonic()
    # Same key discipline as native handle_message: reply_ts is the bare Slack
    # thread timestamp (posting + thread-index key); session_key is the
    # canonical namespaced form (registry, conversation log, thread overrides
    # shared with handler.py module dicts).
    reply_ts = thread_ts or msg_ts
    session_key = canonical_key(reply_ts)

    # ── Re-hydrate durable thread state FIRST (mirrors native ordering) ──
    # The per-thread agent/project overrides AND the durable incognito/temporary
    # privacy flags must be restored before ANY early path consults
    # _is_slack_restricted — the hook auto-reply, the !temporary/!incognito
    # modifiers, and the keyword commands all gate conversation-log writes on it.
    # After a gateway restart the in-memory flag maps (_thread_incognito /
    # _thread_temporary) start empty; hydrating late (at session acquisition, as
    # this used to) let a hook/spawn/cron turn on a previously-incognito thread
    # get logged before the durable flag was reloaded. Native hydrates right
    # after session_key (handler.py:2449-2450), so we match it. Idempotent:
    # _hydrate_thread_overrides guards repeated I/O per session.
    _hydrate_thread_overrides(session_key, conversation_log)
    _hydrate_conv_flags(sessions, session_key)

    # Entry marker: lets operators confirm the NEW transport path handled a
    # message (grep gateway.log for "transport_dispatch: handling"). Fires for
    # every message including the status/ping shortcuts below.
    logger.info(
        "transport_dispatch: handling message session=%s user=%s", session_key, user_id
    )

    # ── Linked thread intercept: route to a linked dashboard slot if any ──
    # Shared with native handle_message so a thread linked via
    # /kirocrew link-to-dashboard routes into its dashboard slot (with the same
    # auth recheck + bang fall-through) instead of spawning a fresh session.
    if await maybe_route_linked_thread(text, session_key, user_id, channel, slack, reply_ts):
        return

    # ── Hook: auto-reply before touching the LLM (mirrors native) ──
    # A HOOK_REPLY from the context builder's hooks short-circuits the turn: post
    # the canned reply, log it, and return WITHOUT spawning an LLM session — same
    # as native handle_message. Without this the transport path would start a
    # full (billable, slower) turn for a message a hook already answers.
    if context_builder:
        hook_result = context_builder.hooks.on_message(text)
        if hook_result.action == HOOK_REPLY:
            await slack.post_message(channel, hook_result.text, reply_ts)
            if conversation_log and not _is_slack_restricted(session_key):
                save_conversation_turn(
                    conversation_log,
                    session_key,
                    text,
                    hook_result.text,
                    source_thread=session_key,
                    source_user=user_id,
                )
            return

    # ── Quick shortcuts (no session needed) ──
    _lower = text.strip().lower()
    if _lower == "status":
        # Use the platform identity seam (native migrated to this) rather than
        # importing the OSS SSO stub directly — keeps the CPP boundary intact
        # and returns the real SSO line under the enterprise companion context.
        mw = await current_context().identity.status_line(prefix=" · sso")
        await slack.post_message(channel, Stats().summary() + mw, reply_ts)
        return
    if _lower == "ping":
        await slack.post_message(channel, "pong", reply_ts)
        return

    # ── !temporary / !incognito privacy modifiers (shared with native) ──
    # Strip + apply the modifier (set the durable flag) BEFORE anything reaches
    # the LLM, so incognito/temporary actually take effect on the default-ON
    # transport path and the modifier token never leaks into the prompt. Mirrors
    # native ordering (modifiers before spawn/run/cron).
    _cmd_text = re.sub(r"^<@[A-Z0-9]+(?:\|[^>]*)?>\s*", "", text.strip())
    text, _cmd_text, _only_modifier = await maybe_apply_privacy_modifiers(
        text, _cmd_text, session_key, user_id, channel, slack, sessions, reply_ts
    )
    if _only_modifier:
        # Message was nothing but the modifier(s) — no LLM turn.
        return

    # ── Path-independent keyword commands: sessions / spawn / run / cron ──
    # These need no LLM session (they dispatch to the subagent/task/cron
    # services or render the sessions view), so they run here alongside the
    # status/ping shortcuts. This is the SAME helper the native path uses, so
    # both paths share one implementation. handle_sessions defaults to True
    # here (unlike native, which keeps its own earlier sessions block) because
    # the transport path has no !temporary/!incognito modifier machinery that
    # could rewrite text into a bare "sessions" match.
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
        channel_agent=agent_override,
    ):
        return

    client: LLMProvider | None = None
    _acquired = False
    renderer: SlackRenderer | None = None

    try:
        # ── Fire the ack reaction + working status IMMEDIATELY, before the
        # (potentially slow, cold-start) session acquisition. SlackRenderer
        # needs no client, so we construct it up front and reuse it for the
        # driver. This matches native handle_message ordering (react first,
        # spawn session after) so the user sees feedback without waiting for
        # kiro-cli to warm up. on_turn_start is idempotent (the driver's later
        # call no-ops). ──
        decider = (
            SlackApprovalDecider(session_key=session_key)
            if approval_mode == APPROVAL_INTERACTIVE
            else None
        )
        renderer = SlackRenderer(
            slack, channel, reply_ts,
            react_ts=msg_ts,
            reactions_enabled=reactions_enabled,
            show_thinking=show_thinking,
            decider=decider,
            user_id=user_id,
        )
        await renderer.on_turn_start()

        # ── Session acquisition (same as handle_message) ──
        # Durable thread overrides + conv flags were already hydrated at the top
        # of this function (before the hook/privacy/keyword early paths), so we
        # do not re-hydrate here.

        # Resolve the kiro-cli agent. Thread override (set via !agent), then the
        # per-channel override (slack.channels.<id>.agent), then the configured
        # default win; otherwise fall back to the canonical "kirocrew" agent so
        # the session loads kirocrew-core (spawn_run) rather than kiro-cli's bare
        # built-in default. In the Slack path these values are kiro agent names,
        # passed straight through to get_or_create. Mirrors native handle_message.
        _agent = (
            _thread_agents.get(session_key)
            or agent_override
            or _get_default_agent()
            or _DEFAULT_KIROCREW_AGENT
        )
        client, is_new, resumed = await sessions.get_or_create(
            session_key, agent=_agent, channel_id=channel
        )
        _acquired = True
        if is_new:
            await sessions.set_channel(session_key, channel)
        sessions.set_slack_link(session_key, reply_ts, channel)
        # Publish this turn's session identity so managed MCP tools resolve
        # X-Session-Key; one shared writer lives in messaging.identity. (#232)
        await publish_turn_identity(sessions, session_key)

        # ── Build message with context ──
        if context_builder:
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                context_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=channel,
                thread_ts=thread_ts or msg_ts,
                agent=_agent,
                resumed=resumed,
                user_display_name=user_display_name,
            )
        else:
            full_message = text

        # ── PreToolUse security gate (channel-neutral predicate) ──
        # Mirrors native handle_message's hooks.on_tool_call: enforces the
        # sensitive-path keystone (~/.aws, ~/.ssh, security_policy.json, ...),
        # the governance ceiling ∩ profile, and the deny-list. Returns "deny"
        # (hard-block, un-overridable by auto/trust/YOLO), "auto_approve" (hook
        # approves, e.g. reads), or "" (passthrough). The closure reads the
        # event's raw_tool_params so the arg-derived scopes (filesystem.write,
        # network.egress) are evaluated, matching native.
        def _tool_gate(event: Any) -> str:
            if context_builder is None:
                return ""
            result = context_builder.hooks.on_tool_call(
                getattr(event, "title", "") or "",
                session_key=session_key,
                agent=_agent or "",
                tool_kind=getattr(event, "tool_kind", "") or "",
                raw_params=getattr(event, "raw_tool_params", None),
                command=getattr(event, "shell_command", None),
                is_shell=bool(getattr(event, "is_shell", False)),
            )
            if result.action == TOOL_DENY:
                return "deny"
            if result.action == TOOL_AUTO_APPROVE:
                return "auto_approve"
            return ""

        # ── Drive the turn (renderer already started above) ──
        driver = TurnDriver(
            client,
            renderer,
            approval_mode=approval_mode,
            decider=decider,
            # Preserve native handle_message's auto_approve_subagent_spawn hook:
            # auto-approve spawn_run when the context builder's hook is enabled,
            # regardless of the interactive ladder.
            auto_approve_tool=lambda title: _should_auto_approve_spawn(
                context_builder, title
            ),
            # Per-session Trust (set via the Trust button) auto-approves all
            # subsequent tools for THIS session, mirroring native. Checked per
            # permission request so a mid-turn Trust click takes effect for the
            # remaining tools in the same turn.
            auto_approve_session=lambda: is_slack_session_trusted(session_key),
            # PreToolUse deny/auto gate (runs before the ladder in TurnDriver;
            # a DENY is un-overridable by auto/trust/YOLO).
            tool_gate=_tool_gate,
        )
        accumulated = await driver.run(full_message)

        # ── Post-turn bookkeeping ──
        # The turn already succeeded (we have `accumulated`), so record success
        # first and isolate the non-critical bookkeeping below — a failure in
        # context-usage accounting or conversation logging must NOT fall through
        # to the outer except and double-record the turn as a failure.
        sessions.record_success(session_key)
        Stats().inc_message_success()

        try:
            sessions.check_context_usage(session_key, client)
        except Exception:
            logger.warning(
                "transport_dispatch: check_context_usage failed session=%s",
                session_key,
                exc_info=True,
            )

        # ── History consolidation (mirrors native handle_message) ──
        # Non-critical bookkeeping: a raise here must NOT fall through to the
        # outer except and re-record the already-successful turn as a failure.
        try:
            if consolidator is not None:
                consolidator.maybe_consolidate(session_key)
        except Exception:
            logger.warning(
                "transport_dispatch: maybe_consolidate failed session=%s",
                session_key,
                exc_info=True,
            )

        # ── Conversation log ──
        try:
            if conversation_log and not _is_slack_restricted(session_key):
                save_conversation_turn(
                    conversation_log,
                    session_key,
                    text,
                    accumulated,
                    source_thread=session_key,
                    source_user=user_id,
                )
        except Exception:
            logger.warning(
                "transport_dispatch: save_conversation_turn failed session=%s",
                session_key,
                exc_info=True,
            )

        # Success audit is also non-critical bookkeeping: a raise here (disk
        # full, serialization error) must NOT fall through to the outer except
        # and re-record the already-successful turn as a failure.
        try:
            sel().log_api_access(
                caller=user_id,
                operation="transport_dispatch.handle",
                outcome="success",
                source="slack",
                resources=f"session={session_key} elapsed={time.monotonic() - _t0:.1f}s",
            )
        except Exception:
            logger.warning(
                "transport_dispatch: sel log_api_access failed session=%s",
                session_key,
                exc_info=True,
            )

    except Exception:
        logger.exception("transport_dispatch: error handling message")
        Stats().inc_message_failed()
        if client and _acquired:
            await sessions.record_failure(session_key)
        # Post error to Slack so user knows something went wrong
        try:
            await slack.post_message(
                channel, "🔧 Something went wrong (transport path). Please try again.", reply_ts
            )
            await slack.set_thread_status(channel, reply_ts, "")
        except Exception:
            pass
    finally:
        # Guarantee renderer teardown even if TurnDriver.run() raised before
        # on_done: cancels the 30s tool-elapsed timer so it can't survive the
        # turn and keep hitting append_task against a dead stream.
        if renderer is not None:
            await renderer.close()
        if _acquired:
            sessions.release(session_key)
