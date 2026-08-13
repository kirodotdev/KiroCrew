"""Full new-path dispatch: TelegramTransport -> TurnDriver -> TelegramRenderer.

``TelegramTransport.receive()`` authorizes + normalizes an inbound update and
hands the ``InboundMessage`` to :meth:`TelegramDispatcher.handle_message`,
which mirrors the Slack transport dispatch:

    command intercept (/new, /compact, /model, /yolo, /help)
    -> construct TelegramRenderer + on_turn_start (immediate ack placeholder)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft-threshold notice)  # each guarded
    -> renderer.close() + session release   # in finally

``on_callback`` resolves interactive tool approvals (``a:<rid>:<1|0>`` ->
``TelegramApprovalDecider.resolve_global``), applies ``/model`` picks
(``m:<index>``) and re-injects ``[OPTIONS:]`` choices (``opt:<i>``) as fresh
turns.

Dependency direction is ``telegram -> messaging`` (allowed). The security
``tool_gate`` and spawn auto-approve are wired inline off ``ctx_builder.hooks``
(channel-neutral) so this module never imports ``kiro_crew.slack``.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.acp.client import AcpError
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.messaging.attachments import IngestLimits, append_attachment_context
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.dispatch import delivery_is_muted
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    UNBIND_REASON_ORIGIN_REBIND,
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    legacy_dashboard_mirror_key,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.messaging.renderer import SilentRenderer
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.safety_override import describe_grant_lifetime, safety_override
from kiro_crew.security import redact, redact_local_paths
from kiro_crew.sel import sel
from kiro_crew.telegram.attachments import process_telegram_attachments
from kiro_crew.telegram.commands import (
    ConversationState,
    build_help_text,
    format_ttl,
    is_bare_mid_turn_override,
    parse_command,
    parse_command_argument,
    parse_dashboard_ttl,
    parse_mid_turn_override,
)
from kiro_crew.telegram.renderer import TelegramApprovalDecider, TelegramRenderer
from kiro_crew.telegram.transport import (
    TELEGRAM_CAPABILITIES,
    TelegramInboundMessage,
    forum_gate_outcome,
)

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.telegram.client import TelegramCallback, TelegramClient

from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI
from kiro_crew.messaging.queue_receipt import (
    ReceiptQueue,
    ReceiptSurface,
)

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Telegram sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the Slack
# path's _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"

# Upper bound on how many queued messages collapse into a single combined turn.
# A single human won't realistically burst past this mid-turn; anything beyond
# stays queued and drains after the next turn (logged for observability).
_MAX_COLLAPSE = 50

# Keep queue collapse within the shared ingestion layer's per-turn file cap.
# Without this, two queued 10-photo albums would concatenate to 20 attachments
# in one turn and ingest_attachments would silently process only the first 10,
# losing the second album entirely. Mirrors discord/transport_dispatch.py.
_MAX_COLLAPSED_ATTACHMENTS = IngestLimits().max_attachments

_HELP_TEXT = build_help_text()


# Hard cap for a user-visible failure reason: one short chat message, never a
# traceback. Generous enough for the ACP entitlement message (which lists the
# models the account does include) while still bounding hostile input.
_FAILURE_REASON_MAX_CHARS = 500


def _user_safe_failure_reason(exc: BaseException) -> str | None:
    """A bounded, user-safe reason for a failed turn, or None for the generic text.

    Only a *permanent* :class:`AcpError` (``transient is False``) yields a
    reason: its message is already user-facing and actionable (e.g. names the
    models the account does include), and the generic "please try again"
    placeholder would be actively wrong for it. Transient and unclassified
    failures keep the retry wording, and any other exception type returns
    None — arbitrary internal errors must never leak into chat (CWE-209).

    The text is untrusted output: credentials/exfil URLs and local filesystem
    paths are redacted, newlines are collapsed, and the length is hard-capped.
    """
    if not isinstance(exc, AcpError) or exc.transient is not False:
        return None
    try:
        text = redact_local_paths(redact(str(exc)))[0]
        text = " ".join(text.split())
    except Exception:
        # Fail closed to the generic placeholder: this helper runs inside the
        # turn's except block, so it must never raise (that would skip
        # record_failure and propagate out of the handler).
        logger.debug("Telegram: failure-reason sanitization failed", exc_info=True)
        return None
    if not text:
        return None
    if len(text) > _FAILURE_REASON_MAX_CHARS:
        text = text[: _FAILURE_REASON_MAX_CHARS - 1].rstrip() + "…"
    return f"⚠️ {text}"


# How long a /model picker stays pressable, and how many pickers are retained.
# Both are bounds on unbounded growth (one entry per press-less /model), not UX
# knobs: an expired or evicted picker answers "reopen /model" rather than acting
# on a stale list.
_MODEL_PICKER_TTL_SECS = 300.0
_MODEL_PICKER_MAX = 50
#: Buttons a picker shows. Telegram renders a one-per-row keyboard fine at this
#: size, and the list is the account's own model set, not a catalogue.
_MODEL_PICKER_LIMIT = 24


@dataclass
class _ModelPicker:
    """A posted /model keyboard, resolving a button index back to a model id."""

    route: tuple[str, str]
    chat_id: int
    message_id: int
    created_at: float
    #: ``(model_id, label)`` in button order. ``model_id`` "" is the Auto row.
    choices: tuple[tuple[str, str], ...]


class TelegramDispatcher:
    """Coordinates Telegram turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-user conversation state
    (generation counter + soft-threshold flag). ``handle_message`` is wired as
    the transport's dispatch callback; ``on_callback`` is wired as the client's
    inline-button handler. ``client`` and ``bot_username`` are set by the
    gateway after construction (the latter from ``getMe``, once the token is
    proven).
    """

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        ctx_builder: "ContextBuilder",
        cfg: "KiroCrewConfig",
        allowed_user_ids: set[int],
        agent: str | None = None,
        conv_log: "ConversationLog | None" = None,
        approval_mode: str = APPROVAL_INTERACTIVE,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self._allowed = set(allowed_user_ids or ())
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        self.client: "TelegramClient | None" = None
        # This bot's own registered username (no leading @), from getMe().
        # Empty until the gateway's startup call resolves -- see
        # kiro_crew.telegram.commands._strip_bot_mention for why an unset
        # value means no @-mention is ever treated as ours.
        self.bot_username: str = ""
        self._conv = ConversationState(seed_fn=self._seed_gen)
        # The mid-turn queue receipt: one in-place "queued" bubble per session,
        # plus the lock that serializes check-then-send-then-store against the
        # end-of-turn drain. Both now live in messaging/queue_receipt.py so
        # Telegram and Discord cannot drift on the lock discipline.
        self._queue = ReceiptQueue()
        # session_key -> the running turn's renderer, so a concurrent mid-turn
        # steer (handled in a separate _handle_busy task) can hand it the user's
        # typed steer text for the inline "↪️ steered: …" chip. Set on turn
        # start, popped in finally. Records text only — no buffer slicing, so
        # none of the old steer-split fragility.
        self._active_renderers: dict[str, TelegramRenderer] = {}
        # route -> the model id the user picked with /model, applied to every
        # session this conversation starts from now on. Keyed by ROUTE, not
        # session_key, so the choice survives /new and the idle/daily rotation
        # (a model is a preference about the peer, not about one session).
        self._model_pref: dict[tuple[str, str], str] = {}
        # Live /model pickers awaiting a button press. Telegram caps
        # callback_data at 64 bytes and model ids routinely exceed that, so the
        # button carries an INDEX into this table instead of the id itself.
        self._model_pickers: dict[str, _ModelPicker] = {}

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(
        self,
        msg: InboundMessage,
        *,
        drain: bool = True,
        interpret_commands: bool = True,
    ) -> None:
        """Drive one authorized inbound message through TurnDriver end-to-end."""
        assert self.client is not None, "TelegramDispatcher.client must be set"
        # Inbound channels-governance gate (off-loop) — recheck per message so a
        # host-profile deny added after connect stops dispatch without a restart
        # (the startup gate only blocks CONNECTING). Silently drop on deny.
        if not await channel_inbound_permitted("telegram"):
            logger.info("telegram inbound dropped: denied by channels governance policy")
            return
        user_id = int(msg.user_id)
        chat_id = int(msg.conversation_id)
        text = msg.text
        # Route to the conversation identity. DM (private) -> (direct, user_id),
        # reproducing the pre-forum key EXACTLY; an authorized supergroup forum
        # message always carries a Topic thread -> (forum, "chat:thread"). A
        # threadless General message never reaches here (the forum gate in
        # transport.receive / on_callback denies it); the threadless (forum,
        # "chat") branch below is defensive dead code, not a served path.
        # ``thread`` is the raw Topic id passed to the renderer so its outbound
        # messages thread into the Topic. Everything downstream (session key,
        # generation counter, awaiting flag) keys on ``route`` so /new, idle
        # rotation and /compact are per-topic, not per-user.
        route = self._route_key(
            chat_type=getattr(msg, "chat_type", "private"),
            user_id=user_id,
            chat_id=chat_id,
            thread=getattr(msg, "thread_id", None),
        )
        thread = getattr(msg, "thread_id", None)
        # The Topic id (int) used to thread EVERY dispatcher-originated reply for
        # this turn back into the user's Topic (command confirmations, receipts,
        # the soft-threshold notice); None only for a DM (an authorized forum
        # turn always carries a Topic — General is denied at the gate).
        reply_thread = self._route_thread(route)

        # Per-message mid-turn override: "/queue …" / "/steer …" let the user
        # choose how THIS message is handled if it lands while a turn is running
        # (overriding the global queue_mode). Ordinary commands are parsed
        # against the ORIGINAL text — and when an override prefix IS present,
        # its payload is turn CONTENT, never a command: "/queue /new" queues the
        # literal "/new" text for after the turn instead of executing it now.
        # interpret_commands=False (the queue-drain path) skips BOTH: a drained
        # payload is replayed as pure content, so a queued "/new" reaches the
        # model as text instead of executing on drain.
        override_mode = None
        # Attachments make this a content-bearing turn, not a control command:
        # a caption of "/new" would otherwise intercept and return BEFORE
        # attachment ingestion, silently discarding the photo the user attached
        # to it. Mirrors discord/transport_dispatch.py's interpret_as_command.
        interpret_as_command = interpret_commands and not msg.attachments
        if interpret_as_command and parse_command(text, self.bot_username) is None:
            override_mode, text = parse_mid_turn_override(text, self.bot_username)

        # ── Command intercept (no LLM session needed; skipped for override
        # payloads and drained queue content — see above) ──
        cmd = (
            parse_command(text, self.bot_username)
            if interpret_as_command and override_mode is None
            else None
        )
        if cmd == "new":
            self._conv.bump_gen(route)
            await self._reply(chat_id, "✅ New conversation started.", thread=reply_thread)
            return
        if cmd == "compact":
            self._conv.clear_awaiting(route)
            await self._handle_compact(route, chat_id)
            return
        if cmd == "link":
            await self._handle_link(route, chat_id)
            return
        if cmd == "unlink":
            await self._handle_unlink(route, chat_id)
            return
        if cmd == "help":
            await self._reply(chat_id, _HELP_TEXT, thread=reply_thread)
            return
        if cmd == "stop":
            await self._handle_stop(route, chat_id)
            return
        if cmd == "model":
            await self._handle_model(route, chat_id, parse_command_argument(text))
            return
        if cmd == "yolo":
            await self._handle_yolo(
                chat_id, parse_command_argument(text), user_id, thread=reply_thread
            )
            return
        if cmd == "dashboard":
            await self._handle_dashboard(route, chat_id, text, user_id)
            return
        # A lone "/queue" / "/steer" is a directive missing its message body.
        # Answering with the usage beats forwarding the token to the model, which
        # would answer the literal string and read as a broken feature. Gated on
        # interpret_as_command so a caption on an attachment is never read as a
        # bare directive -- that would answer with usage and drop the file.
        if (
            interpret_as_command
            and override_mode is None
            and is_bare_mid_turn_override(text, self.bot_username)
        ):
            await self._reply(
                chat_id,
                "Those take a message: /queue <msg> or /steer <msg>.",
                thread=reply_thread,
            )
            return

        # ── Mid-turn concurrency: check the CURRENT-generation key for an
        # in-flight turn BEFORE any idle/daily rotation. Rotating first could
        # mint a new key and miss the running turn, letting a second concurrent
        # turn bypass steer/queue. Surface the message (steer or queue) instead
        # of a silent block.
        session_key = self._session_key(route)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(session_key, msg, text, override_mode, thread=reply_thread)
            return

        self._conv.maybe_rotate(
            route,
            time.time(),
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
        )
        session_key = self._session_key(route)
        channel_id = f"telegram:{user_id}"
        # Resolve the kiro-cli agent: an explicit override wins, else the
        # configured default, else the canonical "kirocrew" agent — so the
        # session loads kirocrew-core (spawn_run) instead of kiro-cli's bare
        # built-in default. Mirrors slack/transport_dispatch.py.
        agent = self._resolve_agent()

        decider = (
            TelegramApprovalDecider(session_key=session_key)
            if self.approval_mode == APPROVAL_INTERACTIVE
            else None
        )
        renderer = TelegramRenderer(
            self.client,
            chat_id,
            TELEGRAM_CAPABILITIES,
            session_key=session_key,
            message_thread_id=int(thread) if thread else None,
        )
        # Same gate as Discord, for the same reason: Telegram also runs its own
        # copy of the turn loop rather than going through ``drive_turn``, so a
        # disconnected conversation would otherwise keep answering.
        muted = delivery_is_muted(self.sessions, session_key, TelegramRenderer.channel_type)
        # Handed to the driver AND closed in the finally. Not a reassignment of
        # ``renderer`` because the concrete ``close`` is not inert (it finalizes the
        # "🤔" placeholder and can surface an error), and a muted turn must leave
        # nothing behind in the conversation. Typed as a union rather than the base
        # ``Renderer`` because this channel WIDENS close to take ``failure_reason``.
        out_renderer: TelegramRenderer | SilentRenderer = (
            SilentRenderer(TELEGRAM_CAPABILITIES, TelegramRenderer.channel_type)
            if muted
            else renderer
        )
        # Expose this turn's renderer so a concurrent mid-turn steer (a separate
        # _handle_busy task) can hand it the user's typed steer text for the
        # inline "↪️ steered: …" chip. Popped in finally.
        # Not published when muted: the steer path calls the channel-specific
        # ``note_steer`` and already skips cleanly on absence, so this both
        # silences the chip in a disconnected conversation and keeps that
        # channel-local API off the shared substitute.
        if not muted:
            self._active_renderers[session_key] = renderer

        # Everything acquire-dependent runs INSIDE the try so the finally always
        # finalizes the placeholder (renderer.close -> no perma-"🤔 …"), even if
        # get_or_create itself raises on a cold-start failure. release() is gated
        # on _acquired so we never release a semaphore we didn't hold. Mirrors
        # slack/transport_dispatch.py.
        _acquired = False
        failure_reason: str | None = None
        attachment_temp_paths: list[str] = []
        try:
            # Ack placeholder first (before the potentially slow cold-start);
            # on_turn_start is idempotent so the driver's later call no-ops.
            # Skipped when muted, as in the Discord twin.
            if not muted:
                await renderer.on_turn_start()
            provider, is_new, resumed = await self.sessions.get_or_create(
                session_key,
                agent=agent,
                channel_id=channel_id,
                # "" is the Auto row's stored value; collapse it to None so Auto
                # means "as if never picked". get_or_create gates its own model
                # resolution on `model is None`, so passing "" would skip that
                # and land on the provider factory's narrower fallback instead.
                model=self._model_pref.get(route) or None,
            )
            _acquired = True
            if is_new:
                await self.sessions.set_channel(session_key, channel_id)
            # Bind this chat as the session's outbound mirror so a turn the user
            # later takes from the dashboard is delivered back here. Slack gets
            # this from its own per-turn thread binding; Telegram had it only
            # behind an explicit /link. Called ON the loop, like every other
            # session-map mutation: `_MAP_LOCK` is what orders it against a
            # concurrent mutation, and the write is bounded — one whole-map
            # rewrite, on a conversation's first turn only.
            self._bind_origin_mirror(session_key, route, chat_id)
            # ── Attachment ingestion (mirrors Discord) ──
            if msg.attachments:
                attachment_result = await process_telegram_attachments(
                    self.client, msg.attachments
                )
                attachment_temp_paths = list(attachment_result.temp_paths)
                text = append_attachment_context(text, attachment_result)
            if not text:
                return
            # Publish this turn's session identity so managed MCP tools resolve
            # X-Session-Key; one shared writer lives in messaging.identity.
            await publish_turn_identity(self.sessions, session_key)
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                self.ctx_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=channel_id,
                agent=agent,
                resumed=resumed,
                runtime_source="telegram",
            )

            # PreToolUse security gate (channel-neutral, off ctx_builder.hooks):
            # sensitive-path keystone + governance ceiling + deny-list. Returns
            # "deny" (un-overridable), "auto_approve", or "" (passthrough).
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
                # Preserve the auto_approve_subagent_spawn hook for spawn_run
                # (replicated inline to avoid a telegram -> slack import).
                auto_approve_tool=lambda title: bool(
                    self.ctx_builder
                    and self.ctx_builder.hooks
                    and self.ctx_builder.hooks.auto_approve_subagent_spawn
                    and title == "spawn_run"
                ),
                # /yolo: read the grant per request, not once at boot, so turning
                # it on (or letting it expire) takes effect on the very next tool
                # instead of after a gateway restart. TurnDriver runs the
                # PreToolUse gate BEFORE this, so a hard deny still wins.
                auto_approve_session=lambda: safety_override().is_active(),
                tool_gate=_tool_gate,
            )
            accumulated = await driver.run(full_message)

            # ── Post-turn bookkeeping (each guarded so a failure here can't
            # fall through to the except and re-record the successful turn). ──
            self.sessions.record_success(session_key)
            try:
                await asyncio.to_thread(self._persist_turn, session_key, text, accumulated, is_new, agent)
            except Exception:
                logger.warning(
                    "Telegram: persist_turn failed session=%s", session_key, exc_info=True
                )
            if is_new:
                try:
                    # Circular import: dashboard boot imports channel packages.
                    from kiro_crew.dashboard.channel_slots import (
                        surface_dispatcher_session,
                    )

                    await surface_dispatcher_session(self)
                except Exception:
                    logger.warning(
                        "Telegram: immediate dashboard session surface failed session=%s",
                        session_key,
                        exc_info=True,
                    )
            try:
                await self._maybe_notice(chat_id, route, session_key, provider)
            except Exception:
                logger.warning(
                    "Telegram: maybe_notice failed session=%s", session_key, exc_info=True
                )
            try:
                sel().log_api_access(
                    caller=f"telegram:{user_id}",
                    operation="transport_dispatch.handle",
                    outcome="success",
                    source="telegram",
                    resources=f"session={session_key}",
                )
            except Exception:
                logger.debug("Telegram: success audit failed", exc_info=True)
        except Exception as exc:
            logger.exception("Telegram transport_dispatch: error handling message")
            # Permanent, user-actionable failures (e.g. model entitlement)
            # surface their own bounded reason instead of the misleading
            # generic retry text; everything else stays generic (None).
            failure_reason = _user_safe_failure_reason(exc)
            if _acquired:
                await self.sessions.record_failure(session_key)
        finally:
            # Always finalize the placeholder (no perma-"🤔 …"), even if
            # get_or_create raised before the semaphore was held. Only release
            # the semaphore if we actually acquired it.
            #
            # ``close()`` is best-effort and must NEVER prevent the three steps
            # after it. A renderer that fails to finalize -- a malformed
            # Telegram response, a socket dropped mid-edit -- would otherwise
            # skip ALL of them: the session semaphore is never given back (and
            # because it is keyed by SESSION, every later message in that
            # conversation blocks forever and the queue never drains), the
            # ``_active_renderers`` entry leaks, and the attachment temp files
            # stay on disk. Discord and the shared pipeline both already guard
            # this; Telegram was the remaining copy that did not.
            try:
                await out_renderer.close(failure_reason=failure_reason)
            except Exception:
                logger.warning(
                    "Telegram: renderer.close failed session=%s",
                    session_key,
                    exc_info=True,
                )
            self._active_renderers.pop(session_key, None)
            if _acquired:
                self.sessions.release(session_key)
            await asyncio.to_thread(cleanup_attachments, attachment_temp_paths)

        # Now that the turn is released, run anything that queued during it
        # (queue_mode == "queue"). ``drain`` is False for drained turns so the
        # loop stays iterative at one level (no recursion); ``limit`` bounds it.
        if drain:
            await self._drain_queue(
                session_key,
                user_id,
                chat_id,
                chat_type=getattr(msg, "chat_type", "private"),
                thread=thread,
            )

    async def _handle_busy(
        self,
        session_key: str,
        msg: InboundMessage,
        text: str,
        override_mode: str | None,
        *,
        thread: int | None = None,
    ) -> None:
        """A message arrived mid-turn: steer the running turn or queue for after
        it. ``text`` is the message with any ``/queue``|``/steer`` directive
        stripped; ``override_mode`` ('queue' | 'steer' | None) forces the path for
        THIS message, overriding the global ``queue_mode``."""
        assert self.client is not None
        chat_id = int(msg.conversation_id)
        mode = override_mode or self.cfg.messaging.queue_mode
        # An attachment-bearing message can never take the steer path: ``steer``
        # forwards TEXT ONLY, so steering a photo/document message would deliver
        # its caption and silently drop every file. Such a message always goes to
        # the queue path below, which carries ``attachments`` through the drain.
        # Mirrors discord/transport_dispatch.py's identical gate -- Telegram was
        # missing it, and album buffering makes it far more reachable: a follow-up
        # typed during the debounce window starts a turn, so the album's own flush
        # arrives mid-turn and would have been steered as caption-only.
        if mode != "queue" and not msg.attachments:
            provider = self.sessions.get_provider(session_key)
            steer = getattr(provider, "steer", None)
            # Only steer when a turn is GENUINELY in flight. ``is_busy`` stays
            # True through post-turn bookkeeping (record_success / _persist_turn
            # / _maybe_notice / SEL audit -- all await points), so without this
            # guard a steer could reach kiro-cli for a prompt that already ended
            # -> silently swallowed (no fresh turn, no queue entry), and the
            # steer-ack reaction would land on a message whose turn already
            # finished. When no live turn, fall through to the queue/handle path
            # below (mirrors the queue path's ``force=False`` fallback), so the
            # message is re-run or queued instead of lost.
            has_active = getattr(provider, "has_active_turn", None)
            live = has_active is None or bool(has_active())
            steered = bool(
                live
                and getattr(provider, "supports_steer", False)
                and steer is not None
                and await steer(text)
            )
            if steered:
                # Record the user's OWN words on the running turn's renderer so
                # it can render an inline "↪️ steered: <text>" chip (never the
                # redacted backend echo). Best-effort: no active renderer -> skip.
                r = self._active_renderers.get(session_key)
                if r is not None:
                    r.note_steer(text)
                # Instant, no-extra-bubble ack: react to the user's steer message
                # so a mid-turn steer isn't silent while it waits for the next
                # generation boundary. The steered reply lands at the end of the
                # turn's output (no pre/post split -- that retroactive slice of a
                # single stream leaked fragments across the cut). Best-effort --
                # reactions need Bot API 7.0+.
                steer_mid = getattr(msg, "message_id", 0)
                if steer_mid:
                    try:
                        await self.client.set_message_reaction(chat_id, steer_mid, _STEER_ACK_EMOJI)
                    except Exception:
                        logger.debug("telegram: steer ack reaction failed", exc_info=True)
                return
        # queue mode (or /queue override, or steer unavailable). Enqueue + receipt
        # happen atomically under ``self._queue.lock`` (see ``_enqueue_with_receipt``)
        # so the end-of-turn drain -- which takes the same lock to dequeue + flip
        # -- cannot interleave between the enqueue and the receipt and orphan a
        # bubble. If the turn finished in the window the message is not queued, so
        # we run it now (re-entering handle_message, which re-strips the directive
        # and runs it as a fresh turn) instead of stranding it.
        if not await self._enqueue_with_receipt(
            session_key, chat_id, text, thread=thread,
            attachments=list(msg.attachments) if msg.attachments else None,
        ):
            await self.handle_message(msg)

    async def _drain_queue(
        self,
        session_key: str,
        user_id: int,
        chat_id: int,
        *,
        chat_type: str = "private",
        thread: str | None = None,
    ) -> None:
        """Collapse every message queued during the just-finished turn into ONE
        combined turn (order preserved, blank-line joined) and answer them
        together, rather than replaying each as a separate turn.

        The dequeue + receipt flip run together under ``self._queue.lock`` so a
        concurrent mid-turn ``_enqueue_with_receipt`` (which takes the same lock)
        cannot interleave and leave an orphaned receipt. The combined turn itself
        runs OUTSIDE the lock -- messages that arrive during it open a fresh
        receipt and drain after the next turn. Only the queued text is replayed
        (matching what ``enqueue`` persists for DM channels: text only).
        """
        # Iterate rather than recurse: one burst can span multiple
        # attachment-capped turns, and a message deferred by the cap must drain
        # in THIS pump rather than waiting for unrelated future user input.
        # Mirrors the Discord drain.
        while True:
            texts: list[str] = []
            all_attachments: list[Any] = []
            remainder: list[tuple[str, str, dict]] = []
            defer_rest = False
            async with self._queue.lock:
                # Drain the ENTIRE queue under the lock, then split: the first
                # _MAX_COLLAPSE messages collapse into this turn; the rest are
                # re-enqueued IN ORIGINAL ORDER (the queue is now empty, so
                # re-adding preserves FIFO) to drain after the next turn. This
                # bounds the combined prompt without dropping or reordering surplus.
                while True:
                    item = self.sessions.dequeue(session_key)
                    if item is None:
                        break
                    item_attachments = list(item[2].get("attachments") or [])
                    # Never collapse past the shared ingestion cap: the extra files
                    # would be dropped inside ingest_attachments with the user given
                    # no indication, so defer instead. Mirrors the Discord drain.
                    exceeds_attachment_cap = bool(
                        texts
                        and item_attachments
                        and len(all_attachments) + len(item_attachments)
                        > _MAX_COLLAPSED_ATTACHMENTS
                    )
                    if (
                        not defer_rest
                        and len(texts) < _MAX_COLLAPSE
                        and not exceeds_attachment_cap
                    ):
                        texts.append(item[1])
                        all_attachments.extend(item_attachments)
                    else:
                        # Once one message no longer fits, defer it AND everything
                        # behind it, so queue order stays exact.
                        defer_rest = True
                        remainder.append(item)
                for _ts, rtext, rkw in remainder:
                    self.sessions.enqueue(
                        session_key, str(time.time()), rtext, force=True,
                        attachments=list(rkw.get("attachments") or []),
                    )
                if texts:
                    await self._receipt_flip_locked(session_key, chat_id, texts, len(remainder))
            if not texts:
                return
            if remainder:
                logger.debug(
                    "telegram: drain deferred %d message(s) for %s to respect the "
                    "collapse cap (%d) / attachment cap (%d); they drain in the "
                    "next iteration of this pump, in order",
                    len(remainder),
                    session_key,
                    _MAX_COLLAPSE,
                    _MAX_COLLAPSED_ATTACHMENTS,
                )
            combined = "\n\n".join(texts)
            await self.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id=str(user_id),
                    conversation_id=str(chat_id),
                    text=combined,
                    # Carry the turn's ORIGINAL route so the drained turn resolves to
                    # the SAME forum session key -- a plain DM-shaped InboundMessage
                    # would drain a queued forum message under the DM key instead.
                    thread_id=thread,
                    chat_type=chat_type,
                    attachments=all_attachments,
                ),
                drain=False,
                # Drained payloads are pure turn content: a queued "/new" must reach
                # the model as literal text, not execute as a command on drain.
                interpret_commands=False,
            )

    # ── Mid-turn queue receipt (single, in-place, persistent record) ───────

    def _receipt_surface(self, chat_id: int, thread: int | None) -> ReceiptSurface:
        """A receipt surface with this conversation's address already bound.

        Binding ``chat_id`` AND the forum ``thread`` here is what keeps forum
        routing out of the shared queue module: it never sees an address at all.
        """
        # cast, not assert: mypy does not carry an assert-narrowed local
        # into the nested class body below, so the closure would still see
        # ``TelegramClient | None``. The caller path always has a live client.
        client = cast("TelegramClient", self.client)
        reply = self._reply

        class _Surface:
            label = "telegram"

            async def send_receipt(self, body: str) -> Any | None:
                return await reply(chat_id, body, thread=thread)

            async def edit_receipt(self, msg_id: Any, body: str) -> None:
                await client.edit_message(chat_id, msg_id, body)

        return _Surface()

    async def _enqueue_with_receipt(
        self,
        session_key: str,
        chat_id: int,
        text: str,
        *,
        thread: int | None = None,
        attachments: list[Any] | None = None,
    ) -> bool:
        """Atomically enqueue a mid-turn message and create/grow its collapsing
        "⏳ Queued (N): …" receipt, under ``self._queue.lock``.

        Holding the lock across BOTH the enqueue and the receipt bookkeeping is
        what makes this race-free against the end-of-turn drain (which takes the
        same lock to dequeue + flip): the drain either sees this message queued
        WITH its receipt or sees neither yet -- never a half state that would
        orphan a bubble. Returns True if queued; False if the turn finished in
        the window (``enqueue`` is a no-op once the semaphore is free), so the
        caller runs the message as a fresh turn instead.
        """
        assert self.client is not None
        async with self._queue.lock:
            if not self.sessions.enqueue(
                session_key, str(time.time()), text, force=False,
                attachments=list(attachments or []),
            ):
                return False
            await self._queue.create_or_grow_locked(
                session_key, self._receipt_surface(chat_id, thread), text
            )
            return True

    async def _receipt_flip_locked(
        self, session_key: str, chat_id: int, answered: list[str], deferred: int = 0
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record and drop the
        live entry so the next mid-turn burst opens a fresh receipt. Caller MUST
        hold ``self._queue.lock`` (the drain holds it across dequeue + flip).

        ``answered`` is the subset actually answered by this turn (capped at
        ``_MAX_COLLAPSE``); the count reflects it -- not the full queued list --
        so a >cap burst doesn't overstate what this turn answers. ``deferred``
        (>0 only past the cap) is noted so the remainder isn't silently implied.
        """
        assert self.client is not None
        await self._queue.flip_answering_locked(
            session_key, self._receipt_surface(chat_id, None), answered, deferred
        )

    async def _receipt_finish_cancelled_locked(self, session_key: str, chat_id: int) -> None:
        """Finalize the receipt to a "🛑 Cancelled" record, if present. Caller
        MUST hold ``self._queue.lock`` (/stop holds it across clear_queue + this)."""
        assert self.client is not None
        await self._queue.finish_cancelled_locked(
            session_key, self._receipt_surface(chat_id, None)
        )

    async def _handle_dashboard(
        self, route: tuple[str, str], chat_id: int, text: str, user_id: int
    ) -> None:
        """Generate and send a presigned dashboard login link.

        Mirrors the Slack ``/kirocrew dashboard`` implementation: calls
        ``generate_token`` directly (never via shell) and builds the URL from
        the ``dashboard.url`` config (``KIROCREW_PORT`` overrides the port,
        matching every other link producer).

        DM-only: a presigned link posted into a forum Topic would hand a
        dashboard login to every member of the supergroup, so group requests
        are refused with a pointer to DM — the same token-leak policy as
        Slack's always-DM delivery.
        """
        assert self.client is not None
        from kiro_crew.dashboard.token_auth import MAX_SESSION_TTL_SECS, generate_token
        from kiro_crew.dashboard.urls import dashboard_origin, parse_dashboard_url

        thread = self._route_thread(route)
        if route[0] != CHAT_TYPE_DIRECT:
            await self._reply(
                chat_id,
                "🔒 Dashboard links are only sent in a direct message — "
                "DM me `/kirocrew dashboard`.",
                thread=thread,
            )
            return
        ttl_secs = min(parse_dashboard_ttl(text), MAX_SESSION_TTL_SECS)
        try:
            token = generate_token(str(user_id), ttl_seconds=ttl_secs)
            origin = dashboard_origin(self.cfg.dashboard.url)
            if not origin:
                # No configured dashboard.url: fall back to the local port
                # (parse_dashboard_url applies the KIROCREW_PORT override).
                _, port = parse_dashboard_url(self.cfg.dashboard.url)
                origin = f"http://localhost:{port}"
            url = f"{origin}/?token={token}"
            ttl_display = format_ttl(ttl_secs)
            # Credential issuance MUST be audited (backend-security-controls):
            # mirrors slack.dashboard_token and telegram.yolo_mode above.
            sel().log_api_access(
                caller=str(user_id),
                operation="telegram.dashboard_token",
                outcome="ok",
                source="telegram",
                resources=f"ttl={ttl_secs}",
            )
            await self._reply(
                chat_id,
                f"🔗 Dashboard link (valid {ttl_display}):\n{url}",
                thread=thread,
            )
        except Exception as exc:
            logger.warning(
                "telegram /kirocrew dashboard: token generation failed", exc_info=True
            )
            try:
                sel().log_api_access(
                    caller=str(user_id),
                    operation="telegram.dashboard_token",
                    outcome="error",
                    source="telegram",
                    resources=f"ttl={ttl_secs}",
                )
            except Exception:
                # The audit trail must never turn a user-facing failure reply
                # into a crash; the warning above already captured the error.
                pass
            await self._reply(
                chat_id,
                f"⚠️ Could not generate dashboard link: {exc}",
                thread=thread,
            )

    async def _handle_stop(self, route: tuple[str, str], chat_id: int) -> None:
        """Hard cancel: abort the in-flight turn and clear everything.

        Aborts the running turn via the provider's cooperative ACP cancel,
        drops every queued message, and finalizes the queue receipt. On a shared
        runtime the cancel is cooperative (it cannot force-kill a co-tenant), so
        the turn stops at the next safe point. Fire-and-forget (no ack wait) so
        the acknowledgement is snappy.
        """
        assert self.client is not None
        session_key = self._session_key(route)
        thread = self._route_thread(route)
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
                        "telegram /stop: cancel failed for %s", session_key, exc_info=True
                    )
        async with self._queue.lock:
            self.sessions.clear_queue(session_key)
            await self._receipt_finish_cancelled_locked(session_key, chat_id)
        await self._reply(
            chat_id,
            "🛑 Stopped." if cancelled_turn else "🛑 Nothing was running — queue cleared.",
            thread=thread,
        )

    # ── /yolo (global auto-approve grant) ──────────────────────────────────

    async def _handle_yolo(
        self, chat_id: int, arg: str, user_id: int, *, thread: int | None = None
    ) -> None:
        """Report or change the global auto-approve grant.

        Reads and writes the process-wide :func:`safety_override` grant — the
        SAME one the dashboard toggle and Slack's ``/kirocrew yolo`` drive, so a
        grant taken here shows up (and expires) everywhere. Reachable only by an
        allow-listed Telegram user, because ``transport.receive`` is
        deny-by-default and owner-only before dispatch ever runs.

        Turning it on does NOT weaken the PreToolUse security gate: the
        sensitive-path keystone, governance ceiling and deny-list all run ahead
        of the auto-approve ladder in ``TurnDriver``, so a hard DENY still wins.

        The three grant mutators run off-loop: ``activate`` resolves the ad-hoc
        duration through a live config read and every one of them writes a SEL
        record (activation's is ``critical=True``), so calling them inline would
        put filesystem latency on the event loop and stall every other chat and
        heartbeat task on a slow disk.
        """
        so = safety_override()
        action = arg.strip().lower().split()[0] if arg.strip() else ""

        if action in ("on", "off", "renew"):
            outcome = "allowed"
            if action == "on":
                if so.is_active():
                    reply = f"🟢 YOLO is already ON ({describe_grant_lifetime()})."
                elif (await asyncio.to_thread(so.activate, "telegram")).active:
                    reply = (
                        f"🟢 YOLO ON ({describe_grant_lifetime()}) — every tool "
                        f"auto-approves. Denied-by-policy tools are still blocked."
                    )
                else:
                    reply = "❌ Couldn't turn YOLO on (audit system unavailable)."
                    outcome = "denied"
            elif action == "off":
                # Unconditional: deactivate() also zeroes the deadline of a
                # grant that already lapsed, which closes the renew grace
                # window so a later "/yolo renew" cannot resurrect it, and
                # records the operator's decision either way.
                await asyncio.to_thread(so.deactivate, "telegram")
                reply = "🔴 YOLO OFF — tools ask for approval again."
            else:
                renewed = (await asyncio.to_thread(so.renew, "telegram")).renewed
                reply = (
                    f"🟢 YOLO renewed ({describe_grant_lifetime()})."
                    if renewed
                    else "🔴 YOLO is not active — use /yolo on first."
                )
            sel().log_api_access(
                caller=str(user_id),
                operation="telegram.yolo_mode",
                outcome=outcome,
                source="telegram",
                resources=f"yolo_{action}",
            )
            await self._reply(chat_id, reply, thread=thread)
            return

        status = f"ON 🟢 ({describe_grant_lifetime()})" if so.is_active() else "OFF 🔴"
        await self._reply(
            chat_id,
            f"YOLO is {status}.\nUsage: /yolo on | off | renew",
            thread=thread,
        )

    # ── /model (inline-button model picker) ────────────────────────────────

    def _model_choices(self, session_key: str) -> tuple[tuple[str, str], ...]:
        """``(model_id, label)`` rows to offer for this session.

        The ONLY source is what this session's backend advertised at
        ``session/new`` — the set THIS account may actually use, carrying the
        backend's own ids. That is deliberate on both counts: a static catalogue
        would offer models the account cannot reach (a refusal mid-conversation),
        and its display keys would need per-backend translation before the wire,
        whereas an advertised id is what ``set_model`` accepts verbatim.

        Returns just the Auto row when nothing is advertised (no live session
        yet), which the caller reads as "there is nothing to pick".
        """
        rows: list[tuple[str, str]] = [("", "Auto (let the backend choose)")]
        provider = self.sessions.get_provider(session_key)
        advertised = getattr(provider, "available_models", None)
        if not callable(advertised):
            return tuple(rows)
        try:
            entries = [m for m in advertised() if isinstance(m, dict)]
        except Exception:  # pragma: no cover - defensive
            logger.warning("telegram /model: available_models failed", exc_info=True)
            return tuple(rows)
        for entry in entries:
            model_id = str(entry.get("modelId") or "").strip()
            # "auto" is already offered as the first row; listing it twice would
            # give the same choice two buttons.
            if not model_id or model_id == "auto":
                continue
            rows.append((model_id, str(entry.get("name") or model_id)))
        return tuple(rows[:_MODEL_PICKER_LIMIT])

    def _prune_model_pickers(self, now: float) -> None:
        """Drop expired pickers, then the oldest ones past the retention cap."""
        for token, picker in list(self._model_pickers.items()):
            if now - picker.created_at > _MODEL_PICKER_TTL_SECS:
                self._model_pickers.pop(token, None)
        while len(self._model_pickers) > _MODEL_PICKER_MAX:
            oldest = min(self._model_pickers, key=lambda t: self._model_pickers[t].created_at)
            self._model_pickers.pop(oldest, None)

    async def _handle_model(self, route: tuple[str, str], chat_id: int, arg: str) -> None:
        """Post the model keyboard (or report the current pick for a bare arg).

        Deliberately button-only: a free-text model id means guessing at names
        the user has no way to enumerate, and a typo lands as a rejected
        ``set_model`` mid-conversation. Any argument is treated as "show me the
        list" rather than parsed.
        """
        assert self.client is not None
        session_key = self._session_key(route)
        thread = self._route_thread(route)
        choices = self._model_choices(session_key)
        if len(choices) <= 1:
            await self._reply(
                chat_id,
                "No model list available yet — send a message first, then /model.",
                thread=thread,
            )
            return

        current = self._model_pref.get(route, "")
        current_label = next(
            (label for mid, label in choices if mid == current),
            current or "Auto",
        )
        header = f"Current model: {current_label}\nPick one:"
        if arg.strip():
            # An argument is not an id to apply — say so once, then show the
            # list anyway so the message is still a step forward.
            header = f"/model takes no argument — pick from the list.\n\n{header}"
        keyboard = [
            [{"text": f"{'• ' if mid == current else ''}{label}", "callback_data": f"m:{index}"}]
            for index, (mid, label) in enumerate(choices)
        ]
        message_id = await self._reply(
            chat_id,
            header,
            thread=thread,
            reply_markup={"inline_keyboard": keyboard},
        )
        if message_id is None:
            return
        now = time.time()
        self._prune_model_pickers(now)
        self._model_pickers[f"{chat_id}:{message_id}"] = _ModelPicker(
            route=route,
            chat_id=chat_id,
            message_id=message_id,
            created_at=now,
            choices=choices,
        )

    async def _apply_model(self, route: tuple[str, str], model_id: str) -> str:
        """Record *model_id* for *route* and push it to the live session.

        *model_id* comes verbatim from the session's advertised list, so it is
        already the id this backend accepts — no canonical translation, which
        would differ per backend and could mangle an id that was correct.

        The preference is stored unconditionally so it reaches the NEXT session
        even when there is nothing live to switch (the common case right after
        ``/new``). When a session does exist, the switch is attempted in place —
        ``session/set_model`` carries the conversation across — and the semaphore
        is taken atomically so the switch cannot interleave JSON-RPC with a turn
        on the same stdio channel.

        Returns the user-facing outcome line.
        """
        label = model_id or "Auto"
        self._model_pref[route] = model_id
        session_key = self._session_key(route)
        live = self.sessions.has_session(session_key)
        # Two different promises, because the preference reaches a session only
        # at creation: ``get_or_create`` returns a reused session from its fast
        # path before it consults ``model=``. With nothing live the next message
        # starts the session, so it genuinely lands then; with a session already
        # up, only a fresh conversation picks it up.
        deferred = f"✅ Model set to {label} — it applies to your next message."
        next_new = (
            f"✅ Model set to {label} — this conversation keeps its current "
            f"model; the switch applies to your next one (/new)."
        )
        # Auto has no ACP id meaning "let the backend choose", so it can only be
        # recorded; the next session start resolves it from config. Claiming a
        # live switch here would be a lie.
        if not model_id:
            return next_new if live else deferred
        if not live:
            return deferred
        if not await self.sessions.try_acquire(session_key):
            return (
                f"✅ Model set to {label}, but a reply is still running — this "
                f"conversation keeps its current model; the switch applies to "
                f"your next one (/new)."
            )
        try:
            provider = self.sessions.get_provider(session_key)
            set_model = getattr(getattr(provider, "client", None), "set_model", None)
            if set_model is None:
                return next_new
            await set_model(model_id)
        except Exception as exc:
            logger.warning(
                "telegram /model: live set_model failed for %s: %s",
                session_key,
                type(exc).__name__,
                exc_info=True,
            )
            # The stored preference still stands, so the next session gets it —
            # but do not claim the running conversation switched when it did not.
            return (
                f"⚠️ Couldn't switch this conversation to {label} "
                f"({type(exc).__name__}) — it applies to your next "
                f"conversation (/new)."
            )
        finally:
            self.sessions.release(session_key)
        return f"✅ Now using {label}."

    # ── Inline-button handler (client's on_callback) ───────────────────────

    async def on_callback(self, cb: "TelegramCallback") -> None:
        """Route an inline-keyboard press: approval decisions or [OPTIONS:]."""
        assert self.client is not None
        # Auth first (deny-by-default short-circuit): don't even ack an
        # unauthorized user's press — avoids a wasted Bot API round-trip.
        if not self._authorized(cb.user_id):
            return
        # Chat-type gate — an authZ boundary that MUST mirror
        # ``transport.receive`` EXACTLY: buttons live on messages the bot sent,
        # so a press can originate from a private DM or an allow-listed
        # supergroup forum Topic. Uses the SHARED ``forum_gate_outcome`` predicate
        # so this fail-closed decision can never drift from the inbound path.
        # NEVER honor a callback from an ordinary group, a non-allow-listed
        # supergroup, or the supergroup General chat (no thread). This gate is
        # ADDITIONAL to the owner/user authorization above, not a replacement.
        # The allow-list source here is LIVE cfg (self.cfg.telegram.*), whereas
        # the transport uses its construction-time frozen copy; that source
        # difference is DELIBERATE (see forum_gate_outcome).
        outcome = forum_gate_outcome(
            cb.chat_type,
            cb.chat_id,
            getattr(cb, "message_thread_id", None),
            allow_forum=self.cfg.telegram.allow_forum,
            allowed_forum_chat_ids=self.cfg.telegram.allowed_forum_chat_ids,
        )
        if outcome is not None:
            sel().log_api_access(
                caller=str(cb.user_id) or "unknown",
                operation="telegram_transport.on_callback",
                outcome=outcome,
                source="telegram",
            )
            return
        # Answer FIRST (after auth) to dismiss the button spinner — the governance
        # check below does off-loop profile-store I/O that could otherwise delay
        # the callback answer past Telegram's expectation. Answering is a no-op UI
        # dismissal; it does NOT resolve the approval or start a turn.
        await self.client.answer_callback(cb.callback_query_id)

        data = cb.data or ""

        # Inbound channels-governance gate (off-loop) — a callback press RESOLVES a
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
        if not _is_reject_press and not await channel_inbound_permitted("telegram"):
            logger.info("telegram callback dropped: denied by channels governance policy")
            return

        # Route the callback to the same conversation identity its turn used so
        # an approval/[OPTIONS:] press resolves against the correct session key:
        # a private press -> (direct, user_id); an allow-listed forum press ->
        # the per-Topic forum key (chat_type + message_thread_id carried through).
        route = self._route_key(
            chat_type=cb.chat_type,
            user_id=cb.user_id,
            chat_id=cb.chat_id,
            thread=getattr(cb, "message_thread_id", None),
        )
        # Topic id to thread the [OPTIONS:] echo sends back into (None for a DM).
        cb_thread = self._route_thread(route)

        # Tool-approval decision: "a:<request_id>:<1|0>".
        if data.startswith("a:"):
            body = data[2:]
            rid, _, flag = body.rpartition(":")
            approved = flag == "1"
            key = TelegramApprovalDecider.key(self._session_key(route), rid)
            resolved = TelegramApprovalDecider.resolve_global(key, approved)
            if resolved:
                verdict = "✅ Approved" if approved else "🚫 Denied"
            else:
                # No pending decision to resolve — the request already timed out
                # (decider denies by default and pops the key) or was answered.
                # Don't imply the press took effect: a post-timeout "Approve" on
                # an already-denied tool must not display "Approved".
                verdict = "⌛ This approval already expired."
            await self.client.edit_message(
                cb.chat_id, cb.message_id, verdict, reply_markup={"inline_keyboard": []}
            )
            return

        # Model pick: "m:<index>" into the picker posted on this message.
        if data.startswith("m:"):
            token = f"{cb.chat_id}:{cb.message_id}"
            picker = self._model_pickers.get(token)
            expired = picker is not None and (
                time.time() - picker.created_at > _MODEL_PICKER_TTL_SECS
            )
            try:
                index = int(data[2:])
            except ValueError:
                index = -1
            if picker is None or expired or not (0 <= index < len(picker.choices)):
                # Covers expired, evicted, and already-consumed alike — the
                # wording must not claim "expired" for a picker that was simply
                # used, which is what a double-press hits.
                self._model_pickers.pop(token, None)
                await self.client.edit_message(
                    cb.chat_id,
                    cb.message_id,
                    "⌛ This model list is no longer active — send /model again.",
                    reply_markup={"inline_keyboard": []},
                )
                return
            # Consume the picker BEFORE applying: the switch takes a round-trip,
            # and a second press in that window would otherwise apply twice.
            self._model_pickers.pop(token, None)
            model_id, label = picker.choices[index]
            outcome = await self._apply_model(picker.route, model_id)
            sel().log_api_access(
                caller=str(cb.user_id) or "unknown",
                operation="telegram.set_model",
                outcome="allowed",
                source="telegram",
                resources=f"model={label}",
            )
            # One edit carries both the result text and the retired keyboard, so
            # the buttons never outlive the choice they represent.
            await self.client.edit_message(
                cb.chat_id, cb.message_id, outcome, reply_markup={"inline_keyboard": []}
            )
            return

        # [OPTIONS:] choice: "opt:<i>" — label recovered from the button text.
        if data.startswith("opt:"):
            choice_text = cb.label
            # Retire the keyboard but KEEP the original answer text intact --
            # tapping an option must not overwrite the answer bubble. The choice
            # is handled as a fresh turn whose reply arrives as a NEW message.
            await self.client.edit_message_reply_markup(
                cb.chat_id, cb.message_id, {"inline_keyboard": []}
            )
            if not choice_text:
                await self._reply(
                    cb.chat_id,
                    "⚠️ Couldn't read that choice — please type it instead.",
                    thread=cb_thread,
                )
                return
            # Echo the picked option as its own block (a quoted bubble) so the
            # user can see what they chose -- a button tap can't render as a
            # real user message, so this stands in for it. Then re-dispatch the
            # choice as a fresh turn whose answer streams in as a NEW message.
            echoed = await self._reply(
                cb.chat_id,
                f"<blockquote>{html.escape(choice_text)}</blockquote>",
                thread=cb_thread,
                parse_mode="HTML",
                retry_plain=False,
            )
            if echoed is None:  # malformed HTML -> plain fallback
                await self._reply(cb.chat_id, f"» {choice_text}", thread=cb_thread)
            # Re-inject the choice as a fresh turn via the normal path, carrying
            # the callback's ORIGINAL route (chat_type + Topic thread) so a forum
            # [OPTIONS:] press re-dispatches under the SAME forum session key
            # instead of a DM-shaped key.
            synthetic = TelegramInboundMessage(
                channel_type="telegram",
                user_id=str(cb.user_id),
                conversation_id=str(cb.chat_id),
                text=choice_text,
                thread_id=(
                    str(cb.message_thread_id) if getattr(cb, "message_thread_id", None) else None
                ),
                chat_type=cb.chat_type,
            )
            await self.handle_message(synthetic)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _authorized(self, user_id: int) -> bool:
        # Deny-by-default (callbacks bypass transport.receive, so re-check here).
        return bool(user_id) and bool(self._allowed) and user_id in self._allowed

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _route_key(
        self,
        *,
        chat_type: str,
        user_id: int,
        chat_id: int,
        thread: str | int | None,
    ) -> tuple[str, str]:
        """Map an inbound message/callback to its conversation-identity key.

        Returns ``(slot, comp)`` where ``slot`` selects the session namespace:
          * private DM -> ``(CHAT_TYPE_DIRECT, str(user_id))`` -- byte-for-byte
            the pre-forum identity, so DM keys are unchanged.
          * supergroup forum Topic -> ``(CHAT_TYPE_FORUM, "{chat_id}:{thread}")``.

        A threadless supergroup (General) message is denied at the forum gate
        and never reaches here; the ``str(chat_id)`` fallback below is defensive
        dead code (kept for safety), NOT a served route.

        The tuple is used as the ``ConversationState`` key (per-topic generation)
        and, via ``_session_key``, as the session-key ``comp`` + ``chat_type``.
        """
        if chat_type in ("group", "supergroup"):
            comp = f"{chat_id}:{thread}" if thread else str(chat_id)
            return CHAT_TYPE_FORUM, comp
        return CHAT_TYPE_DIRECT, str(user_id)

    @staticmethod
    def _route_thread(route: tuple[str, str]) -> int | None:
        """The forum Topic id for a ``route``, or None for a DM.

        Mirrors ``_route_key``'s ``comp`` encoding: a forum Topic route carries
        ``"{chat_id}:{thread}"`` -> the Topic id; a DM (direct) route -> None.
        An authorized forum turn always carries a Topic (General is denied at
        the gate), so the threadless-``comp`` -> None case is only the defensive
        fallback. Used to thread every dispatcher-originated send back into the
        SAME Topic the turn came from.
        """
        slot, comp = route
        if slot == CHAT_TYPE_FORUM and ":" in comp:
            return int(comp.split(":", 1)[1])
        return None

    async def _reply(
        self, chat_id: int, text: str, *, thread: int | None = None, **kw: Any
    ) -> int | None:
        """Send a user-facing chat message, threaded into the originating forum
        Topic (``thread``) or the DM chat (``thread`` is None).

        Single choke point for every dispatcher-originated send (command
        confirmations, queue receipts, the soft-threshold notice, ``[OPTIONS:]``
        echoes) so a forum turn's side messages land in the user's Topic. A
        threadless supergroup General message is denied at the gate, so no served
        send ever lands in the supergroup's General chat. ``answer_callback`` is
        intentionally NOT routed here -- it is a callback ack, not a chat send.
        """
        assert self.client is not None
        return await self.client.send_message(chat_id, text, message_thread_id=thread, **kw)

    def _session_key(self, route: tuple[str, str]) -> str:
        slot, comp = route
        gen = self._conv.current_gen(route)
        return build_dm_session_key(
            "telegram",
            self._resolve_agent(),
            comp,
            gen=gen,
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=slot,
        )

    def _seed_gen(self, route: tuple[str, str]) -> int:
        slot, comp = route
        return seed_generation(
            self.sessions,
            channel="telegram",
            agent=self._resolve_agent(),
            user_id=comp,
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=slot,
        )

    def _origin_mirror_link(self, route: tuple[str, str], chat_id: int) -> ChannelLink:
        """The mirror location for the chat a conversation is being read in.

        One definition shared by the automatic bind, ``/link`` and ``/unlink``:
        an unlink matches an occupied location by VALUE, so a second spelling of
        "this chat" would let the release miss the binding the bind wrote.

        Carries the forum Topic so dashboard-mirrored replies for a forum-linked
        session thread back into the SAME Topic (via
        ``_deliver_cross_surface_reply``'s ``thread_id=link.thread_id``), not the
        supergroup General. ``None`` only for a DM — an authorized forum turn
        always carries a Topic, General being denied at the gate.
        """
        topic = self._route_thread(route)
        return ChannelLink(
            "telegram",
            channel_id=str(chat_id),
            thread_id=(str(topic) if topic is not None else None),
        )

    def _bind_origin_mirror(
        self, session_key: str, route: tuple[str, str], chat_id: int
    ) -> None:
        """Mirror this conversation's dashboard tab back to Telegram, unasked.

        The rule, the re-assert and the opt-out live in
        :func:`~kiro_crew.messaging.link.bind_origin_mirror`, shared with the
        Discord dispatcher; this only supplies Telegram's spelling of "this
        conversation".

        Synchronous and called ON the loop, like every other session-map
        mutation. Interleaving is ordered by ``session_map._MAP_LOCK``, not by the
        loop; what keeps the call here is that the write is BOUNDED — one
        whole-map rewrite, on a conversation's first turn only.
        """
        bind_origin_mirror(
            self.sessions,
            key=session_key,
            location=self._origin_mirror_link(route, chat_id),
        )

    async def _handle_link(self, route: tuple[str, str], chat_id: int) -> None:
        """Re-enable mirroring of this conversation's dashboard tab back here.

        Mirroring is automatic (see :meth:`_bind_origin_mirror`), so this is the
        withdrawal of a previous ``/unlink`` rather than the only way to turn it
        on. Clearing the opt-out is the load-bearing half: rebinding without it
        would be undone by the next automatic bind check.
        """
        assert self.client is not None
        key = self._session_key(route)
        # One write for the whole sequence: each of these mutations would
        # otherwise rewrite the entire session map, stalling the loop three times
        # for what is one user-visible action.
        with self.sessions.batched_save():
            self.sessions.set_mirror_opt_out(key, False)
            self.sessions.set_mirror_link(
                key,
                self._origin_mirror_link(route, chat_id),
                reason=UNBIND_REASON_ORIGIN_REBIND,
            )
            # Drop any pre-unification row so a stale binding cannot outlive the
            # rebind (reads prefer the channel key, but a leftover row would still
            # answer a clear).
            self.sessions.clear_mirror_link(
                legacy_dashboard_mirror_key(key), reason=UNBIND_REASON_ORIGIN_REBIND
            )
        await self._reply(
            chat_id,
            "✅ Linked. Replies from the dashboard for this conversation will "
            "also show up here. Send /unlink to stop.",
            thread=self._route_thread(route),
        )

    async def _handle_unlink(self, route: tuple[str, str], chat_id: int) -> None:
        assert self.client is not None
        key = self._session_key(route)
        # Persist the refusal BEFORE releasing: mirroring is re-asserted on every
        # inbound turn, so a release alone would be undone by the user's next
        # message. Batched with the release so the pair is one whole-map write
        # instead of four. No dashboard nudge here: a swept slot's link chip is
        # refreshed by the periodic channel_slot_reconciler push.
        with self.sessions.batched_save():
            self.sessions.set_mirror_opt_out(key, True)
            reply, _swept = release_conversation_location(
                self.sessions,
                key=key,
                location=self._origin_mirror_link(route, chat_id),
                channel="telegram",
            )
        await self._reply(chat_id, reply, thread=self._route_thread(route))

    def _persist_turn(
        self,
        session_key: str,
        user_text: str,
        reply_text: str,
        is_new: bool,
        agent: str | None = None,
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart)."""
        if self.conv_log is None:
            return
        self.conv_log.append(session_key, "user", user_text, agent=agent)
        if reply_text:
            self.conv_log.append(session_key, "assistant", reply_text, agent=agent)
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Telegram"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(
        self, chat_id: int, route: tuple[str, str], session_key: str, provider: Any
    ) -> None:
        """Soft-threshold context warning as a SEPARATE message (not persisted).

        Kept out of the streamed answer buffer so it is never persisted into the
        assistant turn and replayed next turn as though the assistant said it.
        The hard-compaction backstop is the backend autocompactor
        (``session.autocompact_pct``).
        """
        pct = self.sessions.check_context_usage(session_key, provider)
        soft_pct = self.cfg.telegram.soft_threshold_pct
        if pct >= soft_pct and not self._conv.is_awaiting(route):
            self._conv.set_awaiting(route)
            assert self.client is not None
            await self._reply(
                chat_id,
                "⚠️ Context is getting long. Use /compact to compress or " "/new to start fresh.",
                thread=self._route_thread(route),
            )

    async def _handle_compact(self, route: tuple[str, str], chat_id: int) -> None:
        """In-place ACP ``/compact`` on the user's session (mirrors Slack).

        Holds the per-session semaphore for the WHOLE compaction. Each Telegram
        update is dispatched as its own task, so a bare ``locked()`` check
        followed by ``stream_command`` would race: a normal turn could take the
        semaphore in the window between the check and the stream, and the two
        would then interleave JSON-RPC on one stdio channel and corrupt session
        state. ``try_acquire()`` takes the semaphore atomically (or refuses if a
        turn is already in flight); the ``finally`` always releases it.
        """
        assert self.client is not None
        session_key = self._session_key(route)
        thread = self._route_thread(route)
        # Atomically take the turn semaphore, or refuse. Distinguish "busy" (a
        # turn is streaming) from "no session yet" for the user-facing note.
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self._reply(
                    chat_id,
                    "⏳ Still working on your last message — try /compact once it finishes.",
                    thread=thread,
                )
            else:
                await self._reply(chat_id, "No active session to compact.", thread=thread)
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self._reply(chat_id, "No active session to compact.", thread=thread)
                return

            status_id = await self._reply(chat_id, "🔄 Compacting context…", thread=thread)
            result_text: str | None = None
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
                    err = cr.get("summary", "")
                    result_text = (
                        f"❌ Compaction failed: {err}" if err else "❌ Compaction failed."
                    )
                else:
                    result_text = "⚠️ Compaction timed out."
            except Exception:
                logger.warning("Telegram /compact failed for %s", session_key, exc_info=True)
                result_text = "❌ Compaction failed unexpectedly."
                # Drop the wedged native conversation, NOT the session's channel
                # identity: the map entry carries the mirror binding, so a full
                # ``destroy`` would silently unlink a mirrored conversation.
                # Housekeeping never unlinks (see ``SessionMap.prune`` and
                # ``SessionManager._recycle_held``).
                try:
                    await self.sessions.discard_conversation(session_key)
                except Exception:
                    logger.debug("Telegram: discard after compact failure failed", exc_info=True)

            final = result_text or "✅ Context compacted."
            if status_id:
                await self.client.edit_message(chat_id, status_id, final)
            else:
                await self._reply(chat_id, final, thread=thread)
        finally:
            # Always release the semaphore we took. No-op if the except path
            # already tore the session down (release() looks up by key).
            self.sessions.release(session_key)
