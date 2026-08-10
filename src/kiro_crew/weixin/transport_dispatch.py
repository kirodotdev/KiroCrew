"""Full new-path dispatch: WeixinTransport -> TurnDriver -> WeixinRenderer.

``WeixinTransport.receive()`` authorizes + normalizes an inbound iLink message
and hands the neutral ``InboundMessage`` to :meth:`WeixinDispatcher.handle_message`,
which mirrors the WeCom/Telegram transport dispatch:

    command intercept (/new, /compact)
    -> construct WeixinRenderer + on_turn_start (typing indicator on)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft/hard threshold notice)  # guarded
    -> renderer.close() + session release   # in finally

iLink has no interactive buttons, so the driver runs ``decider``-less
(deny-by-default for INTERACTIVE mode; ``auto``/``trust`` still work) and there
is no callback handler. The security ``tool_gate`` and the ``spawn_run``
auto-approve are wired inline off ``ctx_builder.hooks`` (channel-neutral) so this
module never imports ``kiro_crew.slack``.

Unlike WeCom, iLink CAN send proactively (a reply is not bound to the inbound
request), so a mid-turn message is queued via steer and, when no turn is live,
simply run as a fresh turn.

Dependency direction is ``weixin -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.attachments import append_attachment_context
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn, inbound_permitted
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import build_dm_session_key, seed_generation
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.weixin.attachments import process_weixin_attachments
from kiro_crew.weixin.commands import ConversationState, parse_command
from kiro_crew.weixin.transport import WEIXIN_CAPABILITIES
from kiro_crew.weixin.turn_renderer import WeixinRenderer

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.weixin.client import ContextTokenStore, TypingTicketCache, WeixinClient

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Weixin sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default. Mirrors the
# Slack / Telegram / WeCom paths.
_DEFAULT_KIROCREW_AGENT = "kirocrew"


class WeixinDispatcher:
    """Coordinates Weixin turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-user conversation state
    (generation counter + soft-threshold flag). ``handle_message`` is wired as
    the transport's dispatch callback. ``client`` is set by the gateway after
    construction.
    """

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        ctx_builder: "ContextBuilder",
        cfg: "KiroCrewConfig",
        account_id: str,
        ctx_store: "ContextTokenStore",
        typing_cache: "TypingTicketCache | None" = None,
        agent: str | None = None,
        conv_log: "ConversationLog | None" = None,
        approval_mode: str = APPROVAL_INTERACTIVE,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self.account_id = account_id
        self.ctx_store = ctx_store
        self.typing_cache = typing_cache
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        self.client: "WeixinClient | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(self, inbound: InboundMessage) -> None:
        """Drive one authorized inbound Weixin message through TurnDriver."""
        assert self.client is not None, "WeixinDispatcher.client must be set"
        # Inbound channels-governance gate (off-loop) — recheck per message so a
        # host-profile deny added after connect stops dispatch without a restart
        # (the startup gate only blocks CONNECTING). Silently drop on deny.
        if not await inbound_permitted("weixin"):
            return
        user_id = inbound.user_id
        text = inbound.text
        logger.info("weixin inbound from %s: %d chars", user_id, len(text or ""))

        # ── Command intercept (no LLM session needed) ──
        # A command is the WHOLE message (``parse_command`` matches only an exact
        # alias), so media riding along with one is not something the agent could
        # act on: the command path runs no turn, and downloading the object just
        # to discard it would spend a CDN round trip for nothing. It is still
        # said out loud rather than dropped in silence, which is the failure mode
        # this channel's media support exists to remove.
        cmd = parse_command(text)
        if cmd is not None:
            if inbound.attachments:
                await self._say(user_id, "📎 附件未读取：这条是命令消息，请把附件单独发送。")
            if cmd == "new":
                self._conv.bump_gen(user_id)
                await self._say(user_id, "✅ 已开始新对话")
                return
            self._conv.clear_awaiting(user_id)
            await self._handle_compact(user_id)
            return

        # ── Attachment ingestion (mirrors Telegram/Discord) ──
        # Ingestion produces temp files whose paths are inlined into the text,
        # and only the fresh-turn path (``session/prompt``) turns those paths
        # into image blocks. ``steer()`` sends raw text and is fire-and-forget,
        # so a mid-turn attachment would be cleaned up in this frame's
        # ``finally`` before the running turn ever read the steer -- handing the
        # model a path to a deleted file. So a live turn means no ingestion: the
        # sender is told to resend once the turn ends, and any accompanying text
        # still reaches the turn via steer.
        attachment_temp_paths: list[str] = []
        if inbound.attachments:
            ingested, attachment_temp_paths = await self._ingest_or_refuse(
                inbound, user_id, text
            )
            if ingested is None:
                return
            text = ingested
            inbound.text = text

        try:
            await self._drive(inbound, user_id, text)
        finally:
            if attachment_temp_paths:
                await asyncio.to_thread(cleanup_attachments, attachment_temp_paths)

    async def _ingest_or_refuse(
        self, inbound: InboundMessage, user_id: str, text: str
    ) -> tuple[str | None, list[str]]:
        """Ingest the message's attachments, or refuse them for a live turn.

        Returns ``(text, temp_paths)``, where ``text`` is ``None`` when there is
        nothing left to run -- a refused media-only message. ``inbound`` is
        mutated so a refused attachment cannot be ingested again on re-entry.

        The busy check is made twice on purpose. A CDN download takes real time,
        so a turn can start *while* it is in flight; without the second check the
        already-downloaded paths would be inlined into a steer whose files this
        frame then deletes -- the exact failure the first check exists to
        prevent. On the second check the downloads are discarded immediately
        rather than at the end of the frame, and only the original caption goes
        on. There is no suspension point between that check and ``_drive``'s own
        one, so the two cannot disagree.
        """
        if self.sessions.is_busy(self._session_key(user_id)):
            inbound.attachments = []
            await self._say_resend_after_turn(user_id)
            return (text if (text or "").strip() else None), []

        try:
            result = await process_weixin_attachments(inbound.attachments)
        except Exception:
            # One unreadable attachment must not lose the message. The user is
            # told, because silence here is the exact bug this replaced.
            logger.exception("weixin: attachment ingestion failed for %s", user_id)
            inbound.attachments = []
            return (
                f"{text}\n\n[Attachment could not be read]"
                if text
                else "[Attachment could not be read]"
            ), []

        inbound.attachments = []
        temp_paths = list(result.temp_paths)
        if self.sessions.is_busy(self._session_key(user_id)):
            if temp_paths:
                await asyncio.to_thread(cleanup_attachments, temp_paths)
            await self._say_resend_after_turn(user_id)
            return (text if (text or "").strip() else None), []

        return append_attachment_context(text, result), temp_paths

    async def _say_resend_after_turn(self, user_id: str) -> None:
        """Tell a mid-turn sender their attachment needs resending."""
        await self._say(
            user_id,
            "⏳ 上一条消息还在处理中，附件无法在这轮读取，请等回复结束后重新发送。",
        )

    async def _drive(self, inbound: InboundMessage, user_id: str, text: str) -> None:
        """Session acquisition + turn dispatch for one already-ingested message."""
        assert self.client is not None
        # ── Mid-turn concurrency: check the CURRENT-generation key for an
        # in-flight turn BEFORE any idle/daily rotation (rotating first could
        # mint a new key and miss the running turn).
        session_key = self._session_key(user_id)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(inbound, session_key)
            return

        self._conv.maybe_rotate(
            user_id,
            time.time(),
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
        )
        session_key = self._session_key(user_id)
        conversation_id = f"weixin:{user_id}"
        agent = self._resolve_agent()

        renderer = WeixinRenderer(
            self.client,
            user_id,
            WEIXIN_CAPABILITIES,
            ctx_store=self.ctx_store,
            account_id=self.account_id,
            typing_cache=self.typing_cache,
            session_key=session_key,
        )

        # The turn skeleton (acquire -> identity -> context -> TurnDriver ->
        # guarded post-turn -> finally close/release) lives once in
        # messaging.dispatch. Only the weixin-specific pieces are injected.
        await drive_turn(
            ChannelTurn(
                channel_type="weixin",
                session_key=session_key,
                conversation_id=conversation_id,
                agent=agent,
                user_text=text,
                renderer=renderer,
                approval_mode=self.approval_mode,
                decider=None,  # iLink can't render approve/deny buttons
                persist=lambda user_text, reply, is_new: self._persist_turn(
                    session_key, user_text, reply, is_new
                ),
                after_persist=self._surface_own_session,
                notice=lambda sk, provider: self._maybe_notice(user_id, sk, provider),
                audit_caller=f"weixin:{user_id}",
            ),
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
        )

    async def _handle_busy(self, inbound: InboundMessage, session_key: str) -> None:
        """Mid-turn message: fold into the running turn via steer.

        ``steer()`` returning True only means the session exists, not that a turn
        is active, so it can't detect the is_busy->finished race. Gate on
        ``has_active_turn`` (parity with Telegram/WeCom): if the turn already
        finished, run the message as a fresh turn (safe — is_busy is now False,
        so no re-entry loop).
        """
        assert self.client is not None
        if not self.sessions.is_busy(session_key):
            await self.handle_message(inbound)
            return
        provider = self.sessions.get_provider(session_key)
        steer = getattr(provider, "steer", None)
        has_active = getattr(provider, "has_active_turn", None)
        live = has_active is None or bool(has_active())
        steered = bool(
            live
            and getattr(provider, "supports_steer", False)
            and steer is not None
            and await steer(inbound.text)
        )
        if steered:
            await self._say(inbound.user_id, "⏳ 已合并到当前回复")
        else:
            await self._say(inbound.user_id, "⏳ 正在处理上一条，请稍后重发")

    # ── Helpers ────────────────────────────────────────────────────────────

    async def _say(self, user_id: str, text: str) -> None:
        """One-shot out-of-band message (command ack / notice)."""
        assert self.client is not None
        try:
            await self.client.send_message(
                to=user_id,
                text=text,
                context_token=self.ctx_store.get(self.account_id, user_id),
                client_id=uuid.uuid4().hex,
            )
        except Exception:
            logger.warning("weixin: out-of-band send failed", exc_info=True)

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _session_key(self, user_id: str) -> str:
        gen = self._conv.current_gen(user_id)
        return build_dm_session_key(
            "weixin",
            self._resolve_agent(),
            user_id,
            gen=gen,
            dm_scope=self.cfg.messaging.dm_scope,
        )

    def _seed_gen(self, user_id: str) -> int:
        return seed_generation(
            self.sessions,
            channel="weixin",
            agent=self._resolve_agent(),
            user_id=user_id,
            dm_scope=self.cfg.messaging.dm_scope,
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
            title = (user_text or "").strip().replace("\n", " ")[:40] or "WeChat"
            self.conv_log.set_title(session_key, title)

    async def _surface_own_session(self) -> None:
        # Circular import: dashboard boot imports channel packages.
        from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

        await surface_dispatcher_session(self)

    async def _maybe_notice(self, user_id: str, session_key: str, provider: Any) -> None:
        """Context-length handling, surfaced as a separate message post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold forces
        a compaction so the window never overflows. The backend autocompactor is
        an additional safety net.
        """
        pct = self.sessions.check_context_usage(session_key, provider)
        hard = getattr(self.cfg.weixin, "hard_threshold_pct", 95)
        soft = getattr(self.cfg.weixin, "soft_threshold_pct", 80)
        if pct >= hard:
            self._conv.clear_awaiting(user_id)
            try:
                await provider.compact()
                await provider.wait_for_compaction()
                await self._say(user_id, "🗜️ 上下文接近上限，已自动压缩。")
            except Exception:
                logger.debug("weixin hard-threshold compaction failed", exc_info=True)
        elif pct >= soft and not self._conv.is_awaiting(user_id):
            self._conv.set_awaiting(user_id)
            await self._say(user_id, "⚠️ 对话上下文已较长，回复 /compact 压缩，或 /new 开始新对话。")

    async def _handle_compact(self, user_id: str) -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        session_key = self._session_key(user_id)
        # Serialize compaction against the turn semaphore: compacting while a
        # turn is mutating the same session races the transcript.
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self._say(user_id, "⏳ 正在处理上一条消息，请稍后再试 /compact。")
            else:
                await self._say(user_id, "ℹ️ 当前没有可压缩的对话。")
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self._say(user_id, "ℹ️ 当前没有可压缩的对话。")
                return
            await provider.compact()
            await provider.wait_for_compaction()
            await self._say(user_id, "🗜️ 已压缩上下文。")
        except Exception:
            logger.exception("weixin /compact failed for %s", session_key)
            await self._say(user_id, "⚠️ 压缩失败，请重试。")
        finally:
            self.sessions.release(session_key)
