"""Full new-path dispatch: WeComTransport -> TurnDriver -> WeComRenderer.

``WeComTransport.receive()`` authorizes + normalizes an inbound WS frame and
hands the ``WeComInbound`` (carrying the WS routing keys ``req_id`` /
``response_url``) to :meth:`WeComDispatcher.handle_message`, which mirrors the
Slack/Telegram transport dispatch:

    command intercept (/new, /compact)
    -> construct WeComRenderer + on_turn_start (immediate "🤔 …" placeholder)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft/hard threshold notice)  # guarded
    -> renderer.close() + session release   # in finally

WeCom has no interactive buttons, so the dispatcher runs the driver
``decider``-less (deny-by-default for INTERACTIVE mode; ``auto``/``trust``
still work) and has no callback handler. The security ``tool_gate`` and the
``spawn_run`` auto-approve are wired inline off ``ctx_builder.hooks``
(channel-neutral) so this module never imports ``kiro_crew.slack``.

Dependency direction is ``wecom -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.messaging.link import build_dm_session_key, seed_generation
from kiro_crew.sel import sel
from kiro_crew.wecom.client import new_stream_id
from kiro_crew.wecom.commands import ConversationState, parse_command
from kiro_crew.wecom.renderer import WeComRenderer
from kiro_crew.wecom.transport import WECOM_CAPABILITIES

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.wecom.client import WeComClient, WeComInbound

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so WeCom sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the Slack /
# Telegram paths' _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"


class WeComDispatcher:
    """Coordinates WeCom turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-userid conversation state
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
        owner_id: str = "",
        agent: str | None = None,
        conv_log: "ConversationLog | None" = None,
        approval_mode: str = APPROVAL_INTERACTIVE,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self.owner_id = owner_id
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        self.client: "WeComClient | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(self, inbound: "WeComInbound") -> None:
        """Drive one authorized inbound WeCom message through TurnDriver."""
        assert self.client is not None, "WeComDispatcher.client must be set"
        userid = inbound.userid
        text = inbound.text
        logger.info("WeCom inbound from %s: %d chars", userid, len(text or ""))

        # ── Command intercept (no LLM session needed) ──
        cmd = parse_command(text)
        if cmd == "new":
            self._conv.bump_gen(userid)
            await self.client.send_reply(inbound.response_url, "✅ 已开始新对话")
            return
        if cmd == "compact":
            self._conv.clear_awaiting(userid)
            await self._handle_compact(inbound)
            return
        if cmd in ("link", "unlink"):
            # WeCom replies are bound to an inbound request (no proactive send),
            # so a dashboard->channel mirror can never be delivered here. Reject
            # explicitly rather than silently record an inert link.
            await self.client.send_reply(
                inbound.response_url,
                "ℹ️ 本渠道回复绑定在收到的消息上,不支持从 dashboard 主动推送,"
                "/link 暂不可用。",
            )
            return

        # ── Mid-turn concurrency: check the CURRENT-generation key for an
        # in-flight turn BEFORE any idle/daily rotation (rotating first could
        # mint a new key and miss the running turn). WeCom replies are bound to
        # the inbound request, so a queued-then-drained reply can't be delivered
        # reliably later -- fold it into the running turn via steer.
        session_key = self._session_key(userid)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(inbound, session_key)
            return

        self._conv.maybe_rotate(
            userid,
            time.time(),
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
        )
        session_key = self._session_key(userid)
        channel_id = f"wecom:{userid}"
        # Resolve the kiro-cli agent: an explicit override wins, else the
        # configured default, else the canonical "kirocrew" agent -- so the
        # session loads kirocrew-core (spawn_run) instead of kiro-cli's bare
        # built-in default. Mirrors slack/telegram transport_dispatch.
        agent = self._resolve_agent()

        # WeCom has no interactive buttons -> no decider (deny-by-default for
        # INTERACTIVE; auto/trust still auto-approve via the driver ladder).
        renderer = WeComRenderer(
            self.client,
            inbound.req_id,
            inbound.response_url,
            WECOM_CAPABILITIES,
            session_key=session_key,
        )

        # Everything acquire-dependent runs INSIDE the try so the finally always
        # finalizes the placeholder (renderer.close -> no perma-"🤔 …"), even if
        # get_or_create itself raises on a cold-start failure. release() is gated
        # on _acquired so we never release a semaphore we didn't hold. Mirrors
        # slack/telegram transport_dispatch.
        _acquired = False
        try:
            # Ack placeholder first (before the potentially slow cold-start);
            # on_turn_start is idempotent so the driver's later call no-ops.
            await renderer.on_turn_start()
            provider, is_new, resumed = await self.sessions.get_or_create(
                session_key, agent=agent, channel_id=channel_id
            )
            _acquired = True
            if is_new:
                await self.sessions.set_channel(session_key, channel_id)
            # Publish this turn's session identity so managed MCP tools resolve
            # X-Session-Key; one shared writer lives in messaging.identity. (#232)
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
                decider=None,  # WeCom can't render approve/deny buttons
                # Preserve the auto_approve_subagent_spawn hook for spawn_run
                # (replicated inline to avoid a wecom -> slack import).
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
                    "WeCom: persist_turn failed session=%s", session_key, exc_info=True
                )
            try:
                await self._maybe_notice(inbound, session_key, provider)
            except Exception:
                logger.warning(
                    "WeCom: maybe_notice failed session=%s", session_key, exc_info=True
                )
            try:
                sel().log_api_access(
                    caller=f"wecom:{userid}",
                    operation="transport_dispatch.handle",
                    outcome="success",
                    source="wecom",
                    resources=f"session={session_key}",
                )
            except Exception:
                logger.debug("WeCom: success audit failed", exc_info=True)
        except Exception:
            logger.exception("WeCom transport_dispatch: error handling message")
            if _acquired:
                await self.sessions.record_failure(session_key)
        finally:
            # Always finalize the placeholder (no perma-"🤔 …"), even if
            # get_or_create raised before the semaphore was held. Only release
            # the semaphore if we actually acquired it.
            await renderer.close()
            if _acquired:
                self.sessions.release(session_key)

    async def _handle_busy(self, inbound: Any, session_key: str) -> None:
        """Mid-turn message: fold into the running turn via steer.

        WeCom replies are bound to the inbound request, so a queued-then-drained
        reply can't be delivered reliably later -- WeCom steers rather than
        queueing (capability-driven, like the no-proactive-send gate).

        ``steer()`` returning True only means the session exists, not that a turn
        is active, so it can't detect the is_busy->finished race. Re-check
        ``is_busy`` (the semaphore, which tracks turn-active) instead: if the
        turn already finished, run the message as a fresh turn (safe -- is_busy
        is now False, so no re-entry loop); if a turn is still in flight but
        steer isn't possible yet (cold start, no session id), ask the user to
        resend rather than silently dropping the message.
        """
        assert self.client is not None
        if not self.sessions.is_busy(session_key):
            await self.handle_message(inbound)
            return
        provider = self.sessions.get_provider(session_key)
        steer = getattr(provider, "steer", None)
        # ``is_busy`` stays True through post-turn bookkeeping (all await
        # points), so it alone can't tell a live turn from one that just
        # finished. Gate steer on ``has_active_turn`` (parity with Telegram):
        # steering a prompt that already ended is silently swallowed and would
        # falsely acknowledge a merge. When no turn is live, fall through to the
        # resend prompt instead.
        has_active = getattr(provider, "has_active_turn", None)
        live = has_active is None or bool(has_active())
        steered = bool(
            live
            and getattr(provider, "supports_steer", False)
            and steer is not None
            and await steer(inbound.text)
        )
        if steered:
            await self.client.send_reply(inbound.response_url, "⏳ 已合并到当前回复")
        else:
            await self.client.send_reply(inbound.response_url, "⏳ 正在处理上一条,请稍后重发")

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _session_key(self, userid: str) -> str:
        gen = self._conv.current_gen(userid)
        return build_dm_session_key(
            "wecom",
            self._resolve_agent(),
            userid,
            gen=gen,
            dm_scope=self.cfg.messaging.dm_scope,
        )

    def _seed_gen(self, userid: str) -> int:
        return seed_generation(
            self.sessions,
            channel="wecom",
            agent=self._resolve_agent(),
            user_id=userid,
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
            title = (user_text or "").strip().replace("\n", " ")[:40] or "WeCom"
            self.conv_log.set_title(session_key, title)

    async def _notice_bubble(self, req_id: str, text: str) -> None:
        """Send a threshold notice as a SEPARATE WS bubble (fresh stream_id).

        Kept out of the answer buffer -- and thus out of the persisted turn --
        so it is never replayed next turn as though the assistant said it
        (WeCom binds a reply to the inbound message, so this is the out-of-band
        equivalent of the Telegram/Slack separate-message notice).
        """
        assert self.client is not None
        if not req_id:
            return
        try:
            await self.client.send_stream(req_id, new_stream_id(), text, finish=True)
        except Exception:
            logger.debug("WeCom: notice bubble send failed", exc_info=True)

    async def _maybe_notice(
        self, inbound: "WeComInbound", session_key: str, provider: Any
    ) -> None:
        """Context-length handling, surfaced as a separate bubble post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold forces
        a compaction so the window never overflows. The backend autocompactor is
        an additional safety net.
        """
        userid = inbound.userid
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.wecom.hard_threshold_pct:
            self._conv.clear_awaiting(userid)
            try:
                await provider.compact()
                await provider.wait_for_compaction(timeout=120.0)
                await self._notice_bubble(inbound.req_id, "🗜️ 上下文接近上限，已自动压缩。")
            except Exception:
                logger.debug("WeCom hard-threshold compaction failed", exc_info=True)
        elif pct >= self.cfg.wecom.soft_threshold_pct and not self._conv.is_awaiting(userid):
            self._conv.set_awaiting(userid)
            await self._notice_bubble(
                inbound.req_id,
                "⚠️ 对话上下文已较长，回复 /compact 压缩，或 /new 开始新对话。",
            )

    async def _handle_compact(self, inbound: "WeComInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        assert self.client is not None
        session_key = self._session_key(inbound.userid)
        # Serialize compaction against the turn semaphore: compacting while a
        # turn is mutating the same session races the transcript. Distinguish a
        # busy session (ask the user to retry) from an absent one (nothing to
        # compact), and always release what we acquired.
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self.client.send_reply(
                    inbound.response_url, "⏳ 正在处理上一条消息，请稍后再试 /compact。"
                )
            else:
                await self.client.send_reply(inbound.response_url, "ℹ️ 当前没有可压缩的对话。")
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self.client.send_reply(inbound.response_url, "ℹ️ 当前没有可压缩的对话。")
                return
            await provider.compact()
            await provider.wait_for_compaction(timeout=120.0)
            await self.client.send_reply(inbound.response_url, "🗜️ 已压缩上下文。")
        except Exception:
            logger.exception("WeCom /compact failed for %s", session_key)
            await self.client.send_reply(inbound.response_url, "⚠️ 压缩失败，请重试。")
        finally:
            self.sessions.release(session_key)
