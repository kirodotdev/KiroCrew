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

from kiro_crew.feishu.client import CHAT_GROUP
from kiro_crew.feishu.renderer import FeishuRenderer
from kiro_crew.feishu.transport import FEISHU_CAPABILITIES
from kiro_crew.history import mint_row_mid
from kiro_crew.messaging.commands import compact_unsupported_backend
from kiro_crew.messaging.conversation import ConversationState
from kiro_crew.messaging.dispatch import (
    ChannelTurn,
    build_directive_consumer,
    drive_turn,
    inbound_permitted,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    build_dm_session_key,
    seed_generation,
)
from kiro_crew.messaging.pre_turn import resolve_pre_turn
from kiro_crew.platform.context import redact_via_context
from kiro_crew.safety_override import safety_override

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
        # Conversation state keyed by ROUTE rather than by sender: a group turn
        # must never share a bucket with the sender's private DM (see _route).
        # ``seed_fn`` recovers the highest generation already on disk so /new
        # advances past a stale one instead of resurrecting it after a restart.
        self._conv: ConversationState[tuple[str, str]] = ConversationState(seed_fn=self._seed_gen)

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
        route = self._route(inbound)
        logger.info("Feishu inbound from %s: %d chars", open_id, len(text or ""))

        # ── Command intercept (no LLM session needed) ──────────────────────
        # Matched against the mention-FREE body, not `text`: a group message must
        # @-mention the bot, so `text` reads "@BotName /new" and a whole-string
        # match would never fire -- the command would be dispatched to the model
        # as a prompt instead. Falls back to `text` for a frame with no mentions.
        cmd = (inbound.command_text or text).strip().lower()
        if cmd in ("/new", "/reset"):
            self._conv.bump_gen(route)
            await self.client.send_reply(inbound.message_id, "✅ 已开始新对话")
            return
        if cmd == "/compact":
            # Releasing the latch here is what keeps the soft notice a
            # per-growth-cycle nudge rather than a once-ever one: the user
            # just complied, so the next time context climbs back into the
            # soft band they must be told again. Mirrors WeCom.
            self._conv.clear_awaiting(route)
            await self._handle_compact(inbound)
            return

        # Busy check, then rotation, then a re-derived key -- the ordering and
        # the reasons it matters live in messaging.pre_turn.
        session_key = await resolve_pre_turn(
            conv=self._conv,
            sessions=self.sessions,
            key=route,
            session_key_for=self._session_key,
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
            on_busy=lambda sk: self._handle_busy(inbound, sk),
        )
        if session_key is None:
            return  # folded into the running turn

        conversation_id = f"feishu:{route[1]}"
        agent = self._resolve_agent()

        # Feishu has no interactive buttons -> no decider (deny-by-default for
        # INTERACTIVE; auto/trust still auto-approve via the driver ladder).
        renderer = FeishuRenderer(
            self.client,
            inbound.message_id,
            FEISHU_CAPABILITIES,
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
                # Session-directive consumer: monitor_start /
                # autonudge_stop / ... return a marker TurnDriver decodes;
                # apply it against THIS turn's session key. Without it the
                # driver leaves the marker inert while the tool still
                # reports success to the model -- a silent no-op is worse
                # than a refusal. Dashboard-only directives stay refused
                # for a channel turn (slot=None, fail-closed).
                directive_consumer=build_directive_consumer(
                    session_key=session_key, sessions=self.sessions, dispatcher=self
                ),
                conversation_id=conversation_id,
                agent=agent,
                user_text=text,
                renderer=renderer,
                approval_mode=self.approval_mode,
                decider=None,  # Feishu can't render approve/deny buttons
                # No buttons means no way to approve a tool in band, so without an
                # out-of-band grant the INTERACTIVE ladder denies every tool and the
                # agent can only talk. This is the SAME process-global grant the
                # dashboard toggle and Slack's `/kirocrew yolo` drive, so it needs no
                # Feishu command of its own and it still expires. Read per request,
                # not captured at boot, so arming it (or letting it lapse) takes
                # effect on the next tool rather than after a gateway restart. It
                # does NOT weaken the PreToolUse gate: TurnDriver runs the
                # sensitive-path keystone, the governance ceiling and the deny-list
                # ahead of this rung, so a hard deny still wins.
                auto_approve_session=lambda: safety_override().is_active(),
                # Private context is withheld whenever the SESSION is shared, not
                # merely when the sender is not the owner. ``_route`` already keys a
                # group on its ``chat_id``, so one session serves every member and a
                # group turn never resumes a DM -- but that only isolates the turn
                # HISTORY. Memory, lessons and skills come from the context builder,
                # and without this they would be assembled into a reply the whole
                # group reads. So a group turn is minimal for EVERYONE, including an
                # allow-listed sender: whatever the agent says in a group is visible
                # to the group anyway, and DMs are where context-rich work belongs.
                #
                # The ``not is_operator`` half of WhatsApp's rule does not transfer:
                # WhatsApp's transport IS the operator's own account, so it can tell
                # the operator from a peer. Feishu has a bot identity and authorises
                # against ``allowed_open_ids``, where every admitted DM sender is an
                # equally-trusted peer with their own ``open_id``-keyed session.
                minimal_context=inbound.chat_type == CHAT_GROUP,
                persist=lambda user_text, reply, is_new: self._persist_turn(
                    session_key, user_text, reply, is_new, agent
                ),
                notice=lambda sk, provider: self._maybe_notice(inbound, sk, provider),
                audit_caller=f"feishu:{open_id}",
                after_persist=_surface_new_session,
            ),
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
        )

    # ── Helpers ───────────────────────────────────────────────────────────

    async def _handle_busy(self, inbound: "LarkInbound", session_key: str) -> None:
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
            await self.client.send_reply(inbound.message_id, "⏳ 正在处理上一条，请稍后重发")

    async def _handle_compact(self, inbound: "LarkInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        assert self.client is not None
        session_key = self._session_key(self._route(inbound))
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self.client.send_reply(
                    inbound.message_id, "⏳ 正在处理上一条消息，请稍后再试 /compact。"
                )
            else:
                await self.client.send_reply(inbound.message_id, "ℹ️ 当前没有可压缩的对话。")
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self.client.send_reply(inbound.message_id, "ℹ️ 当前没有可压缩的对话。")
                return
            # Capability gate (#8156, mirroring the dashboard's #7800 gate): a
            # backend that cannot serve a manual /compact treats the prompt as
            # ordinary text and never answers, so dispatching would strand the
            # unbounded wait below. Informational (this surface speaks Chinese;
            # the wording translates ``compact_unsupported_reply``), never an
            # error.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("Feishu: manual /compact declined — %s compacts itself", unsupported)
                await self.client.send_reply(
                    inbound.message_id, "ℹ️ 当前后端会自动压缩上下文，无需手动 /compact。"
                )
                return
            await provider.compact()
            await provider.wait_for_compaction()
            await self.client.send_reply(inbound.message_id, "🗜️ 已压缩上下文。")
        except Exception:
            logger.exception("Feishu /compact failed for %s", session_key)
            await self.client.send_reply(inbound.message_id, "⚠️ 压缩失败，请重试。")
        finally:
            self.sessions.release(session_key)

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    @staticmethod
    def _route(inbound: "LarkInbound") -> tuple[str, str]:
        """The session namespace this message belongs to, as ``(slot, comp)``.

        A group chat gets its OWN bucket keyed by ``chat_id`` under the
        non-direct ``forum`` slot, so an allow-listed user writing in a group
        never resumes their private DM session — otherwise prior DM content
        would become model context for a reply every group member reads, and
        under ``dm_scope="unified"`` group traffic would collapse into the
        cross-channel DM bucket. A p2p message keeps the plain ``direct``
        bucket keyed by ``open_id``.
        """
        if inbound.chat_type == CHAT_GROUP and inbound.chat_id:
            return CHAT_TYPE_FORUM, inbound.chat_id
        return CHAT_TYPE_DIRECT, inbound.open_id

    def _seed_gen(self, route: tuple[str, str]) -> int:
        """The highest generation already persisted for *route*'s bucket.

        Injected into :class:`ConversationState` so the in-memory counter is
        restart-safe: without it a restart would send ``/new`` back to
        generation 0 and silently resurrect the conversation the user had
        already discarded.
        """
        slot, comp = route
        return seed_generation(
            self.sessions,
            channel="feishu",
            agent=self._resolve_agent(),
            user_id=comp,
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=slot,
        )

    def _session_key(self, route: tuple[str, str]) -> str:
        slot, comp = route
        return build_dm_session_key(
            "feishu",
            self._resolve_agent(),
            comp,
            gen=self._conv.current_gen(route),
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=slot,
        )

    def _persist_turn(
        self,
        session_key: str,
        user_text: str,
        reply_text: str,
        is_new: bool,
        agent: str | None = None,
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart).

        ``agent`` is the RESOLVED agent for this turn, not the configured
        default: without it a custom-agent conversation persists with no agent
        metadata and the dashboard attributes it to the default agent.
        """
        if self.conv_log is None:
            return
        # This row is an EGRESS: persisted, then served to dashboard readers.
        user_text = redact_via_context(user_text)
        self.conv_log.append(session_key, "user", user_text, agent=agent, mid=mint_row_mid())
        if reply_text:
            self.conv_log.append(
                session_key, "assistant", reply_text, agent=agent, mid=mint_row_mid()
            )
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Feishu"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(self, inbound: "LarkInbound", session_key: str, provider: Any) -> None:
        """Send a context-threshold notice as a separate reply post-turn.

        The soft notice is latched per route: it fires once while usage sits
        between the soft and hard thresholds, instead of repeating on every
        turn. Compaction clears the latch so a later climb back past the soft
        threshold notifies again. Latching per route keeps a group's notice
        independent of the sender's DM.
        """
        assert self.client is not None
        route = self._route(inbound)
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.feishu.soft_threshold_pct:
            # Capability gate (#8156): no forced compaction to run and the
            # soft nudge's /compact advice cannot work — the backend compacts
            # on its own as context fills.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("Feishu: context notice skipped — %s compacts itself", unsupported)
                return
        if pct >= self.cfg.feishu.hard_threshold_pct:
            self._conv.clear_awaiting(route)
            try:
                await provider.compact()
                await provider.wait_for_compaction()
                await self.client.send_reply(inbound.message_id, "🗜️ 上下文接近上限，已自动压缩。")
            except Exception:
                logger.debug("Feishu hard-threshold compaction failed", exc_info=True)
        elif pct >= self.cfg.feishu.soft_threshold_pct and not self._conv.is_awaiting(route):
            # Latch before awaiting the send so a turn arriving while this
            # reply is in flight does not emit a duplicate notice.
            self._conv.set_awaiting(route)
            await self.client.send_reply(
                inbound.message_id,
                "⚠️ 对话上下文已较长，回复 /compact 压缩，或 /new 开始新对话。",
            )
