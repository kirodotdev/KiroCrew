"""Full new-path dispatch: DiscordTransport -> TurnDriver -> DiscordRenderer.

``DiscordTransport.receive()`` authorizes + normalizes an inbound message and
hands the ``InboundMessage`` to :meth:`DiscordDispatcher.handle_message`,
which mirrors the Telegram transport dispatch:

    command intercept (!new, !compact, !help, …)
    -> construct DiscordRenderer + on_turn_start (typing indicator)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft-threshold notice)  # each guarded
    -> renderer.close() + session release   # in finally

``on_interaction`` resolves interactive tool approvals (``a:<rid>:<1|0>`` ->
``DiscordApprovalDecider.resolve_global``) and re-injects ``[OPTIONS:]``
choices (``opt:<i>``) as fresh turns.

Dependency direction is ``discord -> messaging`` (allowed). The security
``tool_gate`` and spawn auto-approve are wired inline off ``ctx_builder.hooks``
(channel-neutral) so this module never imports ``kiro_crew.slack``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.discord.attachments import (
    append_attachment_context,
    process_discord_attachments,
)
from kiro_crew.discord.commands import (
    ConversationState,
    parse_command,
    parse_command_argument,
    parse_mid_turn_override,
)
from kiro_crew.discord.renderer import DiscordApprovalDecider, DiscordRenderer
from kiro_crew.discord.session_resume import (
    DiscordSessionResume,
    ResumeReleaseError,
    RoutingDecision,
)
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.messaging.attachments import IngestLimits
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.dispatch import delivery_is_muted
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import (
    UNBIND_REASON_ORIGIN_REBIND,
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    legacy_dashboard_mirror_key,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.messaging.renderer import Renderer, SilentRenderer
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session_map import ConversationOwnershipConflict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.discord.client import DiscordClient, DiscordInteraction
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager

from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI
from kiro_crew.messaging.queue_receipt import (
    ReceiptQueue,
    ReceiptSurface,
)

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Discord sessions load kirocrew-core
# (spawn_run etc.) — mirrors the Slack/Telegram paths.
_DEFAULT_KIROCREW_AGENT = "kirocrew"

#: Values that mean "let the backend pick" rather than naming an agent. The
#: dashboard writes ``"default"`` into most session metadata, and ``"auto"`` is
#: its sibling sentinel (see ``dashboard/handlers/agents.py``). Neither is a
#: kiro-cli mode, so neither may reach ACP ``session/set_mode``.
_AGENT_SENTINELS = frozenset({"default", "auto"})

# Upper bound on how many queued messages collapse into a single combined turn.
_MAX_COLLAPSE = 50
# Keep queue collapse within the shared ingestion layer's per-turn file cap.
_MAX_COLLAPSED_ATTACHMENTS = IngestLimits().max_attachments

#: Commands that still run while this conversation owes the user a detach notice.
#: Everything else targets a session or is a plain turn and must be refused until
#: the user has been told — including a bare message, whose ``cmd`` is ``None``.
_DETACH_EXEMPT_COMMANDS = frozenset({"new", "unlink", "sessions", "help"})
_RELEASE_FAILURE = (
    "⚠️ Couldn't save the session release, so the command was NOT completed. "
    "Fix the gateway's storage problem, then retry."
)
_HELP_TEXT = """\
🦞 **Kiro Crew — Discord**

Commands:
`!new` — Start a fresh conversation
`!compact` — Compress context (when it gets long)
`!sessions [query]` — Continue a recent or matching dashboard session here (owner only)
`!link` — Resume mirroring dashboard replies here (on by default)
`!unlink` — Stop mirroring dashboard replies here
`!stop` — Stop the current reply and clear the queue
`!help` — Show this message

While a reply is running, prefix a message to control it:
`!queue <msg>` — answer it after the current turn
`!steer <msg>` — fold it into the running turn now

Just send a message to chat. Replies stream in real-time.
"""


class DiscordDispatcher:
    """Coordinates Discord turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-user conversation state
    (generation counter + soft-threshold flag). ``handle_message`` is wired as
    the transport's dispatch callback; ``on_interaction`` is wired as the
    client's button handler. ``client`` is set by the gateway after
    construction.
    """

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        ctx_builder: "ContextBuilder",
        cfg: "KiroCrewConfig",
        allowed_user_ids: set[str],
        allowed_thread_ids: set[str] | None = None,
        agent: str | None = None,
        conv_log: "ConversationLog | None" = None,
        approval_mode: str = APPROVAL_INTERACTIVE,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self._allowed = set(allowed_user_ids or ())
        self._allowed_threads = set(allowed_thread_ids or ())
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        self.client: "DiscordClient | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)
        # The mid-turn queue receipt + the lock serializing it against the
        # end-of-turn drain, shared with Telegram via messaging/queue_receipt.py.
        self._queue = ReceiptQueue()
        # session_key -> the running turn's renderer (for steer chips).
        self._active_renderers: dict[str, DiscordRenderer] = {}
        # channel_id -> (lock, in-flight deciders); dropped when the last one leaves.
        self._routing_locks: dict[str, tuple[asyncio.Lock, list[int]]] = {}
        # A message in governance predates a refusal but is not a decider yet.
        self._routing_checks: dict[str, int] = {}
        self._session_resume = DiscordSessionResume(
            sessions,
            conv_log,
            self._allowed,
        )
        # Kept as a direct alias for diagnostics/tests; the controller owns it.
        self._session_pickers = self._session_resume.pickers

    def register_allowed_thread(self, thread_id: str) -> None:
        """Authorize interactions in a thread created by the inbound transport."""
        self._allowed_threads.add(thread_id)

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(
        self,
        msg: InboundMessage,
        *,
        drain: bool = True,
        interpret_commands: bool = True,
    ) -> None:
        """Drive one authorized inbound message through TurnDriver end-to-end."""
        assert self.client is not None, "DiscordDispatcher.client must be set"
        channel_id = msg.conversation_id
        self._routing_checks[channel_id] = self._routing_checks.get(channel_id, 0) + 1
        # Inbound channels-governance gate (off-loop). The startup gate only stops
        # a transport from CONNECTING; a host-profile deny added after it connected
        # would otherwise keep dispatching inbound messages until restart. Recheck
        # per message so a runtime deny takes effect immediately — silently drop
        # (no reply) on deny, matching how an unauthorized user is ignored.
        try:
            permitted = await channel_inbound_permitted("discord")
        finally:
            remaining = self._routing_checks[channel_id] - 1
            if remaining:
                self._routing_checks[channel_id] = remaining
            else:
                self._routing_checks.pop(channel_id)
        if not permitted:
            logger.info("discord inbound dropped: denied by channels governance policy")
            return
        user_id = msg.user_id
        thread_id = msg.thread_id or ""
        scope_id = self._scope_id(user_id, thread_id)
        text = msg.text

        # Attachments make this a content-bearing turn, not a control command.
        # Otherwise a caption such as ``!help`` would intercept before ingestion
        # and silently discard the attached file — the exact class of bug this
        # path is meant to eliminate.
        interpret_as_command = interpret_commands and not msg.attachments

        # Per-message mid-turn override (!queue/!steer) — see the Telegram
        # dispatcher for the full precedence rationale.
        override_mode = None
        if interpret_as_command and parse_command(text) is None:
            override_mode, text = parse_mid_turn_override(text)

        # ── Command intercept (no LLM session needed) ──
        cmd = (
            parse_command(text)
            if interpret_as_command and override_mode is None
            else None
        )
        # `!compact` and `!stop` act on the resolved session, so after a binding was
        # destroyed they would compact or cancel the NATIVE DM session while the user
        # believes they drive the resumed one; deciding here makes that structural.
        route = RoutingDecision()
        if interpret_commands and cmd not in _DETACH_EXEMPT_COMMANDS:
            async with self._routing_turn(channel_id) as queued:
                route = await self._session_resume.route(channel_id)
                if route.refusal is not None:
                    # Settle only once the refusal landed AND nobody who predates it
                    # is still in governance or queued: otherwise that message could
                    # route into a transcript the user never chose.
                    landed = await self.client.send_message(channel_id, route.refusal)
                    if (landed and len(queued) == 1 and not self._routing_checks.get(channel_id)):
                        await self._session_resume.settle(channel_id, route)
                    return
        if cmd == "new":
            try:
                left_resumed = await self._session_resume.leave_resumed_session(channel_id)
            except ResumeReleaseError:
                await self.client.send_message(channel_id, _RELEASE_FAILURE)
                return
            self._conv.bump_gen(scope_id)
            message = "✅ New conversation started."
            if left_resumed is not None:
                message = "✅ New conversation started — left the resumed session."
            await self.client.send_message(channel_id, message)
            return
        if cmd == "compact":
            self._conv.clear_awaiting(scope_id)
            await self._handle_compact(user_id, channel_id, thread_id, route.resumed_key)
            return
        if cmd == "sessions":
            # DM-ONLY. The owner gate answers WHO may resume, not WHERE the
            # result may be shown: in an allow-listed guild thread the picker
            # would post dashboard session TITLES and the bind would replay five
            # transcript messages, making private history readable by every
            # member of that thread. Resume is inherently a private-surface
            # operation, so refuse outside a DM rather than redacting harder.
            if thread_id:
                await self.client.send_message(
                    channel_id,
                    "🔒 `!sessions` works only in a direct message — it lists and "
                    "replays private dashboard conversations, so it will not post "
                    "them into a shared thread. DM me instead.",
                )
                return
            await self._session_resume.show_picker(
                self.client,
                user_id,
                channel_id,
                query=parse_command_argument(text),
            )
            return
        if cmd == "link":
            await self._handle_link(user_id, channel_id, thread_id, route.resumed_key)
            return
        if cmd == "unlink":
            await self._handle_unlink(user_id, channel_id, thread_id)
            return
        if cmd == "help":
            await self.client.send_message(channel_id, _HELP_TEXT)
            return
        if cmd == "stop":
            await self._handle_stop(user_id, channel_id, thread_id, route.resumed_key)
            return

        # ── Mid-turn concurrency: check the CURRENT-generation key BEFORE any
        # idle/daily rotation (see the Telegram dispatcher's rationale). ──
        # ``resumed_key`` comes from the decision above and is NOT re-resolved: a
        # second resolver call let an unlink landing mid-decision route silently.
        resumed_key = route.resumed_key
        session_key = resumed_key or self._session_key(user_id, thread_id)
        if self.sessions.is_busy(session_key):
            if resumed_key is not None:
                # Do NOT queue or steer into a resumed session's running turn.
                # ``_drain_queue`` is only ever called from the tail of a
                # DISCORD-driven turn; the dashboard turn loop has no knowledge
                # of this queue, so a message enqueued while the dashboard is
                # driving would sit until some later Discord turn and then
                # execute out of order. Refusing is honest and recoverable.
                await self.client.send_message(
                    channel_id,
                    "⏳ That session is busy with a turn started elsewhere. "
                    "Send it again once it finishes, or `!unlink` to go back to "
                    "your own conversation.",
                )
                return
            await self._handle_busy(session_key, msg, text, override_mode)
            return

        self._conv.maybe_rotate(
            scope_id,
            time.time(),
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
        )
        session_key = resumed_key or self._session_key(user_id, thread_id)
        chan_id = f"discord:{channel_id}" if thread_id else f"discord:{user_id}"
        agent = self._resolve_agent()
        if resumed_key is not None:
            # A resumed session must run as ITSELF, not as Discord's agent. On a
            # cold start get_or_create applies the agent we pass, so handing it
            # the Discord default would load the dashboard conversation's
            # transcript and then run it under a different system prompt — and a
            # different allowedTools set, which is a permission-boundary change,
            # not just a tone change. get_metadata touches the filesystem, so it
            # goes off-loop. Fall back to the Discord agent only when the
            # conversation recorded none.
            persisted = await asyncio.to_thread(self._persisted_agent, resumed_key)
            if persisted:
                agent = persisted

        decider = (
            DiscordApprovalDecider(session_key=session_key)
            if self.approval_mode == APPROVAL_INTERACTIVE
            else None
        )
        renderer = DiscordRenderer(
            self.client, channel_id, DISCORD_CAPABILITIES, session_key=session_key
        )
        # Discord runs its OWN copy of the turn loop instead of going through
        # ``messaging.dispatch.drive_turn``, so the disconnect gate there does not
        # reach it — without this the dashboard control changed nothing here but
        # its own label. The turn still runs and the inbound message still lands in
        # the session: the binding is retained by design, and the dashboard is
        # where that user is now working. Only the writes back are dropped.
        muted = delivery_is_muted(self.sessions, session_key, DiscordRenderer.channel_type)
        # Handed to the driver AND closed in the finally, rather than reassigning
        # ``renderer``: the concrete renderer's ``close`` is not inert — it posts an
        # error placeholder when the turn produced no output, which a muted turn by
        # definition did, so closing the real one leaked "⚠️ Error" into the
        # conversation the user had just disconnected.
        out_renderer: Renderer = (
            SilentRenderer(DISCORD_CAPABILITIES, DiscordRenderer.channel_type)
            if muted
            else renderer
        )
        if not muted:
            # Published for mid-turn steer chips. Deliberately NOT published when
            # muted: the steer path calls the channel-specific ``note_steer`` and
            # already skips cleanly when there is no entry, so leaving it out both
            # silences the chip in a disconnected conversation and keeps that
            # channel-local API off the shared substitute.
            self._active_renderers[session_key] = renderer
        attachment_temp_paths: list[str] = []

        # Everything acquire-dependent runs INSIDE the try so the finally
        # always finalizes the renderer; release() is gated on _acquired.
        # Mirrors telegram/transport_dispatch.py.
        _acquired = False
        try:
            # Typing indicator BEFORE the cold start. get_or_create can spend
            # seconds spawning/handshaking an ACP session, and until this runs
            # Discord shows nothing at all, so the user sees dead air and assumes
            # the bot missed the message. This is the ordering the shared
            # skeleton documents ("typing indicator before cold start" in
            # messaging/dispatch.py) and the one telegram/transport_dispatch.py
            # still uses. Safe here: on_turn_start only spawns a background
            # refresh task, is idempotent (the driver calls it again later), and
            # the enclosing finally always finalizes the renderer, so an early
            # return below cannot leak a typing loop.
            # Skipped when muted: a disconnected conversation must not
            # even show a typing indicator.
            if not muted:
                await renderer.on_turn_start()
            # Acquire before attachment I/O. A large download yields repeatedly;
            # leaving the session idle in that window lets a later message run
            # first and persist the conversation in reverse order.
            provider, is_new, resumed = await self.sessions.get_or_create(
                session_key, agent=agent, channel_id=chan_id
            )
            _acquired = True
            if msg.attachments:
                attachment_result = await process_discord_attachments(
                    self.client, msg.attachments
                )
                attachment_temp_paths = list(attachment_result.temp_paths)
                text = append_attachment_context(text, attachment_result)
            if not text:
                return
            # New-session bookkeeping belongs to THIS conversation's own session
            # only. A resumed dashboard session is pre-existing by definition, and
            # `get_or_create` returns is_new whenever its ACP session is merely
            # COLD — which is the normal case, since the picker lists *history*,
            # not live sessions. Treating it as new caused two routine data
            # losses on the very first resumed message:
            #   • set_channel writes through to the legacy slack_channel_id field,
            #     stamping `discord:<id>` onto the dashboard session. That survives
            #     `!unlink` (which clears only `mirror`), so get_mirror_link then
            #     synthesizes a bogus Slack link and every later `!sessions` pick
            #     of that session is refused with "already active on Slack".
            #   • _persist_turn(is_new=True) calls set_title, replacing the
            #     dashboard conversation's title with the first 40 characters of
            #     the Discord message.
            is_new_own_session = is_new and resumed_key is None
            if is_new_own_session:
                await self.sessions.set_channel(session_key, chan_id)
            if resumed_key is None:
                # Record the conversation's REAL send target so unattended
                # output about the session — the auto-compact notice — can reach
                # the user. `chan_id` above is the legacy namespaced bucket and
                # carries the user id for a DM, which is not a postable channel.
                # Skipped for a resumed dashboard session: its own surface owns
                # the notice, and stamping it here would bind a dashboard entry
                # to Discord.
                # An in-memory dict assignment on the session manager, not a
                # persisted field: the target is only needed while the session
                # is live, so no disk I/O and no cross-thread state land on this
                # turn path.
                self.sessions.set_origin_link(
                    session_key, ChannelLink("discord", channel_id=channel_id)
                )
                # Bind this conversation as the session's outbound mirror so a
                # turn the user later takes from the dashboard is delivered back
                # here. Slack gets this from its own per-turn thread binding;
                # Discord had it only behind an explicit `!link`, so the chat sat
                # there looking dead while the conversation continued elsewhere.
                # Inside the `resumed_key is None` branch with set_origin_link,
                # for the same reason: a resumed session's own surface owns its
                # output and `!link` refuses there too, so the automatic path must
                # not do what the explicit one declines. (It would also decline on
                # its own, having found the resume binding for this very channel —
                # the placement is what keeps that from being load-bearing.)
                self._bind_origin_mirror(session_key, channel_id)
            # Publish this turn's session identity so managed MCP tools resolve
            # X-Session-Key; one shared writer lives in messaging.identity.
            await publish_turn_identity(self.sessions, session_key)
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                self.ctx_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=chan_id,
                agent=agent,
                resumed=resumed,
                runtime_source="discord",
            )

            # PreToolUse security gate (channel-neutral, off ctx_builder.hooks).
            def _tool_gate(event: Any) -> str:
                result = self.ctx_builder.hooks.on_tool_call(
                    getattr(event, "title", "") or "",
                    session_key=session_key,
                    agent=agent,
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

            driver = TurnDriver(
                provider,
                out_renderer,
                approval_mode=self.approval_mode,
                decider=decider,
                auto_approve_tool=lambda title: bool(
                    self.ctx_builder
                    and self.ctx_builder.hooks
                    and self.ctx_builder.hooks.auto_approve_subagent_spawn
                    and title == "spawn_run"
                ),
                tool_gate=_tool_gate,
            )
            accumulated = await driver.run(full_message)

            # ── Post-turn bookkeeping (each guarded — see Telegram). ──
            self.sessions.record_success(session_key)
            try:
                # Loop-side: put the turn in the live dashboard window FIRST so
                # the dashboard's own save serializes it in chronological
                # position instead of appending it to the foreign tail.
                mirrored = self._mirror_turn_to_live_slot(session_key, text, accumulated)
                await asyncio.to_thread(
                    self._persist_turn,
                    session_key,
                    text,
                    accumulated,
                    is_new_own_session,
                    mirrored,
                    agent=agent,
                )
            except Exception:
                logger.warning(
                    "Discord: persist_turn failed session=%s",
                    session_key,
                    exc_info=True,
                )
            if is_new_own_session:
                try:
                    await self._surface_own_session()
                except Exception:
                    logger.warning(
                        "Discord: immediate dashboard session surface failed session=%s",
                        session_key,
                        exc_info=True,
                    )
            try:
                await self._maybe_notice(channel_id, scope_id, session_key, provider)
            except Exception:
                logger.warning(
                    "Discord: maybe_notice failed session=%s",
                    session_key,
                    exc_info=True,
                )
            try:
                sel().log_api_access(
                    caller=f"discord:{user_id}",
                    operation="transport_dispatch.handle",
                    outcome="success",
                    source="discord",
                    resources=f"session={session_key}",
                )
            except Exception:
                logger.debug("Discord: success audit failed", exc_info=True)
        except Exception:
            logger.exception("Discord transport_dispatch: error handling message")
            if _acquired:
                await self.sessions.record_failure(session_key)
        finally:
            # Renderer finalization is best-effort and must NEVER prevent the
            # session release below — a rendering failure (e.g. Discord/proxy
            # returning a malformed body) that also failed finalization would
            # otherwise leave the session permanently busy, blocking every
            # subsequent Discord message and the queue drain.
            try:
                await out_renderer.close()
            except Exception:
                logger.warning(
                    "Discord: renderer.close failed session=%s",
                    session_key,
                    exc_info=True,
                )
            self._active_renderers.pop(session_key, None)
            if _acquired:
                self.sessions.release(session_key)
            await asyncio.to_thread(
                cleanup_attachments, attachment_temp_paths
            )

        # Drain anything queued during the turn (queue_mode == "queue").
        if drain:
            await self._drain_queue(session_key, user_id, channel_id, thread_id)

    async def _handle_busy(
        self,
        session_key: str,
        msg: InboundMessage,
        text: str,
        override_mode: str | None,
    ) -> None:
        """A message arrived mid-turn: steer the running turn or queue it."""
        assert self.client is not None
        channel_id = msg.conversation_id
        mode = override_mode or self.cfg.messaging.queue_mode
        if mode != "queue" and not msg.attachments:
            provider = self.sessions.get_provider(session_key)
            steer = getattr(provider, "steer", None)
            # Only steer when a turn is GENUINELY in flight (see the Telegram
            # dispatcher for the post-turn-bookkeeping race rationale).
            has_active = getattr(provider, "has_active_turn", None)
            live = has_active is None or bool(has_active())
            steered = bool(
                live
                and getattr(provider, "supports_steer", False)
                and steer is not None
                and await steer(text)
            )
            if steered:
                r = self._active_renderers.get(session_key)
                if r is not None:
                    r.note_steer(text)
                # Instant, no-extra-bubble ack: react to the user's steer
                # message. Best-effort.
                steer_mid = getattr(msg, "message_id", "")
                if steer_mid:
                    try:
                        await self.client.add_reaction(channel_id, steer_mid, _STEER_ACK_EMOJI)
                    except Exception:
                        logger.debug("discord: steer ack reaction failed", exc_info=True)
                return
        # queue mode (or !queue override, or steer unavailable). Atomic
        # enqueue + receipt under self._queue.lock — see the Telegram dispatcher.
        if not await self._enqueue_with_receipt(
            session_key,
            channel_id,
            text,
            attachments=msg.attachments,
        ):
            await self.handle_message(msg)

    async def _drain_queue(
        self, session_key: str, user_id: str, channel_id: str, thread_id: str = ""
    ) -> None:
        """Collapse every message queued during the just-finished turn into ONE
        combined turn (order preserved). See the Telegram dispatcher for the
        lock/ordering rationale."""
        # Iterate rather than recurse: one burst can span multiple
        # attachment-capped turns, and messages arriving during a drained turn
        # join the same FIFO pump instead of waiting for unrelated future input.
        while True:
            texts: list[str] = []
            attachments: list[Any] = []
            remainder: list[tuple[str, str, dict]] = []
            defer_rest = False
            async with self._queue.lock:
                while True:
                    item = self.sessions.dequeue(session_key)
                    if item is None:
                        break
                    item_attachments = list(item[2].get("attachments") or [])
                    exceeds_attachment_cap = bool(
                        texts
                        and item_attachments
                        and len(attachments) + len(item_attachments)
                        > _MAX_COLLAPSED_ATTACHMENTS
                    )
                    if (
                        not defer_rest
                        and len(texts) < _MAX_COLLAPSE
                        and not exceeds_attachment_cap
                    ):
                        texts.append(item[1])
                        attachments.extend(item_attachments)
                    else:
                        # Once one message no longer fits, defer it and everything
                        # behind it so queue order remains exact.
                        defer_rest = True
                        remainder.append(item)
                for _ts, rtext, rkw in remainder:
                    self.sessions.enqueue(
                        session_key,
                        str(time.time()),
                        rtext,
                        force=True,
                        attachments=list(rkw.get("attachments") or []),
                    )
                if texts:
                    await self._receipt_flip_locked(
                        session_key,
                        channel_id,
                        [text or "[attachment]" for text in texts],
                        len(remainder),
                    )
            if not texts:
                return
            if remainder:
                logger.debug(
                    "discord: drain deferred %d message(s) for %s "
                    "to preserve collapse/attachment caps and FIFO order",
                    len(remainder),
                    session_key,
                )
            combined = "\n\n".join(texts)
            await self.handle_message(
                InboundMessage(
                    channel_type="discord",
                    user_id=user_id,
                    conversation_id=channel_id,
                    text=combined,
                    thread_id=thread_id or None,
                    attachments=attachments,
                ),
                drain=False,
                interpret_commands=False,
            )

    # ── Mid-turn queue receipt (single, in-place, persistent record) ───────

    async def _enqueue_with_receipt(
        self,
        session_key: str,
        channel_id: str,
        text: str,
        *,
        attachments: list[Any] | None = None,
    ) -> bool:
        """Atomically enqueue a mid-turn message and create/grow its collapsing
        receipt, under ``self._queue.lock``. Returns True if queued; False if the
        turn finished in the window (caller runs the message as a fresh turn)."""
        assert self.client is not None
        async with self._queue.lock:
            if not self.sessions.enqueue(
                session_key,
                str(time.time()),
                text,
                force=False,
                attachments=list(attachments or []),
            ):
                return False
            # An attachment-only message has no text; show a placeholder rather
            # than a blank entry in the receipt.
            await self._queue.create_or_grow_locked(
                session_key, self._receipt_surface(channel_id), text or "[attachment]"
            )
            return True

    async def _receipt_flip_locked(
        self,
        session_key: str,
        channel_id: str,
        answered: list[str],
        deferred: int = 0,
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record. Caller MUST
        hold ``self._queue.lock``."""
        assert self.client is not None
        await self._queue.flip_answering_locked(
            session_key, self._receipt_surface(channel_id), answered, deferred
        )

    async def _receipt_finish_cancelled_locked(self, session_key: str, channel_id: str) -> None:
        """Finalize the receipt to a "🛑 Cancelled" record, if present. Caller
        MUST hold ``self._queue.lock``."""
        assert self.client is not None
        await self._queue.finish_cancelled_locked(
            session_key, self._receipt_surface(channel_id)
        )

    def _receipt_surface(self, channel_id: str) -> ReceiptSurface:
        """A receipt surface with this channel's address already bound."""
        # cast, not assert: mypy does not carry an assert-narrowed local
        # into the nested class body below, so the closure would still see
        # ``DiscordClient | None``. The caller path always has a live client.
        client = cast("DiscordClient", self.client)

        class _Surface:
            label = "discord"

            async def send_receipt(self, body: str) -> Any | None:
                return await client.send_message(channel_id, body)

            async def edit_receipt(self, msg_id: Any, body: str) -> None:
                await client.edit_message(channel_id, msg_id, body)

        return _Surface()

    @asynccontextmanager
    async def _routing_turn(self, channel_id: str) -> "AsyncIterator[list[int]]":
        """Serialize one channel's route -> refusal send -> settle, because delivery is
        what retires the refusal. Only THIS channel, never the accepted turn, and not
        `_bind_lock`, which `choose()` holds across its own Discord round-trips."""
        lock, deciders = self._routing_locks.setdefault(channel_id, (asyncio.Lock(), []))
        deciders.append(1)
        try:
            async with lock:
                yield deciders
        finally:
            deciders.pop()
            if not deciders:
                self._routing_locks.pop(channel_id, None)

    async def _handle_stop(
        self,
        user_id: str,
        channel_id: str,
        thread_id: str,
        resumed_key: str | None,
    ) -> None:
        """Hard cancel: abort the in-flight turn and clear everything."""
        assert self.client is not None
        session_key = resumed_key or self._session_key(user_id, thread_id)
        cancelled_turn = False
        if self.sessions.is_busy(session_key):
            provider = self.sessions.get_provider(session_key)
            cancel = getattr(provider, "cancel", None)
            if cancel is not None:
                try:
                    await cancel(wait_ack_timeout=0)
                    cancelled_turn = True
                except Exception:
                    logger.warning(
                        "discord !stop: cancel failed for %s",
                        session_key,
                        exc_info=True,
                    )
        async with self._queue.lock:
            self.sessions.clear_queue(session_key)
            await self._receipt_finish_cancelled_locked(session_key, channel_id)
        await self.client.send_message(
            channel_id,
            "🛑 Stopped." if cancelled_turn else "🛑 Nothing was running — queue cleared.",
        )

    # ── Button handler (client's on_interaction) ───────────────────────────

    async def on_interaction(self, itx: "DiscordInteraction") -> None:
        """Route a button press: approval decisions or [OPTIONS:] choices."""
        assert self.client is not None
        # Auth first (deny-by-default short-circuit).
        if not self._authorized(itx.user_id):
            return
        # Guild buttons are accepted only in an allow-listed channel that
        # Discord confirms is a thread. This mirrors transport.receive().
        thread_id = itx.channel_id if itx.guild_id else ""
        if itx.guild_id and (
            thread_id not in self._allowed_threads
            or not await self.client.is_thread_channel(thread_id)
        ):
            return
        # Ack FIRST (after auth) to dismiss Discord's "interaction failed" state —
        # the governance check below does off-loop profile-store I/O that can, on a
        # slow FS, exceed Discord's ~3s interaction-ack deadline. Acking is a no-op
        # UI dismissal; it does NOT resolve the approval or start a turn.
        await self.client.ack_component_interaction(itx.interaction_id, itx.interaction_token)

        data = itx.custom_id or ""

        # Inbound channels-governance gate (off-loop) — a button press RESOLVES a
        # tool approval (executes the governed tool) or injects an [OPTIONS:]
        # choice (starts a turn), so it must pass the SAME gate as a message BEFORE
        # any resolution. Without it, an admin deny added after connect could still
        # execute a governed tool via a stale approval button.
        # EXCEPTION: an explicit REJECT of a tool approval ("a:...:0") is a DENIAL —
        # exactly what a channels-deny wants — so let it resolve the pending future
        # as refused rather than silently dropping it (which would strand the
        # kiro-cli approval until timeout, ~300s). Approve presses and [OPTIONS:]
        # turns stay blocked.
        _is_reject_press = data.startswith("a:") and data.rpartition(":")[2] == "0"
        if not _is_reject_press and not await channel_inbound_permitted("discord"):
            logger.info("discord interaction dropped: denied by channels governance policy")
            return

        # Session picker: "s:<nonce>:<index>". The controller binds the nonce
        # to the owner, channel, message, TTL, and exact server-side choice list.
        # Guild-side refusal is defence in depth: show_picker already refuses
        # outside a DM, so a guild `s:` press means a stale or forged button —
        # honouring it would replay private transcript into a shared thread.
        if data.startswith("s:"):
            if itx.guild_id:
                return
            await self._session_resume.choose(self.client, itx, data)
            return

        # Tool-approval decision: "a:<request_id>:<nonce>:<1|0>". The nonce is
        # validated by resolve_global — a stale button (reused request ID from
        # before a restart, or an earlier prompt) fails closed.
        if data.startswith("a:"):
            body = data[2:]
            head, _, flag = body.rpartition(":")
            rid, _, nonce = head.rpartition(":")
            approved = flag == "1"
            key = DiscordApprovalDecider.key(
                self._inbound_session_key(itx.user_id, itx.channel_id, thread_id),
                rid,
            )
            resolved = DiscordApprovalDecider.resolve_global(key, approved, nonce=nonce)
            if resolved:
                verdict = "✅ Approved" if approved else "🚫 Denied"
            else:
                # No pending decision — already timed out (deny-by-default) or
                # answered. Don't imply the press took effect.
                verdict = "⌛ This approval already expired."
            await self.client.edit_message(itx.channel_id, itx.message_id, verdict, components=[])
            return

        # [OPTIONS:] choice: "opt:<i>" — label recovered from the button text.
        if data.startswith("opt:"):
            choice_text = itx.label
            # Retire the buttons but KEEP the original answer text intact —
            # a components-only PATCH leaves the content unchanged.
            await self.client.edit_message_components(itx.channel_id, itx.message_id, [])
            if not choice_text:
                await self.client.send_message(
                    itx.channel_id,
                    "⚠️ Couldn't read that choice — please type it instead.",
                )
                return
            # Echo the picked option as a quoted line (a button tap can't
            # render as a real user message), then re-dispatch as a fresh turn.
            await self.client.send_message(itx.channel_id, f"> {choice_text}")
            synthetic = InboundMessage(
                channel_type="discord",
                user_id=itx.user_id,
                conversation_id=itx.channel_id,
                text=choice_text,
                thread_id=thread_id or None,
            )
            await self.handle_message(synthetic)

    # ── Public injection surface ────────────────────────────────────────────
    # Contract for out-of-band callers (AutoNudge fire path, the REST create
    # endpoint, future channel injectors): synthetic turns bypass
    # transport.receive, so authorization and session-key derivation MUST go
    # through these methods — renaming the private helpers behind them breaks
    # loudly here instead of silently at fire time.

    def is_authorized(self, user_id: str) -> bool:
        """Deny-by-default allowlist check for out-of-band (synthetic) turns."""
        return self._authorized(user_id)

    def current_session_key(self, user_id: str) -> str:
        """The user's CURRENT DM session key (dm_scope + ``!new`` generation)."""
        return self._session_key(user_id)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _authorized(self, user_id: str) -> bool:
        # Deny-by-default (interactions bypass transport.receive, so re-check).
        return bool(user_id) and bool(self._allowed) and user_id in self._allowed

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _persisted_agent(self, session_key: str) -> str:
        """The agent a session was recorded with, or "" when unknown.

        Blocking (reads the conversation log's metadata) — call via
        ``asyncio.to_thread``. Returns "" on any failure so the caller falls back
        to the channel's own agent rather than the turn failing.

        ``"default"``/``"auto"`` are dashboard sentinels meaning "let the backend
        pick", NOT agent names -- most dashboard sessions record ``"default"``.
        Forwarding one reaches ACP ``session/set_mode``, which rejects it with
        ``Mode 'default' not found`` and fails every resumed turn, so they are
        normalized to "" and the channel's own agent is used instead.
        """
        if self.conv_log is None:
            return ""
        try:
            meta = self.conv_log.get_metadata(session_key)
        except Exception:
            logger.debug(
                "discord: could not read persisted agent for %s", session_key, exc_info=True
            )
            return ""
        recorded = str((meta or {}).get("agent") or "").strip()
        if recorded.lower() in _AGENT_SENTINELS:
            return ""
        return recorded

    @staticmethod
    def _scope_id(user_id: str, thread_id: str = "") -> str:
        return f"thread:{thread_id}" if thread_id else f"user:{user_id}"

    def _session_key(self, user_id: str, thread_id: str = "") -> str:
        scope_id = self._scope_id(user_id, thread_id)
        gen = self._conv.current_gen(scope_id)
        return build_dm_session_key(
            "discord",
            self._resolve_agent(),
            thread_id or user_id,
            gen=gen,
            dm_scope=("per-channel-peer" if thread_id else self.cfg.messaging.dm_scope),
            chat_type=("group" if thread_id else "direct"),
        )

    def _inbound_session_key(
        self,
        user_id: str,
        channel_id: str,
        thread_id: str = "",
    ) -> str:
        resumed = self._session_resume.resumed_session(channel_id)
        return resumed or self._session_key(user_id, thread_id)

    def _seed_gen(self, scope_id: str) -> int:
        if scope_id.startswith("thread:"):
            thread_id = scope_id.removeprefix("thread:")
            bucket = build_dm_session_key(
                "discord",
                self._resolve_agent(),
                thread_id,
                dm_scope="per-channel-peer",
                chat_type="group",
            )
            return self.sessions.max_generation(bucket)
        user_id = scope_id.removeprefix("user:")
        return seed_generation(
            self.sessions,
            channel="discord",
            agent=self._resolve_agent(),
            user_id=user_id,
            dm_scope=self.cfg.messaging.dm_scope,
        )

    def _origin_mirror_link(self, channel_id: str) -> ChannelLink:
        """The mirror location for the conversation a session is being read in.

        One definition shared by the automatic bind, ``!link`` and ``!unlink``: an
        unlink matches an occupied location by VALUE, so a second spelling of
        "this conversation" would let the release miss the binding the bind wrote.

        No ``thread_id``: a Discord thread IS a channel with its own id (the
        inbound path takes ``thread_id`` FROM ``channel_id``), so *channel_id*
        already scopes a thread conversation, and it is also the id the transport
        posts to.
        """
        return ChannelLink("discord", channel_id=channel_id)

    def _bind_origin_mirror(self, session_key: str, channel_id: str) -> None:
        """Mirror this conversation's dashboard tab back to Discord, unasked.

        The rule, the re-assert and the opt-out live in
        :func:`~kiro_crew.messaging.link.bind_origin_mirror`, shared with the
        Telegram dispatcher; this only supplies Discord's spelling of "this
        conversation".

        Synchronous and called ON the loop, like every other session-map
        mutation. Interleaving is ordered by ``session_map._MAP_LOCK`` (held for
        the whole of each guarded mutation, including the ``os.replace``), not by
        the loop; what keeps the call here is that the write is BOUNDED — one
        whole-map rewrite whose cost the loop pays once per conversation, on its
        first turn only. ``test_the_binding_write_stays_on_the_loop_thread``
        ratchets that placement.
        """
        bind_origin_mirror(
            self.sessions,
            key=session_key,
            location=self._origin_mirror_link(channel_id),
        )

    async def _handle_link(
        self,
        user_id: str,
        channel_id: str,
        thread_id: str,
        resumed_key: str | None,
    ) -> None:
        """Re-enable mirroring of this conversation's dashboard tab back here.

        Mirroring is automatic (see :meth:`_bind_origin_mirror`), so this is the
        withdrawal of a previous ``!unlink`` rather than the only way to turn it
        on. Clearing the opt-out is the load-bearing half: rebinding without it
        would be undone by the next automatic bind check.
        """
        assert self.client is not None
        # Refuse while a resumed session owns this conversation: linking would
        # rebind the same location and silently strand the resumed session. The
        # owner comes from the turn's routing decision, not a fresh resolve — a
        # second resolve is a second answer, and the gap between them is where a
        # concurrent rebind slips through.
        if resumed_key is not None:
            await self.client.send_message(
                channel_id,
                "⚠️ A resumed session is active here. Send `!unlink` first.",
            )
            return
        key = self._session_key(user_id, thread_id)
        try:
            # One write for the whole sequence: each of these mutations would
            # otherwise rewrite the entire session map, stalling the loop three
            # times for what is one user-visible action.
            #
            # The claim goes FIRST inside the batch on purpose. ``batched_save``
            # writes on the way out even when the block raises, so a refusal
            # raised after the opt-out withdrawal would PERSIST that withdrawal
            # for a link that never happened. ``set_mirror_link`` refuses before
            # it mutates anything, so ordering it first leaves the batch clean
            # and nothing is written.
            with self.sessions.batched_save():
                self.sessions.set_mirror_link(
                    key,
                    self._origin_mirror_link(channel_id),
                    reason=UNBIND_REASON_ORIGIN_REBIND,
                )
                self.sessions.set_mirror_opt_out(key, False)
                # Drop any pre-unification row so a stale binding cannot outlive
                # the rebind (reads prefer the channel key, but a leftover row
                # would still answer a clear).
                self.sessions.clear_mirror_link(
                    legacy_dashboard_mirror_key(key), reason=UNBIND_REASON_ORIGIN_REBIND
                )
        except ConversationOwnershipConflict:
            # Reachable past the resumed-session check above because that check
            # fails CLOSED on duplicate inbound bindings: with two of them at this
            # conversation `resumed_session` denies routing and returns None,
            # while the claim is still refused. Same instruction either way, and
            # reporting it beats surfacing a traceback as a generic command
            # failure.
            logger.info("discord link refused: conversation already held")
            await self.client.send_message(
                channel_id,
                "⚠️ Another session is already linked here. Send `!unlink` first.",
            )
            return
        await self.client.send_message(
            channel_id,
            "✅ Linked. Replies from the dashboard for this conversation will "
            "also show up here. Send `!unlink` to stop.",
        )

    async def _handle_unlink(self, user_id: str, channel_id: str, thread_id: str = "") -> None:
        assert self.client is not None
        # A resumed session takes precedence: it is what the user is actually
        # talking to, so releasing it is the only way back to their own
        # conversation from Discord.
        try:
            left_resumed = await self._session_resume.leave_resumed_session(channel_id)
        except ResumeReleaseError:
            await self.client.send_message(channel_id, _RELEASE_FAILURE)
            return
        if left_resumed is not None:
            await self.client.send_message(
                channel_id,
                "✅ Left the resumed session. Back to your Discord conversation.",
            )
            return
        key = self._session_key(user_id, thread_id)
        # Persist the refusal BEFORE releasing: mirroring is re-asserted on every
        # inbound turn, so a release alone would be undone by the user's next
        # message. Batched with the release so the pair is one whole-map write.
        with self.sessions.batched_save():
            self.sessions.set_mirror_opt_out(key, True)
            reply, swept = release_conversation_location(
                self.sessions,
                key=key,
                location=self._origin_mirror_link(channel_id),
                channel="discord",
            )
        if swept:
            # A swept binding can belong to a dashboard slot whose link chip is
            # projected at push time — nudge the dashboard like every other
            # binding mutation does.
            self._session_resume._push_slots()
        await self.client.send_message(channel_id, reply)

    def _live_dashboard_slot(self, session_key: str) -> Any | None:
        """The OPEN dashboard slot for *session_key*, or ``None``.

        Only meaningful for a resumed ``dashboard:`` key.
        """
        if not session_key.startswith("dashboard:"):
            return None
        state = getattr(self._session_resume, "dashboard_state", None)
        getter = getattr(state, "get_slot", None) if state is not None else None
        if not callable(getter):
            return None
        try:
            return getter(session_key[len("dashboard:") :])
        except Exception:
            logger.debug(
                "discord: live dashboard slot lookup failed for %s", session_key, exc_info=True
            )
            return None

    def _mirror_turn_to_live_slot(self, session_key: str, user_text: str, reply_text: str) -> bool:
        """Land a resumed turn in the live dashboard window. Loop-side only.

        A disk-only append is not enough. The dashboard save writes
        ``meta + frozen prefix + its own window + foreign tail``, so a line
        appended to disk BEFORE a later dashboard turn is re-serialized AFTER
        it, and the transcript reads back out of chronological order. Appending
        to the live window puts the turn in the region the save re-serializes,
        so ordering is preserved. Mirrors ``dashboard/cron_inject.py``, which
        appends to the slot and persists idempotently for the same reason.

        Returns True when the in-memory slot took the turn, so the disk write
        can use the idempotent append and not duplicate it.
        """
        slot = self._live_dashboard_slot(session_key)
        if slot is None:
            return False
        try:
            slot.append("user", user_text, "msg msg-u")
            if reply_text:
                slot.append("assistant", reply_text, "msg msg-a")
        except Exception:
            logger.debug(
                "discord: could not mirror turn into live slot %s", session_key, exc_info=True
            )
            return False
        state = getattr(self._session_resume, "dashboard_state", None)
        push = getattr(state, "push_slots_update", None)
        if callable(push):
            try:
                push()
            except Exception:
                logger.debug("discord: slots push after resumed turn failed", exc_info=True)
        return True

    def _persist_turn(
        self,
        session_key: str,
        user_text: str,
        reply_text: str,
        is_new: bool,
        mirrored: bool = False,
        agent: str | None = None,
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart).

        When the turn already went into a live dashboard slot (*mirrored*), the
        disk write must be idempotent: that slot's own save re-serializes its
        window, so a plain append would persist the same message twice.
        """
        if self.conv_log is None:
            return
        if mirrored:
            self.conv_log.append_if_absent(session_key, "user", user_text, agent=agent)
            if reply_text:
                self.conv_log.append_if_absent(session_key, "assistant", reply_text, agent=agent)
        else:
            self.conv_log.append(session_key, "user", user_text, agent=agent)
            if reply_text:
                self.conv_log.append(session_key, "assistant", reply_text, agent=agent)
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Discord"
            self.conv_log.set_title(session_key, title)

    async def _surface_own_session(self) -> None:
        """Surface a newly created Discord session in the dashboard immediately."""
        from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

        # Keep compatibility with the session-resume controller's older state
        # attachment while all gateways move through register_channel_transport.
        if not hasattr(self, "dashboard_state"):
            self.dashboard_state = getattr(self._session_resume, "dashboard_state", None)
        await surface_dispatcher_session(self)

    async def _maybe_notice(
        self, channel_id: str, scope_id: str, session_key: str, provider: Any
    ) -> None:
        """Soft-threshold context warning as a SEPARATE message (not persisted).

        The hard-compaction backstop is the backend autocompactor
        (``session.autocompact_pct``).
        """
        pct = self.sessions.check_context_usage(session_key, provider)
        soft_pct = self.cfg.discord.soft_threshold_pct
        if pct >= soft_pct and not self._conv.is_awaiting(scope_id):
            self._conv.set_awaiting(scope_id)
            assert self.client is not None
            await self.client.send_message(
                channel_id,
                "⚠️ Context is getting long. Use `!compact` to compress or "
                "`!new` to start fresh.",
            )

    async def _handle_compact(
        self,
        user_id: str,
        channel_id: str,
        thread_id: str,
        resumed_key: str | None,
    ) -> None:
        """In-place ACP ``/compact`` on the conversation's session."""
        assert self.client is not None
        session_key = resumed_key or self._session_key(user_id, thread_id)
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self.client.send_message(
                    channel_id,
                    "⏳ Still working on your last message — try `!compact` " "once it finishes.",
                )
            else:
                await self.client.send_message(channel_id, "No active session to compact.")
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self.client.send_message(channel_id, "No active session to compact.")
                return

            status_id = await self.client.send_message(channel_id, "🔄 Compacting context…")
            result_text: str | None = None

            def _safe(text: str) -> str:
                """Redact backend-echoed, LLM-influenced compaction text before
                it reaches the external Discord surface: normal turns get this
                via the shared TurnDriver, but this path sends directly."""
                cleaned, _ = redact_credentials(text or "")
                cleaned, _ = redact_exfiltration_urls(cleaned)
                return cleaned

            try:

                # Compaction runs over the prompt transport:
                # provider.compact() drives /compact via session/prompt (the
                # commands/execute path does NOT run compaction — it returns
                # with no status). Bound compact()'s prompt
                # turn here, then let wait_for_compaction() own its OWN deadline
                # for a status emitted async after end_turn — it must NOT be
                # nested inside another timeout, or the graceful "timed out"
                # branch is unreachable and a slow-but-healthy session gets
                # destroyed by the outer TimeoutError.
                await asyncio.wait_for(provider.compact(), timeout=120)
                cr = await provider.wait_for_compaction()
                if cr["type"] == "completed":
                    # ``summary`` is model-facing compacted context, not a
                    # user-facing receipt. Never publish its orchestration text.
                    result_text = "✅ Context compacted."
                elif cr["type"] == "failed":
                    err = _safe(cr.get("summary", ""))
                    result_text = (
                        f"❌ Compaction failed: {err}" if err else "❌ Compaction failed."
                    )
                else:
                    result_text = "⚠️ Compaction timed out."
            except Exception:
                logger.warning("Discord !compact failed for %s", session_key, exc_info=True)
                result_text = "❌ Compaction failed unexpectedly."
                # Drop the wedged native conversation, NOT the session's channel
                # identity: the map entry carries the mirror binding, so a full
                # ``destroy`` would silently unlink a mirrored conversation.
                # Housekeeping never unlinks (see ``SessionMap.prune`` and
                # ``SessionManager._recycle_held``).
                try:
                    await self.sessions.discard_conversation(session_key)
                except Exception:
                    logger.debug(
                        "Discord: discard after compact failure failed",
                        exc_info=True,
                    )

            final = result_text or "✅ Context compacted."
            if status_id:
                await self.client.edit_message(channel_id, status_id, final)
            else:
                await self.client.send_message(channel_id, final)
        finally:
            self.sessions.release(session_key)
