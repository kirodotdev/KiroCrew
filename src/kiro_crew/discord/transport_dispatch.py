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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kiro_crew.acp.types import EVENT_COMPACTION_STATUS, EVENT_COMPLETE
from kiro_crew.discord.commands import (
    ConversationState,
    parse_command,
    parse_mid_turn_override,
)
from kiro_crew.discord.renderer import DiscordApprovalDecider, DiscordRenderer
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import (
    ChannelLink,
    build_dm_session_key,
    dashboard_mirror_key,
    seed_generation,
)
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.discord.client import DiscordClient, DiscordInteraction
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Discord sessions load kirocrew-core
# (spawn_run etc.) — mirrors the Slack/Telegram paths.
_DEFAULT_KIROCREW_AGENT = "kirocrew"

# Upper bound on how many queued messages collapse into a single combined turn.
_MAX_COLLAPSE = 50

_HELP_TEXT = """\
🦞 **KiroCrew — Discord**

Commands:
`!new` — Start a fresh conversation
`!compact` — Compress context (when it gets long)
`!link` — Mirror this conversation's dashboard tab here
`!unlink` — Stop mirroring
`!stop` — Stop the current reply and clear the queue
`!help` — Show this message

While a reply is running, prefix a message to control it:
`!queue <msg>` — answer it after the current turn
`!steer <msg>` — fold it into the running turn now

Just send a message to chat. Replies stream in real-time.
"""


def _short(text: str, limit: int = 40) -> str:
    """Collapse whitespace and truncate for compact receipt display."""
    collapsed = " ".join(text.split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


_RECEIPT_MAX_ITEMS = 5  # verbatim items in a receipt before "…and N more"
# Instant, no-extra-bubble acknowledgement that a mid-turn steer was accepted
# and folded into the running turn.
_STEER_ACK_EMOJI = "🫡"


def _receipt_text(
    texts: list[str],
    *,
    answering: bool = False,
    cancelled: bool = False,
) -> str:
    """Render the single collapsing receipt for ``texts`` (order preserved)."""
    count = len(texts)
    items = " · ".join(f"“{_short(t)}”" for t in texts[:_RECEIPT_MAX_ITEMS])
    if count > _RECEIPT_MAX_ITEMS:
        items += f" · …and {count - _RECEIPT_MAX_ITEMS} more"
    if cancelled:
        return f"🛑 Cancelled ({count}): {items}"
    if answering:
        return f"▶️ Now answering ({count}): {items}"
    return f"⏳ Queued ({count}): {items}"


@dataclass
class _QueueReceipt:
    """The single, in-place receipt bubble tracking messages queued mid-turn."""

    msg_id: str
    texts: list[str]


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
        # session_key -> the single in-place "queued" receipt bubble.
        self._queue_receipts: dict[str, _QueueReceipt] = {}
        # Serializes receipt bookkeeping against the end-of-turn drain.
        self._receipt_lock = asyncio.Lock()
        # session_key -> the running turn's renderer (for steer chips).
        self._active_renderers: dict[str, DiscordRenderer] = {}

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
        # Inbound channels-governance gate (off-loop). The startup gate only stops
        # a transport from CONNECTING; a host-profile deny added after it connected
        # would otherwise keep dispatching inbound messages until restart. Recheck
        # per message so a runtime deny takes effect immediately — silently drop
        # (no reply) on deny, matching how an unauthorized user is ignored.
        if not await channel_inbound_permitted("discord"):
            logger.info("discord inbound dropped: denied by channels governance policy")
            return
        user_id = msg.user_id
        channel_id = msg.conversation_id
        thread_id = msg.thread_id or ""
        scope_id = self._scope_id(user_id, thread_id)
        text = msg.text

        # Per-message mid-turn override (!queue/!steer) — see the Telegram
        # dispatcher for the full precedence rationale.
        override_mode = None
        if interpret_commands and parse_command(text) is None:
            override_mode, text = parse_mid_turn_override(text)

        # ── Command intercept (no LLM session needed) ──
        cmd = parse_command(text) if interpret_commands and override_mode is None else None
        if cmd == "new":
            self._conv.bump_gen(scope_id)
            await self.client.send_message(channel_id, "✅ New conversation started.")
            return
        if cmd == "compact":
            self._conv.clear_awaiting(scope_id)
            await self._handle_compact(user_id, channel_id, thread_id)
            return
        if cmd == "link":
            await self._handle_link(user_id, channel_id, thread_id)
            return
        if cmd == "unlink":
            await self._handle_unlink(user_id, channel_id, thread_id)
            return
        if cmd == "help":
            await self.client.send_message(channel_id, _HELP_TEXT)
            return
        if cmd == "stop":
            await self._handle_stop(user_id, channel_id, thread_id)
            return

        # ── Mid-turn concurrency: check the CURRENT-generation key BEFORE any
        # idle/daily rotation (see the Telegram dispatcher's rationale). ──
        session_key = self._session_key(user_id, thread_id)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(session_key, msg, text, override_mode)
            return

        self._conv.maybe_rotate(
            scope_id,
            time.time(),
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
        )
        session_key = self._session_key(user_id, thread_id)
        chan_id = f"discord:{channel_id}" if thread_id else f"discord:{user_id}"
        agent = self._resolve_agent()

        decider = (
            DiscordApprovalDecider(session_key=session_key)
            if self.approval_mode == APPROVAL_INTERACTIVE
            else None
        )
        renderer = DiscordRenderer(
            self.client, channel_id, DISCORD_CAPABILITIES, session_key=session_key
        )
        self._active_renderers[session_key] = renderer

        # Everything acquire-dependent runs INSIDE the try so the finally
        # always finalizes the renderer; release() is gated on _acquired.
        # Mirrors telegram/transport_dispatch.py.
        _acquired = False
        try:
            await renderer.on_turn_start()
            provider, is_new, resumed = await self.sessions.get_or_create(
                session_key, agent=agent, channel_id=chan_id
            )
            _acquired = True
            if is_new:
                await self.sessions.set_channel(session_key, chan_id)
            # Publish this turn's session identity so managed MCP tools resolve
            # X-Session-Key; one shared writer lives in messaging.identity. (#232)
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
                renderer,
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
                await asyncio.to_thread(self._persist_turn, session_key, text, accumulated, is_new)
            except Exception:
                logger.warning(
                    "Discord: persist_turn failed session=%s",
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
                await renderer.close()
            except Exception:
                logger.warning(
                    "Discord: renderer.close failed session=%s",
                    session_key,
                    exc_info=True,
                )
            self._active_renderers.pop(session_key, None)
            if _acquired:
                self.sessions.release(session_key)

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
        if mode != "queue":
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
        # enqueue + receipt under _receipt_lock — see the Telegram dispatcher.
        if not await self._enqueue_with_receipt(session_key, channel_id, text):
            await self.handle_message(msg)

    async def _drain_queue(
        self, session_key: str, user_id: str, channel_id: str, thread_id: str = ""
    ) -> None:
        """Collapse every message queued during the just-finished turn into ONE
        combined turn (order preserved). See the Telegram dispatcher for the
        lock/ordering rationale."""
        texts: list[str] = []
        remainder: list[tuple[str, str, dict]] = []
        async with self._receipt_lock:
            while True:
                item = self.sessions.dequeue(session_key)
                if item is None:
                    break
                if len(texts) < _MAX_COLLAPSE:
                    texts.append(item[1])
                else:
                    remainder.append(item)
            for _ts, rtext, _kw in remainder:
                self.sessions.enqueue(session_key, str(time.time()), rtext, force=True)
            if texts:
                await self._receipt_flip_locked(session_key, channel_id, texts, len(remainder))
        if not texts:
            return
        if remainder:
            logger.debug(
                "discord: drain hit collapse cap=%d for %s; %d message(s) "
                "deferred (in order) to the next turn",
                _MAX_COLLAPSE,
                session_key,
                len(remainder),
            )
        combined = "\n\n".join(texts)
        await self.handle_message(
            InboundMessage(
                channel_type="discord",
                user_id=user_id,
                conversation_id=channel_id,
                text=combined,
                thread_id=thread_id or None,
            ),
            drain=False,
            interpret_commands=False,
        )

    # ── Mid-turn queue receipt (single, in-place, persistent record) ───────

    async def _enqueue_with_receipt(self, session_key: str, channel_id: str, text: str) -> bool:
        """Atomically enqueue a mid-turn message and create/grow its collapsing
        receipt, under ``_receipt_lock``. Returns True if queued; False if the
        turn finished in the window (caller runs the message as a fresh turn)."""
        assert self.client is not None
        async with self._receipt_lock:
            if not self.sessions.enqueue(session_key, str(time.time()), text, force=False):
                return False
            receipt = self._queue_receipts.get(session_key)
            if receipt is None:
                msg_id = await self.client.send_message(channel_id, _receipt_text([text]))
                if msg_id is not None:
                    self._queue_receipts[session_key] = _QueueReceipt(msg_id=msg_id, texts=[text])
                return True
            receipt.texts.append(text)
            try:
                await self.client.edit_message(
                    channel_id, receipt.msg_id, _receipt_text(receipt.texts)
                )
            except Exception:
                logger.debug("discord: queue receipt grow failed", exc_info=True)
            return True

    async def _receipt_flip_locked(
        self,
        session_key: str,
        channel_id: str,
        answered: list[str],
        deferred: int = 0,
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record. Caller MUST
        hold ``_receipt_lock``."""
        assert self.client is not None
        receipt = self._queue_receipts.pop(session_key, None)
        if receipt is None:
            return
        body = _receipt_text(answered, answering=True)
        if deferred:
            body += f" · +{deferred} deferred"
        try:
            await self.client.edit_message(channel_id, receipt.msg_id, body)
        except Exception:
            logger.debug("discord: queue receipt flip failed", exc_info=True)

    async def _receipt_finish_cancelled_locked(self, session_key: str, channel_id: str) -> None:
        """Finalize the receipt to a "🛑 Cancelled" record, if present. Caller
        MUST hold ``_receipt_lock``."""
        assert self.client is not None
        receipt = self._queue_receipts.pop(session_key, None)
        if receipt is None:
            return
        try:
            await self.client.edit_message(
                channel_id, receipt.msg_id, _receipt_text(receipt.texts, cancelled=True)
            )
        except Exception:
            logger.debug("discord: queue receipt cancel-finalize failed", exc_info=True)

    async def _handle_stop(self, user_id: str, channel_id: str, thread_id: str = "") -> None:
        """Hard cancel: abort the in-flight turn and clear everything."""
        assert self.client is not None
        session_key = self._session_key(user_id, thread_id)
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
        async with self._receipt_lock:
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

        # Tool-approval decision: "a:<request_id>:<nonce>:<1|0>". The nonce is
        # validated by resolve_global — a stale button (reused request ID from
        # before a restart, or an earlier prompt) fails closed.
        if data.startswith("a:"):
            body = data[2:]
            head, _, flag = body.rpartition(":")
            rid, _, nonce = head.rpartition(":")
            approved = flag == "1"
            key = DiscordApprovalDecider.key(self._session_key(itx.user_id, thread_id), rid)
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

    async def _handle_link(self, user_id: str, channel_id: str, thread_id: str = "") -> None:
        """Mirror this conversation's dashboard tab back to Discord."""
        assert self.client is not None
        key = dashboard_mirror_key(self._session_key(user_id, thread_id))
        self.sessions.set_mirror_link(key, ChannelLink("discord", channel_id=channel_id))
        await self.client.send_message(
            channel_id,
            "✅ Linked. Replies from the dashboard for this conversation will "
            "also show up here. Send `!unlink` to stop.",
        )

    async def _handle_unlink(self, user_id: str, channel_id: str, thread_id: str = "") -> None:
        assert self.client is not None
        key = dashboard_mirror_key(self._session_key(user_id, thread_id))
        was_linked = self.sessions.clear_mirror_link(key)
        await self.client.send_message(
            channel_id,
            "✅ Unlinked." if was_linked else "This conversation wasn't linked.",
        )

    def _persist_turn(
        self, session_key: str, user_text: str, reply_text: str, is_new: bool
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart)."""
        if self.conv_log is None:
            return
        self.conv_log.append(session_key, "user", user_text)
        if reply_text:
            self.conv_log.append(session_key, "assistant", reply_text)
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Discord"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(
        self, channel_id: str, scope_id: str, session_key: str, provider: Any
    ) -> None:
        """Soft-threshold context warning as a SEPARATE message (not persisted)."""
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

    async def _handle_compact(self, user_id: str, channel_id: str, thread_id: str = "") -> None:
        """In-place ACP ``/compact`` on the conversation's session."""
        assert self.client is not None
        session_key = self._session_key(user_id, thread_id)
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

                async def _run() -> None:
                    nonlocal result_text
                    async for ev in provider.stream_command("/compact"):
                        if ev.kind == EVENT_COMPACTION_STATUS:
                            if ev.text == "completed":
                                summary = _safe(ev.title or "")
                                result_text = (
                                    f"✅ Compacted: {summary}"
                                    if summary
                                    else "✅ Context compacted."
                                )
                            elif ev.text == "failed":
                                result_text = (
                                    f"❌ Compaction failed: "
                                    f"{_safe(ev.title or '') or 'unknown error'}"
                                )
                        elif ev.kind == EVENT_COMPLETE:
                            break

                await asyncio.wait_for(_run(), timeout=120)
                if not result_text:
                    cr = await provider.wait_for_compaction(timeout=120.0)
                    if cr["type"] == "completed":
                        summary = _safe(cr.get("summary", ""))
                        result_text = (
                            f"✅ Compacted: {summary}" if summary else "✅ Context compacted."
                        )
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
                try:
                    await self.sessions.destroy(session_key)
                except Exception:
                    logger.debug(
                        "Discord: destroy after compact failure failed",
                        exc_info=True,
                    )

            final = result_text or "✅ Context compacted."
            if status_id:
                await self.client.edit_message(channel_id, status_id, final)
            else:
                await self.client.send_message(channel_id, final)
        finally:
            self.sessions.release(session_key)
