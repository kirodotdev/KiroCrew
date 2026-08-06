"""Full new-path dispatch: FeishuTransport -> TurnDriver -> FeishuRenderer.

``FeishuTransport.receive()`` authorises + normalises an inbound message and
hands the ``LarkInbound`` (carrying the ``message_id`` reply anchor) to
:meth:`FeishuDispatcher.handle_message`, which mirrors the WeCom transport
dispatch:

    command intercept (/new, /compact)
    -> construct FeishuRenderer
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft/hard threshold notice)
    -> renderer.close() + session release   # in finally

Feishu has no interactive buttons, so the dispatcher runs the driver
``decider``-less (deny-by-default for INTERACTIVE; ``auto`` still
auto-approves) and has no callback handler.  The security ``tool_gate`` and
the ``spawn_run`` auto-approve are wired inline off ``ctx_builder.hooks``
(channel-neutral) so this module never imports ``kiro_crew.slack``.

Dependency direction is ``feishu -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.feishu.renderer import FeishuRenderer
from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn, inbound_permitted
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import build_dm_session_key, seed_generation

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.feishu.client import LarkClient, LarkInbound
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Feishu sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default.  Mirrors the
# Slack / Telegram / WeCom paths' _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"


class FeishuDispatcher:
    """Coordinates Feishu turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime.  Holds per-user conversation state
    (generation counter for ``/new`` resets).  ``handle_message`` is wired as
    the transport's dispatch callback.  ``client`` is set by the gateway after
    construction to break the construction cycle.
    """

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        ctx_builder: "ContextBuilder",
        cfg: "KiroCrewConfig",
        agent: str | None = None,
        conv_log: "ConversationLog | None" = None,
        approval_mode: str = APPROVAL_INTERACTIVE,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        # Set by maybe_start_feishu after construction to avoid a cycle.
        self.client: "LarkClient | None" = None
        # Per-user conversation generation counter (incremented by /new).
        self._gen: dict[str, int] = {}

    # ── Turn dispatch (transport's dispatch callback) ─────────────────────

    async def handle_message(self, inbound: "LarkInbound") -> None:
        """Drive one authorised inbound Feishu message through TurnDriver."""
        assert self.client is not None, "FeishuDispatcher.client must be set"

        # Inbound channels-governance gate -- recheck per message so a host-
        # profile deny stops dispatch without requiring a restart.
        if not await inbound_permitted("feishu"):
            return

        open_id = inbound.open_id
        text = inbound.text
        logger.info("Feishu inbound from %s: %d chars", open_id, len(text or ""))

        # ── Command intercept (no LLM session needed) ──────────────────────
        cmd = text.strip().lower()
        if cmd in ("/new", "/reset"):
            self._gen[open_id] = self._gen.get(open_id, 0) + 1
            await self.client.send_reply(inbound.message_id, "✅ 已开始新对话")
            return
        if cmd == "/compact":
            await self._handle_compact(inbound)
            return

        # ── Mid-turn concurrency: steer or queue prompt ────────────────────
        session_key = self._session_key(open_id)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(inbound, session_key)
            return

        conversation_id = f"feishu:{open_id}"
        agent = self._resolve_agent()

        # Feishu has no interactive buttons -> no decider (deny-by-default for
        # INTERACTIVE; auto/trust still auto-approve via the driver ladder).
        renderer = FeishuRenderer(
            self.client,
            inbound.message_id,
            FEISHU_CAPABILITIES,
            session_key=session_key,
        )

        # Surface a newly-created Feishu session in the dashboard immediately
        # (don't wait for the ~30s reconciler).  Circular-import safe via
        # deferred local import.
        async def _surface_new_session() -> None:
            from kiro_crew.dashboard.channel_slots import (  # noqa: PLC0415
                surface_dispatcher_session,
            )

            await surface_dispatcher_session(self)

        await drive_turn(
            ChannelTurn(
                channel_type="feishu",
                session_key=session_key,
                conversation_id=conversation_id,
                agent=agent,
                user_text=text,
                renderer=renderer,
                approval_mode=self.approval_mode,
                decider=None,  # Feishu can't render approve/deny buttons
                persist=lambda user_text, reply, is_new: self._persist_turn(
                    session_key, user_text, reply, is_new
                ),
                notice=lambda sk, provider: self._maybe_notice(
                    inbound, sk, provider
                ),
                audit_caller=f"feishu:{open_id}",
                after_persist=_surface_new_session,
            ),
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _handle_busy(
        self, inbound: "LarkInbound", session_key: str
    ) -> None:
        """Mid-turn message: try to steer; else ask the user to resend."""
        assert self.client is not None
        # Re-check: the turn may have just finished between is_busy and here.
        if not self.sessions.is_busy(session_key):
            await self.handle_message(inbound)
            return
        provider = self.sessions.get_provider(session_key)
        has_active = getattr(provider, "has_active_turn", None)
        live = has_active is None or bool(has_active())
        steer = getattr(provider, "steer", None)
        steered = bool(
            live
            and getattr(provider, "supports_steer", False)
            and steer is not None
            and await steer(inbound.text)
        )
        if steered:
            await self.client.send_reply(inbound.message_id, "⏳ 已合并到当前回复")
        else:
            await self.client.send_reply(
                inbound.message_id, "⏳ 正在处理上一条，请稍后重发"
            )

    async def _handle_compact(self, inbound: "LarkInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        assert self.client is not None
        session_key = self._session_key(inbound.open_id)
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self.client.send_reply(
                    inbound.message_id, "⏳ 正在处理上一条消息，请稍后再试 /compact。"
                )
            else:
                await self.client.send_reply(
                    inbound.message_id, "ℹ️ 当前没有可压缩的对话。"
                )
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self.client.send_reply(
                    inbound.message_id, "ℹ️ 当前没有可压缩的对话。"
                )
                return
            await provider.compact()
            await provider.wait_for_compaction(timeout=120.0)
            await self.client.send_reply(inbound.message_id, "🗜️ 已压缩上下文。")
        except Exception:
            logger.exception("Feishu /compact failed for %s", session_key)
            await self.client.send_reply(inbound.message_id, "⚠️ 压缩失败，请重试。")
        finally:
            self.sessions.release(session_key)

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _session_key(self, open_id: str) -> str:
        gen = self._gen.get(open_id, 0)
        return build_dm_session_key(
            "feishu",
            self._resolve_agent(),
            open_id,
            gen=gen,
            dm_scope=self.cfg.messaging.dm_scope,
        )

    def _persist_turn(
        self,
        session_key: str,
        user_text: str,
        reply_text: str,
        is_new: bool,
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart)."""
        if self.conv_log is None:
            return
        self.conv_log.append(session_key, "user", user_text)
        if reply_text:
            self.conv_log.append(session_key, "assistant", reply_text)
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Feishu"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(
        self, inbound: "LarkInbound", session_key: str, provider: Any
    ) -> None:
        """Send a context-threshold notice as a separate reply post-turn."""
        assert self.client is not None
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.feishu.hard_threshold_pct:
            try:
                await provider.compact()
                await provider.wait_for_compaction(timeout=120.0)
                await self.client.send_reply(
                    inbound.message_id, "🗜️ 上下文接近上限，已自动压缩。"
                )
            except Exception:
                logger.debug(
                    "Feishu hard-threshold compaction failed", exc_info=True
                )
        elif pct >= self.cfg.feishu.soft_threshold_pct:
            await self.client.send_reply(
                inbound.message_id,
                "⚠️ 对话上下文已较长，回复 /compact 压缩，或 /new 开始新对话。",
            )
