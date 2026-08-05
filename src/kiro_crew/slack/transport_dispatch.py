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
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
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

    # ── Resolve the thread to its OWNING session (mirrors native handle_message) ──
    # canonical_key above is purely syntactic: it namespaces the bare thread ts
    # into ``slack:<ts>``. That is only the right session when this thread was
    # BORN in Slack. A thread created by the dashboard's send-to-Slack action
    # belongs to that dashboard session, and the binding lives in the thread
    # index (keyed by the bare thread_ts, not the namespaced key). Without this
    # lookup a reply in such a thread mints a brand-new ``slack:<ts>`` session
    # -- forking one conversation into two, with the reply landing in a session
    # that has none of the dashboard context. reply_ts stays untouched: it is
    # still the Slack timestamp we post and react to.
    linked_session_key: str | None = None

    def _resolve_thread_owner(stage: str) -> None:
        """Re-read which session owns this thread, and route this turn there.

        Ownership is RE-READ at each decision point rather than cached, because
        this function awaits many times before the turn is acquired: inbound
        governance, the hook path, the privacy modifiers and session acquisition
        all yield. A dashboard send-to-Slack landing in any of those windows
        claims the thread, and every decision taken from a stale reading
        afterwards is wrong -- the reply runs in a session with none of the
        dashboard context, and an unguarded ``set_slack_link`` overwrites the
        newer binding so all later replies misroute too. Cheap to repeat: it is
        an in-memory dict read off the thread index.
        """
        nonlocal session_key, linked_session_key
        owner = sessions.get_session_for_thread(reply_ts)
        if not owner:
            return
        if owner != session_key:
            logger.info(
                "🔗 Slack thread %s owned by %s (at %s) — routing there",
                session_key,
                owner,
                stage,
            )
            session_key = owner
            # Durable privacy state is keyed BY SESSION, and the hydration at
            # entry ran for the previous key. Re-hydrate for the new owner in
            # the same breath as the reroute -- otherwise _is_slack_restricted()
            # consults an unpopulated flag map, and an incognito or temporary
            # session's turn gets persisted to disk. Keeping this inside the
            # helper makes it structurally impossible to reroute without it.
            # Both helpers guard repeated I/O per session, so this is cheap.
            _hydrate_thread_overrides(session_key, conversation_log)
            _hydrate_conv_flags(sessions, session_key)
        linked_session_key = owner

    _resolve_thread_owner("inbound")

    # Inbound channels-governance gate (off-loop), same as native handle_message:
    # a ``channels`` policy that denies ``slack`` drops the message before any
    # processing. Default build (no policy) permits — behavior unchanged.
    if not await channel_inbound_permitted("slack"):
        logger.info("slack inbound (transport) dropped: denied by channels governance policy")
        return

    # ── Re-hydrate durable thread state FIRST (mirrors native ordering) ──
    # The per-thread agent/project overrides AND the durable incognito/temporary
    # privacy flags must be restored before ANY early path consults
    # _is_slack_restricted — the hook auto-reply, the !temporary/!incognito
    # modifiers, and the keyword commands all gate conversation-log writes on it.
    # After a gateway restart the in-memory flag maps (_thread_incognito /
    # _thread_temporary) start empty; hydrating late (at session acquisition)
    # would let a hook/spawn/cron turn on a previously-incognito thread
    # get logged before the durable flag was reloaded. Native hydrates right
    # after session_key, so we match it. Idempotent:
    # _hydrate_thread_overrides guards repeated I/O per session.
    _hydrate_thread_overrides(session_key, conversation_log)
    _hydrate_conv_flags(sessions, session_key)

    # Entry marker: lets operators confirm the NEW transport path handled a
    # message (grep gateway.log for "transport_dispatch: handling"). Fires for
    # every message including the status/ping shortcuts below.
    logger.info("transport_dispatch: handling message session=%s user=%s", session_key, user_id)

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
    # Re-read ownership BEFORE the modifiers run: they call set_slack_link
    # unconditionally (handler.py), so acting on a key that went stale while
    # governance and the hook path awaited would overwrite a dashboard binding
    # created in that window and misroute every later reply in this thread.
    _resolve_thread_owner("pre-privacy")
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
            slack,
            channel,
            reply_ts,
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

        # ── Final ownership check, as late as possible before we commit ──
        # The resolution at the top of this function is the one hydration and the
        # !temporary / !incognito handlers needed, but it is many awaits old by
        # now (inbound governance, the hook path, renderer start). Re-resolve
        # here so the turn is ACQUIRED under the thread's current owner instead
        # of a stale one -- otherwise a Link-to-Dashboard click landing in that
        # window leaves this reply running in a contextless Slack session.
        # Deliberately before the _agent resolution below, which is keyed by
        # session_key. A change that lands DURING get_or_create is not handled
        # here: the turn stays in whoever owned the thread at acquisition, and
        # the self-link guard below keeps the index uncorrupted either way.
        _resolve_thread_owner("pre-acquisition")
        if decider is not None:
            # The decider was constructed with the pre-reroute key, and that key
            # is what maps a human's Trust click back to a session. Leaving it
            # stale would apply Trust to the wrong session. Same object the
            # renderer and driver hold, so re-pointing it here is sufficient.
            decider.session_key = session_key

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
        if not linked_session_key and not sessions.get_session_for_thread(reply_ts):
            # Two conditions, deliberately. The first is the routing decision
            # made at the top of this function. The second is a FRESH read,
            # because that decision is many awaits old by now -- inbound
            # governance, the hook path and session acquisition all yield -- and
            # a dashboard send-to-Slack landing in that window would claim this
            # thread after we looked. Self-linking on the stale value would
            # overwrite that newer binding and send every later reply to the
            # wrong session. Only claim a thread that is STILL unclaimed; the
            # turn itself continues on the session we already acquired.
            #
            # reply_ts (not session_key) is the true Slack timestamp -- storing
            # the namespaced key as slack_thread_ts would corrupt reply routing.
            sessions.set_slack_link(session_key, reply_ts, channel)
        # Publish this turn's session identity so managed MCP tools resolve
        # X-Session-Key; one shared writer lives in messaging.identity.
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
                runtime_source="slack",
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
            auto_approve_tool=lambda title: _should_auto_approve_spawn(context_builder, title),
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
