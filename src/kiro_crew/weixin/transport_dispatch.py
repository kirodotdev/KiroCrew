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

from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import build_dm_session_key, seed_generation
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.sel import sel
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
        if not await channel_inbound_permitted("weixin"):
            logger.info("weixin inbound dropped: denied by channels governance policy")
            return
        user_id = inbound.user_id
        text = inbound.text
        logger.info("weixin inbound from %s: %d chars", user_id, len(text or ""))

        # ── Command intercept (no LLM session needed) ──
        cmd = parse_command(text)
        if cmd == "new":
            self._conv.bump_gen(user_id)
            await self._say(user_id, "✅ 已开始新对话")
            return
        if cmd == "compact":
            self._conv.clear_awaiting(user_id)
            await self._handle_compact(user_id)
            return

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
        channel_id = f"weixin:{user_id}"
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

        # Everything acquire-dependent runs INSIDE the try so the finally always
        # finalizes the turn (renderer.close), even if get_or_create raises on a
        # cold-start failure. release() is gated on _acquired so we never release
        # a semaphore we didn't hold.
        _acquired = False
        try:
            # Typing indicator first (before the potentially slow cold start);
            # on_turn_start is idempotent so the driver's later call no-ops.
            await renderer.on_turn_start()
            provider, is_new, resumed = await self.sessions.get_or_create(
                session_key, agent=agent, channel_id=channel_id
            )
            _acquired = True
            if is_new:
                await self.sessions.set_channel(session_key, channel_id)
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
                renderer,
                approval_mode=self.approval_mode,
                decider=None,  # iLink can't render approve/deny buttons
                # Preserve the auto_approve_subagent_spawn hook for spawn_run
                # (replicated inline to avoid a weixin -> slack import).
                auto_approve_tool=lambda title: bool(
                    self.ctx_builder
                    and self.ctx_builder.hooks
                    and self.ctx_builder.hooks.auto_approve_subagent_spawn
                    and title == "spawn_run"
                ),
                tool_gate=_tool_gate,
            )
            accumulated = await driver.run(full_message)

            # ── Post-turn bookkeeping (each guarded so a failure here can't
            # fall through to the except and re-record the successful turn). ──
            self.sessions.record_success(session_key)
            try:
                await asyncio.to_thread(
                    self._persist_turn, session_key, text, accumulated, is_new
                )
            except Exception:
                logger.warning(
                    "weixin: persist_turn failed session=%s", session_key, exc_info=True
                )
            try:
                await self._maybe_notice(user_id, session_key, provider)
            except Exception:
                logger.warning(
                    "weixin: maybe_notice failed session=%s", session_key, exc_info=True
                )
            try:
                sel().log_api_access(
                    caller=f"weixin:{user_id}",
                    operation="transport_dispatch.handle",
                    outcome="success",
                    source="weixin",
                    resources=f"session={session_key}",
                )
            except Exception:
                logger.debug("weixin: success audit failed", exc_info=True)
        except Exception:
            logger.exception("weixin transport_dispatch: error handling message")
            if _acquired:
                await self.sessions.record_failure(session_key)
        finally:
            # Always finalize the turn, even if get_or_create raised before the
            # semaphore was held. Only release if we actually acquired it.
            await renderer.close()
            if _acquired:
                self.sessions.release(session_key)

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
                await provider.wait_for_compaction(timeout=120.0)
                await self._say(user_id, "🗜️ 上下文接近上限，已自动压缩。")
            except Exception:
                logger.debug("weixin hard-threshold compaction failed", exc_info=True)
        elif pct >= soft and not self._conv.is_awaiting(user_id):
            self._conv.set_awaiting(user_id)
            await self._say(
                user_id, "⚠️ 对话上下文已较长，回复 /compact 压缩，或 /new 开始新对话。"
            )

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
            await provider.wait_for_compaction(timeout=120.0)
            await self._say(user_id, "🗜️ 已压缩上下文。")
        except Exception:
            logger.exception("weixin /compact failed for %s", session_key)
            await self._say(user_id, "⚠️ 压缩失败，请重试。")
        finally:
            self.sessions.release(session_key)
