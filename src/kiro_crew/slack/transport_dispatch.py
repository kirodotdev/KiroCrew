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

import asyncio
import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.dashboard.chat_utils import (
    expire_slack_options,
    mint_options_token,
    remember_slack_options,
)
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import HOOK_REPLY, TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.llm_helpers import save_conversation_turn_off_loop
from kiro_crew.messaging import auto_title
from kiro_crew.messaging.dispatch import build_directive_consumer
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import canonical_key
from kiro_crew.platform import current_context
from kiro_crew.platform.context import redact_via_context
from kiro_crew.sel import sel
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.slack.handler import (
    _get_default_agent,
    _hydrate_conv_flags,
    _hydrate_thread_overrides,
    _is_slack_restricted,
    _maybe_auto_title_slack,
    _should_auto_approve_spawn,
    _thread_agents,
    get_dashboard_state,
    get_orch_cfg,
    is_slack_session_trusted,
    is_thread_temporary,
    maybe_apply_privacy_modifiers,
    maybe_handle_keyword_command,
    maybe_route_linked_thread,
    track_background_task,
)
from kiro_crew.slack.renderer import PARTIAL_TURN_MARKER, SlackApprovalDecider, SlackRenderer
from kiro_crew.stats import Stats

if TYPE_CHECKING:
    from kiro_crew.context import ContextBuilder
    from kiro_crew.cron import CronService
    from kiro_crew.dashboard.state import DashboardState
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


async def _refresh_dashboard_tab(session_key: str) -> None:
    """Push freshly-written transcript lines into an open dashboard tab.

    The Slack transport persists a turn but owns no dashboard slot, so without
    this an open tab learned about a Slack turn only when the background
    reconciler next ran -- up to 30 seconds later, and later still while a turn
    was in flight. Called after each transcript write so the tab tracks the
    conversation as it happens.

    Best-effort by design: this is presentation bookkeeping, and a dashboard
    that is absent (Slack-only gateway) or erroring must never fail the turn.
    """
    try:
        # Kept function-local ON PURPOSE, unlike the handler accessors above:
        # the gateway supports running Slack-only with no dashboard at all
        # (``--slack-only``), and the dashboard is already optional at runtime
        # here (see the None check below). A module-level import would pull the
        # whole dashboard module graph into the Slack inbound path's import
        # time and make it a hard dependency of a mode that does not use it.
        from kiro_crew.dashboard.channel_slots import surface_channel_state

        state = get_dashboard_state()
        if state is None:
            return
        await surface_channel_state(state, getattr(get_orch_cfg(), "dashboard", None))
    except Exception:
        logger.debug(
            "transport_dispatch: dashboard refresh failed session=%s",
            session_key,
            exc_info=True,
        )


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
    gateway: Any | None = None,
    from_trusted_bot: bool = False,
) -> None:
    """Drive a Slack message through the new transport path end-to-end.

    This replaces handle_message when the feature flag is on. It uses
    TurnDriver + SlackRenderer instead of the inline stream loop.

    ``gateway`` is the orchestrator that owns this dispatch (when the caller
    has one): its ``dashboard_state`` attribute supplies the live gateway
    state to the session-directive consumer, so a monitor directive on a
    dashboard-owned thread can resolve the slot instead of failing closed on
    the sessions-backed stand-in.
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

    # Resolve the agent early so ALL persist paths can forward it — including
    # the hook auto-reply below, whose write CREATES the session file when a
    # hook answers the first message in a thread (the metadata header records
    # the agent only on the creating append, so a missed site pins the session
    # to "default" forever). Mirrors native handle_message, which resolves
    # _agent right after hydration for exactly this reason. Re-resolved again
    # at session acquisition below, after the late ownership re-resolution can
    # move session_key.
    _agent = (
        _thread_agents.get(session_key)
        or agent_override
        or _get_default_agent()
        or _DEFAULT_KIROCREW_AGENT
    )

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
                await save_conversation_turn_off_loop(
                    conversation_log,
                    session_key,
                    text,
                    hook_result.text,
                    source_thread=session_key,
                    source_user=user_id,
                    agent=_agent,
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

    # A new turn supersedes whatever question the previous one ended on, so any
    # OPTIONS control still live in this thread stops being answerable.
    #
    # Placed HERE, below every short-circuit above, because only a message that
    # actually starts a turn supersedes anything. ``ping``, ``status``, a
    # modifier-only message, a hook's canned reply and the keyword commands all
    # answer and return WITHOUT running the agent, so the conversation has not
    # moved and the pending question is still the one being waited on --
    # expiring for those spends a live control and leaves valid choices
    # unanswerable, the exact inverse of the stale click this lifecycle exists to
    # prevent. Keeping it at one point below the short-circuits, rather than
    # guarding each of them, means a shortcut added later inherits the right
    # behaviour instead of silently reintroducing this.
    #
    # Resolve the OWNING session, not the key derived above: the control is
    # recorded under whichever session owns the thread, and for a
    # dashboard-linked thread that is its ``dashboard:chat-N`` key. Expiring
    # under the wrong key silently no-ops and leaves the control clickable.
    await expire_slack_options(
        cast("DashboardState | None", get_dashboard_state()),
        sessions.get_session_for_thread(reply_ts) or session_key,
    )

    client: LLMProvider | None = None
    _acquired = False
    renderer: SlackRenderer | None = None
    # Hoisted above the try deliberately: the failure path reads both to decide
    # whether partial assistant output still needs rescuing, and the turn can
    # die anywhere inside the try — including before the points that used to
    # initialize these — which would make the except branch raise NameError
    # while handling the original error.
    _logged_user_turn = False
    _stamped_turn = False

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
            # The restricted-session ceiling on shipping local bytes, the same
            # signal that denies artifact registration: a conversation the user
            # marked temporary or incognito must not upload files into a Slack
            # channel, where they persist for everyone who can read it. Defaulting
            # this True while nothing passed it meant the ceiling did not exist.
            uploads_allowed=not _is_slack_restricted(session_key),
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

        # Re-resolve the kiro-cli agent: the early resolution above serves the
        # pre-acquisition persist paths (hook auto-reply), but the ownership
        # re-resolution just above can move session_key, and a !agent override
        # may have landed since. Thread override (set via !agent), then the
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
        # Authorize the outbound-image root, which only exists once the provider
        # does. Unauthorized, `_upload_root` stays empty and `_uploads_enabled()`
        # is permanently False, so the whole extract-and-upload path is dead while
        # `files_outbound=True` advertises it: an agent that writes
        # `![chart](/tmp/chart.png)` ships the raw path as text. The root is the
        # provider's own resolved cwd, which is what bounds extraction to files
        # the session may read. Mirrors the Discord dispatcher.
        renderer.authorize_upload_root(client.cwd)
        # Expire AGAIN, now that the turn is serialized. The pass above (just
        # before the turn machinery) runs before `get_or_create` waits its turn,
        # so two messages arriving together both clear the control while it is
        # still the OLD one — then the first turn ends by posting a NEW control,
        # which the second turn never expires because its only pass already
        # happened. The user is left with live buttons from a question the
        # conversation has already moved past, which is the exact defect this
        # path exists to prevent. Same staleness reasoning as the FRESH thread
        # read below.
        await expire_slack_options(
            cast("DashboardState | None", get_dashboard_state()),
            sessions.get_session_for_thread(reply_ts) or session_key,
        )
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

        # ── Conversation log: the user's turn, at RECEIPT ──
        # Recorded BEFORE the turn runs rather than alongside the reply
        # afterwards. Writing both rows at the end meant the message did not
        # exist anywhere for the whole duration of the turn, so a dashboard tab
        # had nothing to show until the reply was already finished -- and both
        # rows then carried effectively the same timestamp, losing how long the
        # turn actually took. ``session_key`` is final by this point: the
        # thread-owner re-resolution and session acquisition above are done, so
        # this cannot write under a key the turn later abandons.
        #
        # Slack-restricted (incognito / temporary) sessions are skipped at BOTH
        # write points, so a restricted session still persists nothing. Note
        # this is a skip-at-each-write guarantee, not parity with the old
        # single write: because Slack events dispatch concurrently, an
        # `!incognito` that lands mid-turn used to suppress the whole turn
        # (both rows were still unwritten), and can now only suppress the
        # reply -- the question is already durable.
        if conversation_log and not _is_slack_restricted(session_key):
            try:
                # Off the loop deliberately. ``ConversationLog.append`` takes a
                # cross-process flock, and ON the event loop that primitive
                # makes a single NON-BLOCKING acquire and raises on any
                # concurrent holder -- so calling it inline would both write to
                # disk on the loop and silently drop this row whenever another
                # writer happens to hold the lock.
                #
                # Awaited via to_thread rather than routed through
                # ``append_off_loop`` because that helper is fire-and-forget:
                # the tab refresh below must not run until the row is actually
                # on disk (it reads the file), and a failure here has to be
                # visible so the post-turn fallback can cover the whole turn.
                await asyncio.to_thread(
                    conversation_log.append,
                    session_key,
                    "user",
                    # Scrubbed for this row ONLY: `text` itself still reaches
                    # `build_message` below, and the prompt is never redacted.
                    redact_via_context(text),
                    source_thread=session_key,
                    source_user=user_id,
                    # This is the write that creates the session file, and the
                    # metadata header records the agent only when the creating
                    # append supplies it — omit it and the dashboard forever
                    # shows this session as "default" even though the turn ran
                    # under ``_agent`` (see ConversationLog.append docstring).
                    agent=_agent,
                )
                _logged_user_turn = True
            except Exception:
                logger.warning(
                    "transport_dispatch: user-turn log failed session=%s",
                    session_key,
                    exc_info=True,
                )
            if _logged_user_turn:
                await _refresh_dashboard_tab(session_key)

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
                # Temporary mode reads NO memory, and that is the half the
                # write-side ``_is_slack_restricted`` gates cannot cover:
                # refusing to WRITE still leaves yesterday's memories and
                # lessons in today's prompt, which is exactly what
                # ``NOTICE_TEMPORARY`` tells the user will not happen. The
                # predicate is the temporary-only one on purpose -- incognito
                # deliberately still reads, which is the documented difference
                # between the two modes.
                blocks_reads=is_thread_temporary(session_key),
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
            # regardless of the interactive ladder. The predicate takes the
            # permission EVENT so identity comes from event.tool_name/is_shell,
            # never the model-authored title.
            auto_approve_tool=lambda event: _should_auto_approve_spawn(context_builder, event),
            # Per-session Trust (set via the Trust button) auto-approves all
            # subsequent tools for THIS session, mirroring native. Checked per
            # permission request so a mid-turn Trust click takes effect for the
            # remaining tools in the same turn.
            auto_approve_session=lambda: is_slack_session_trusted(session_key),
            # PreToolUse deny/auto gate (runs before the ladder in TurnDriver;
            # a DENY is un-overridable by auto/trust/YOLO).
            tool_gate=_tool_gate,
            # Session-directive consumer: monitor_start / autonudge_stop / ...
            # return a marker the driver decodes; apply it against THIS turn's
            # session key. ``gateway`` (when the caller passed one) carries the
            # live ``dashboard_state``; without it the consumer falls back to
            # its sessions-backed authorizer stand-in (dashboard-only
            # directives stay refused either way).
            directive_consumer=build_directive_consumer(
                session_key=session_key, sessions=sessions, dispatcher=gateway
            ),
            audit_session_key=session_key,
            audit_agent=_agent or "kirocrew",
            closing_gate=lambda: sessions.begin_turn(session_key),
        )
        # The thread's owner as of the moment the turn starts producing output.
        # A dashboard link landing during the run moves the conversation to a
        # different session, which makes any control this turn posts stale the
        # instant it lands — compared after the run below.
        _pre_run_owner = sessions.get_session_for_thread(reply_ts) or session_key

        # Persisting-and-stamping is handed to the renderer because the token has
        # to be inside the footer it is about to post, and the footer is posted
        # from inside the run below. Only the renderer knows the final text; only
        # we know the conversation and its transcript, so we pass in the operation
        # rather than the state.

        async def _persist_and_stamp(final_text: str) -> str | None:
            nonlocal _stamped_turn
            if not conversation_log or _is_slack_restricted(session_key):
                return None
            _log = conversation_log
            if _logged_user_turn:
                # The user's question was made durable before the run, so only
                # the reply is outstanding. Read the row back inside the same hold
                # that wrote it, so the stamp names this reply and not whatever a
                # later writer appends.
                def _append_and_read() -> str | None:
                    with _log.atomic_appends(session_key):
                        _log.append(
                            session_key,
                            "assistant",
                            final_text,
                            source_thread=session_key,
                            source_user=user_id,
                            agent=_agent,
                        )
                        return _log.last_row_ts(session_key)

                row_ts = await asyncio.to_thread(_append_and_read)
            else:
                row_ts = await save_conversation_turn_off_loop(
                    _log,
                    session_key,
                    text,
                    final_text,
                    source_thread=session_key,
                    source_user=user_id,
                    # This branch runs exactly when the user-turn receipt write
                    # failed, so THIS write is the one that creates the session
                    # file — without the agent the metadata header pins the
                    # session to "default" forever (same reasoning as the
                    # receipt write above).
                    agent=_agent,
                )
            _stamped_turn = True
            # The asker is the conversation that RAN this turn. Resolving the
            # thread's owner here would name whoever took it over mid-turn -- a
            # session that never asked the question.
            return mint_options_token(
                cast("DashboardState | None", get_dashboard_state()),
                session_key,
                row_ts,
            )

        renderer.stamp_options = _persist_and_stamp
        accumulated = await driver.run(full_message)

        # ── Post-turn bookkeeping ──
        # The turn already succeeded (we have `accumulated`), so record success
        # first and isolate the non-critical bookkeeping below — a failure in
        # context-usage accounting or conversation logging must NOT fall through
        # to the outer except and double-record the turn as a failure.
        sessions.record_success(session_key)
        Stats().inc_message_success()

        # Remember this turn's OPTIONS control, if it posted one, so the next
        # turn on this thread can strike it through.
        try:
            # The LIVE owner of the thread, not the key this turn started
            # under. A thread linked to a dashboard mid-turn changes owner,
            # and the next turn's expiry looks the control up under the new
            # key — so recording under the old one files it where nothing
            # will ever find it, leaving the control clickable into a
            # question the conversation has already passed.
            _options_owner = sessions.get_session_for_thread(reply_ts) or session_key
            remember_slack_options(
                cast("DashboardState | None", get_dashboard_state()),
                _options_owner,
                renderer.posted_options,
            )
            # An owner change during the run IS supersession: the thread now
            # belongs to a different session, so the control this turn just
            # posted asks a question the conversation has moved past. Spend it
            # ourselves — narrowed to our own ts, so a control the new owner
            # recorded meanwhile survives.
            #
            # Deliberately NOT also checking ``sessions.is_busy`` here, unlike the
            # native footer path. There the permit is released before the footer
            # goes up, so a busy session means somebody ELSE. Here the turn still
            # holds its semaphore at this point (``record_success`` resets the
            # failure counter, it does not release), so ``is_busy`` would report
            # OUR OWN turn and strike every fresh control through the moment it
            # was posted — a worse defect than the one this guards.
            _posted = renderer.posted_options
            if _posted is not None and _options_owner != _pre_run_owner:
                await expire_slack_options(
                    cast("DashboardState | None", get_dashboard_state()),
                    _options_owner,
                    ts=_posted.ts,
                )
        except Exception:
            logger.debug(
                "transport_dispatch: failed to record OPTIONS control session=%s",
                session_key,
                exc_info=True,
            )

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

        # ── Conversation log: the reply ──
        # The user's row already landed at receipt, so only the reply is added
        # here. If that receipt write failed we fall back to the combined write
        # so a turn is never persisted reply-only.
        try:
            if conversation_log and not _is_slack_restricted(session_key):
                # Skipped only when the stamp above already wrote this turn (an
                # options turn). Every other completion -- and any turn whose stamp
                # failed -- still persists here, so no path ends up writing the
                # turn twice and none ends up not writing it at all.
                if _stamped_turn:
                    pass
                elif _logged_user_turn:
                    if accumulated:
                        # Same off-loop reasoning as the receipt write above.
                        await asyncio.to_thread(
                            conversation_log.append,
                            session_key,
                            "assistant",
                            accumulated,
                            source_thread=session_key,
                            source_user=user_id,
                            agent=_agent,
                        )
                else:
                    await save_conversation_turn_off_loop(
                        conversation_log,
                        session_key,
                        text,
                        accumulated,
                        source_thread=session_key,
                        source_user=user_id,
                        agent=_agent,
                    )
                await _refresh_dashboard_tab(session_key)
        except Exception:
            logger.warning(
                "transport_dispatch: save_conversation_turn failed session=%s",
                session_key,
                exc_info=True,
            )

        # ── Auto-title the conversation (fire-and-forget) ──
        # The native loop has always titled a thread after its first successful
        # turn, and this path never did — while ``messaging.use_transport``
        # defaults True, so on a default install NO Slack session got a generated
        # title and every surface fell back to a deterministic truncation. Same
        # claim tracker as native (``auto_title.try_claim`` is check-and-mark in
        # one step), so a session cannot be titled twice when both paths are live.
        #
        # Requires ``accumulated``: a turn that produced no text has nothing to
        # name, and titling it would spend a background turn to be told SKIP.
        # Skipped for a restricted session, which persists nothing to title.
        # Isolated like every other bookkeeping step here, so a failure to even
        # SPAWN the task never re-records this successful turn as a failure.
        try:
            if (
                accumulated
                and not _is_slack_restricted(session_key)
                and auto_title.try_claim(session_key)
            ):
                track_background_task(
                    asyncio.create_task(
                        _maybe_auto_title_slack(
                            slack,
                            sessions,
                            channel,
                            session_key,
                            conversation_log,
                            text,
                            accumulated,
                        )
                    )
                )
        except Exception:
            logger.warning(
                "transport_dispatch: auto-title dispatch failed session=%s",
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

    except SessionClosingError:
        # Shutdown began between the claim and the dispatch, so no turn opened.
        # Mirrors the native handler's own gate: clear the thread status and
        # return quietly. Deliberately NOT the generic branch below -- a restart
        # is not a session fault (record_failure counts toward tripping the
        # circuit breaker on a session that never misbehaved), is not a failed
        # message, and does not warrant an error posted into the thread.
        logger.info("Aborting Slack dispatch for %s — gateway is shutting down", session_key)
        with contextlib.suppress(Exception):
            await slack.set_thread_status(channel, reply_ts, "")
    except Exception:
        logger.exception("transport_dispatch: error handling message")
        Stats().inc_message_failed()
        if client and _acquired:
            await sessions.record_failure(session_key)
        # ── Rescue partial progress ──
        # A transient backend fault (the "died before streaming started" class,
        # a dropped stream) leaves the user row on disk and everything the model
        # already produced in memory only. Nothing persisted it, so each retry
        # re-read a transcript that ended at the question and started over: a
        # 28-minute outage on 2026-09-02 burned five identical attempts that each
        # re-derived the same ticket ids before dying again.
        #
        # Persisting the partial reply makes the next attempt resume — it reads
        # what was already established instead of rediscovering it. Guarded so
        # this can never make the failure worse:
        #   * ``_stamped_turn`` — the reply already landed via the OPTIONS stamp,
        #     so writing again would duplicate the turn.
        #   * ``renderer.turn_finalized`` — the turn already ENDED normally. A
        #     fault raised after the stream finished (a footer post that fails,
        #     say) unwinds through this same branch, and stamping that complete
        #     reply as cut off would tell the next turn to resume finished work.
        #     Safe to read here: ``close()`` also sets the flag, but from the
        #     ``finally`` below, which runs after this decision is made.
        #   * ``_logged_user_turn`` — the rescue runs ONLY when the user row is
        #     confirmed on disk. When the receipt append raised, whether the row
        #     landed is unknowable from here: ``ConversationLog.append`` writes
        #     the row and only then invalidates caches and calls
        #     ``_maybe_rotate``, whose own guard covers just the ``stat`` — so a
        #     rotation fault on an oversized transcript raises with the row
        #     already durable, while a lock timeout raises with nothing written.
        #     Writing both rows there would duplicate the question; writing the
        #     assistant row alone would orphan the answer. Neither is honest, so
        #     the rescue no-ops and the retry starts from the question, exactly
        #     as it did before this change. Same rule the delivery ledger applies
        #     to an unacknowledged send: when an outcome is unconfirmable, record
        #     nothing.
        #   * ``_is_slack_restricted`` — incognito/temporary sessions persist
        #     nothing, same as both success-path writes.
        #   * off-loop ``to_thread``, because ``append`` takes a cross-process
        #     flock and ON the loop it single-shots and drops the row.
        #   * its own try/except: a disk failure here must not replace the real
        #     exception in the log with a bookkeeping one.
        # Persist only what Slack CONFIRMED it showed. ``SlackRenderer`` batches on
        # an edit throttle, so the text the model has produced runs routinely ahead
        # of the text the user has seen; a rescue that persisted the former would
        # record unseen output as established fact, and the retry would build on
        # something that was never on screen. ``delivered_text`` is the renderer's
        # delivery ledger — advanced only where Slack acknowledged the append — so
        # it is the honest source here.
        #
        # This is the only rescue in the codebase, on purpose — and the reason is
        # confirmable delivery, not an absence of mid-turn sending. Renderers on
        # the shared ``messaging/dispatch.drive_turn`` path DO emit during a turn
        # (``telegram`` live-edits per chunk, ``wecom`` pushes stream frames,
        # ``webex`` rides the buffer tail on a status frame), but none of them can
        # say afterwards which bytes the user actually retained: those frames are
        # throttled, replaced wholesale, or truncated. Slack is the one renderer
        # that keeps a per-append record of what the API acknowledged, so it is
        # the one place a rescue can persist shown text rather than produced text.
        _shown = renderer.delivered_text if renderer is not None else ""
        # ``strip`` decides EMPTINESS only. What gets persisted is ``_shown``
        # verbatim: leading indentation is content in a transcript — a fenced block
        # or a nested list loses its structure if the margin is trimmed — and the
        # success-path write does not trim it either.
        if (
            _shown.strip()
            and _logged_user_turn
            and not _stamped_turn
            and renderer is not None
            and not renderer.turn_finalized
            and conversation_log
            and not _is_slack_restricted(session_key)
        ):
            try:
                await asyncio.to_thread(
                    conversation_log.append,
                    session_key,
                    "assistant",
                    _shown + PARTIAL_TURN_MARKER,
                    source_thread=session_key,
                    source_user=user_id,
                )
                logger.info(
                    "transport_dispatch: rescued %d chars of partial reply session=%s",
                    len(_shown),
                    session_key,
                )
            except Exception:
                logger.warning(
                    "transport_dispatch: partial-reply rescue failed session=%s",
                    session_key,
                    exc_info=True,
                )
        # Post error to Slack so user knows something went wrong. The error
        # MESSAGE is suppressed for trusted-bot messages: in a mutual-mesh
        # setup (A trusts B, B trusts A) an error reply is itself a
        # bot-authored event the peer admits, so replying would open an
        # unbounded error-reply ping-pong. Mirrors the native path's guard in
        # handler.py (from_trusted_bot and _had_error). The thread STATUS is
        # cleared unconditionally — skipping it would leave a stale "working"
        # status pinned to the thread forever.
        if from_trusted_bot:
            logger.info(
                "Suppressing transport error reply to trusted bot message (echo-loop guard)"
            )
        else:
            try:
                await slack.post_message(
                    channel, "🔧 Something went wrong (transport path). Please try again.", reply_ts
                )
            except Exception:
                pass
        try:
            await slack.set_thread_status(channel, reply_ts, "")
        except Exception:
            pass
    finally:
        # Guarantee renderer teardown even if TurnDriver.run() raised before
        # on_done: cancels the 30s tool-elapsed timer so it can't survive the
        # turn and keep hitting append_task against a dead stream.
        #
        # Teardown is best-effort and must NEVER prevent the release below: the
        # semaphore is keyed by SESSION, so a close() that raises here would
        # wedge every later message in that conversation (and its queue drain)
        # until the gateway restarts, not merely lose this turn. Same guard as
        # Discord's dispatcher and the shared pipeline.
        if renderer is not None:
            try:
                await renderer.close()
            except Exception:
                logger.warning(
                    "Slack: renderer.close failed session=%s",
                    session_key,
                    exc_info=True,
                )
        if _acquired:
            sessions.release(session_key)
