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

WeCom has no interactive buttons and no callback handler, so the dispatcher
runs the driver ``decider``-less. That alone would leave the INTERACTIVE ladder
denying every tool, so ``ChannelTurn.auto_approve_session`` carries the
process-global safety-override grant (``auto``/``trust`` modes still
auto-approve on their own). The security ``tool_gate`` and the ``spawn_run``
auto-approve are wired by the shared pipeline off ``ctx_builder.hooks``
(channel-neutral) so this module never imports ``kiro_crew.slack``.

Dependency direction is ``wecom -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.history import mint_row_mid
from kiro_crew.messaging.attachments import append_attachment_context
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.commands import compact_unsupported_backend
from kiro_crew.messaging.dispatch import (
    ChannelTurn,
    build_directive_consumer,
    drive_turn,
    inbound_permitted,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import (
    DM_SCOPE_UNIFIED,
    UNBIND_REASON_ORIGIN_REBIND,
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    channel_namespace_of,
    legacy_dashboard_mirror_key,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.platform.context import redact_via_context
from kiro_crew.safety_override import safety_override
from kiro_crew.wecom.attachments import process_wecom_attachments
from kiro_crew.wecom.commands import (
    ConversationState,
    build_help_text,
    build_override_usage,
    is_bare_mid_turn_override,
    parse_command,
    parse_mid_turn_override,
)
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
        # Inbound channels-governance gate (off-loop) — recheck per message so a
        # host-profile deny added after connect stops dispatch without a restart
        # (the startup gate only blocks CONNECTING). Silently drop on deny.
        if not await inbound_permitted("wecom"):
            return
        userid = inbound.userid
        text = inbound.text
        logger.info("WeCom inbound from %s: %d chars", userid, len(text or ""))

        # ── Command intercept (no LLM session needed) ──
        # An attachment makes this message CONTENT, never a command. Every branch
        # below RETURNS without ingesting, and a WeCom media URL lives ~5 minutes,
        # so a photo captioned "/new" would reset the conversation and the picture
        # would simply never arrive — no error, no mention of it, and by the time
        # the user resent it the URL was dead. The rule is therefore about the early
        # return, not about parsing: ``parse_mid_turn_override`` below stays live
        # because it only strips a prefix and the media still reaches ``_ingest_media``
        # on the same path. Slack draws this line the same way (``and not files`` on
        # its stop keyword). A command in its own message is unaffected.
        has_media = bool(inbound.attachments)
        cmd = None if has_media else parse_command(text)
        if cmd == "new":
            self._conv.bump_gen(userid)
            await self.client.say(inbound, "✅ 已开始新对话")
            return
        if cmd == "compact":
            self._conv.clear_awaiting(userid)
            await self._handle_compact(inbound)
            return
        if cmd == "help":
            await self.client.say(inbound, build_help_text())
            return
        if cmd == "stop":
            await self._handle_stop(inbound)
            return
        if cmd == "link":
            await self._handle_link(inbound)
            return
        if cmd == "unlink":
            await self._handle_unlink(inbound)
            return

        if not has_media and is_bare_mid_turn_override(text):
            # Neither a command nor a complete override. Handing the literal
            # "/queue" to the model gets it ANSWERED as chat text, which reads to
            # the user exactly like the feature not existing.
            await self.client.say(inbound, build_override_usage())
            return

        # ── Mid-turn concurrency: check the CURRENT-generation key for an
        # in-flight turn BEFORE any idle/daily rotation (rotating first could
        # mint a new key and miss the running turn). WeCom replies are bound to
        # the inbound request, so a queued-then-drained reply can't be delivered
        # reliably later -- fold it into the running turn via steer.
        override, payload = parse_mid_turn_override(text)
        session_key = self._session_key(userid)
        if self.sessions.is_busy(session_key):
            # An attachment cannot ride the busy path: ``_session/steer`` carries
            # TEXT ONLY, so folding this message into the running turn would send
            # the caption and drop the picture with no sign it was ever there.
            # Cleared and refused explicitly, so the user knows to resend it.
            if inbound.attachments:
                inbound.attachments = []
                await self.client.say(
                    inbound, "⏳ 正在处理上一条消息，附件无法并入，请稍后重新发送。"
                )
                if not (text or "").strip():
                    return
            await self._handle_busy(inbound, session_key, override=override, payload=payload)
            return
        if override is not None:
            # No turn is running, so there is nothing to steer into or queue
            # behind: run the payload as an ordinary turn rather than answering
            # the directive itself.
            text = payload
            inbound.text = payload

        prompt_text, media_temp_paths = await self._ingest_media(inbound, text, userid)
        if prompt_text is None:
            return
        text = prompt_text

        self._conv.maybe_rotate(
            userid,
            time.time(),
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
        )
        session_key = self._session_key(userid)
        conversation_id = f"wecom:{userid}"
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
            chat_id=userid,
        )

        # The turn skeleton (acquire -> identity -> context -> TurnDriver ->
        # guarded post-turn -> finally close/release) lives once in
        # messaging.dispatch. Only the wecom-specific pieces are injected.
        # Immediately surface a newly-created channel session in the dashboard
        # (feature: don't wait for the ~30s reconciler). Circular import —
        # dashboard boot imports channel packages — so import lazily.
        async def _surface_new_session() -> None:
            from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

            await surface_dispatcher_session(self)

        try:
            # INSIDE the cleanup-protected block, because it AWAITS: the bind offloads
            # to a thread, so a gateway shutdown can cancel here — and outside the
            # `try` that cancellation skips the `finally` and leaves the user's
            # DECRYPTED attachments on disk. Ordering is unchanged: still before the
            # turn, so a mirrored reply has a binding by the time one exists.
            #
            # Mirroring back to this conversation is the default, re-asserted every
            # turn and withdrawn only by /unlink. Reachable only because the long
            # connection carries aibot_send_msg; a bind written without a delivery
            # path would be an inert row that promised a two-way link.
            await self._bind_origin_mirror(session_key, inbound)
            await drive_turn(
                ChannelTurn(
                    channel_type="wecom",
                    session_key=session_key,
                    # Session-directive consumer: monitor_start / autonudge_stop /
                    # ... return a marker TurnDriver decodes; apply it against THIS
                    # turn's session key (dashboard-only directives stay refused
                    # for channel sessions).
                    directive_consumer=build_directive_consumer(
                        session_key=session_key, sessions=self.sessions, dispatcher=self
                    ),
                    conversation_id=conversation_id,
                    agent=agent,
                    user_text=text,
                    renderer=renderer,
                    approval_mode=self.approval_mode,
                    decider=None,  # WeCom can't render approve/deny buttons
                    # With no widget, INTERACTIVE is deny-by-default here, so the
                    # global grant is the channel's only way to let a tool through.
                    # Passed as a predicate, not a bool: a grant that lapses
                    # mid-turn must stop auto-approving the rest of that turn.
                    auto_approve_session=lambda: safety_override().is_active(),
                    persist=lambda user_text, reply, is_new: self._persist_turn(
                        session_key, user_text, reply, is_new, agent
                    ),
                    notice=lambda sk, provider: self._maybe_notice(inbound, sk, provider),
                    audit_caller=f"wecom:{userid}",
                    after_persist=_surface_new_session,
                ),
                sessions=self.sessions,
                ctx_builder=self.ctx_builder,
            )
        finally:
            if media_temp_paths:
                # The ingest pipeline hands ownership of the DECRYPTED temp
                # files to us. In a finally because a cancelled turn (gateway
                # shutdown) would otherwise leave the user's plaintext
                # attachment on disk. Off-loop: unlinking can block on a
                # network-backed TMPDIR.
                await asyncio.to_thread(cleanup_attachments, media_temp_paths)

    async def _handle_busy(
        self,
        inbound: Any,
        session_key: str,
        *,
        override: str | None = None,
        payload: str = "",
    ) -> None:
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
        # A ``/queue`` directive asks for the message to be answered AFTER this
        # turn. WeCom cannot honour that: a reply is addressed by the inbound
        # req_id, and holding the message means answering it against a request
        # that has since been answered. Say so instead of silently steering it,
        # which would merge text the user explicitly asked to keep separate.
        if override == "queue":
            await self.client.say(inbound, "ℹ️ 本渠道暂不支持排队，回复结束后请重新发送这条消息。")
            return
        steer_text = payload if override == "steer" and payload else inbound.text
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
            and await steer(steer_text)
        )
        if steered:
            await self.client.say(inbound, "⏳ 已合并到当前回复")
        else:
            await self.client.say(inbound, "⏳ 正在处理上一条，请稍后重发")

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
        # This row is an EGRESS: persisted, then served to dashboard readers.
        user_text = redact_via_context(user_text)
        self.conv_log.append(session_key, "user", user_text, agent=agent, mid=mint_row_mid())
        if reply_text:
            self.conv_log.append(
                session_key, "assistant", reply_text, agent=agent, mid=mint_row_mid()
            )
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "WeCom"
            self.conv_log.set_title(session_key, title)

    async def _notice_bubble(self, inbound: "WeComInbound", text: str) -> None:
        """Send a threshold notice as a SEPARATE bubble, on its OWN request id.

        Kept out of the answer buffer -- and thus out of the persisted turn -- so
        it is never replayed next turn as though the assistant said it.

        Deliberately a PUSH rather than ``client.say``. A notice is sent post-turn,
        between ``on_done`` and the renderer's ``close()``, and ``say`` opens a
        fresh stream on the SAME inbound ``req_id`` -- which is the only key a
        cmd-less ACK carries, so the client attributes an arriving ACK to the newest
        stream sent on that req_id. Sending the notice there retargets that
        attribution: a refusal ACK for the ANSWER's sealing frame would be recorded
        against the notice instead, leaving the answer looking accepted and
        defeating the recovery in ``_recover_unconfirmed_seal`` in exactly the case
        it exists for. A push mints its own req_id, so it cannot collide -- and its
        acceptance is confirmed rather than assumed, which for a notice the user
        must actually see is the better guarantee anyway. The conversation is warm
        by construction here: this runs on a turn the user just sent.
        """
        assert self.client is not None
        try:
            if not await self.client.send_proactive(inbound.userid, text):
                logger.warning("WeCom: the threshold notice was refused by the platform")
        except Exception:
            logger.debug("WeCom: notice bubble send failed", exc_info=True)

    async def _maybe_notice(self, inbound: "WeComInbound", session_key: str, provider: Any) -> None:
        """Context-length handling, surfaced as a separate bubble post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold forces
        a compaction so the window never overflows. The backend autocompactor is
        an additional safety net.
        """
        userid = inbound.userid
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.wecom.soft_threshold_pct:
            # Capability gate (#8156): no forced compaction to run and the
            # soft nudge's /compact advice cannot work — the backend compacts
            # on its own as context fills.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("WeCom: context notice skipped — %s compacts itself", unsupported)
                return
        if pct >= self.cfg.wecom.hard_threshold_pct:
            self._conv.clear_awaiting(userid)
            try:
                await provider.compact()
                await provider.wait_for_compaction()
                await self._notice_bubble(inbound, "🗜️ 上下文接近上限，已自动压缩。")
            except Exception:
                logger.debug("WeCom hard-threshold compaction failed", exc_info=True)
        elif pct >= self.cfg.wecom.soft_threshold_pct and not self._conv.is_awaiting(userid):
            self._conv.set_awaiting(userid)
            await self._notice_bubble(
                inbound,
                "⚠️ 对话上下文已较长，回复 /compact 压缩，或 /new 开始新对话。",
            )

    async def _ingest_media(
        self, inbound: "WeComInbound", text: str, userid: str
    ) -> tuple[str | None, list[str]]:
        """Download, decrypt and inline any inbound media.

        Returns ``(prompt_text, temp_paths)``; ``None`` text means the message was
        handled without running a turn. ``inbound.attachments`` is cleared either
        way, so a refused item can never be ingested twice on re-entry.

        The busy check is made AFTER the download as well as before, because a
        decrypt+download takes real time and a turn can start while it is in
        flight. Without the second check the already-written temp files would be
        inlined into a steer whose files this frame then deletes.
        """
        pairs = list(inbound.attachments or [])
        if not pairs:
            return text, []
        assert self.client is not None
        if self.sessions.is_busy(self._session_key(userid)):
            inbound.attachments = []
            await self.client.say(inbound, "⏳ 正在处理上一条消息，请稍后重新发送附件。")
            return (text if (text or "").strip() else None), []
        try:
            result = await process_wecom_attachments(pairs, proxy=self.client.proxy)
        except Exception:
            # One unreadable attachment must not lose the message. The user is
            # TOLD, because silence here is indistinguishable from the bot
            # ignoring them.
            logger.exception("WeCom: attachment ingestion failed for %s", userid)
            inbound.attachments = []
            note = "[附件无法读取]"
            return (f"{text}\n\n{note}" if text else note), []
        inbound.attachments = []
        temp_paths = list(result.temp_paths)
        if self.sessions.is_busy(self._session_key(userid)):
            if temp_paths:
                await asyncio.to_thread(cleanup_attachments, temp_paths)
            await self.client.say(inbound, "⏳ 正在处理上一条消息，请稍后重新发送附件。")
            return (text if (text or "").strip() else None), []
        for rejection in result.rejections:
            logger.info("WeCom: attachment refused for %s: %s", userid, rejection)
        return append_attachment_context(text, result), temp_paths

    # ── Dashboard mirror ───────────────────────────────────────────────────

    def _origin_mirror_link(self, inbound: "WeComInbound") -> ChannelLink:
        """The mirror location for the conversation this turn was read in.

        One definition shared by the automatic bind, ``/link`` and ``/unlink``: an
        unlink matches an occupied location by VALUE, so a second spelling of
        "this chat" would let the release miss the binding the bind wrote.

        WeCom has no thread or topic concept — the conversation IS the session —
        so ``thread_id`` is always None.
        """
        return ChannelLink("wecom", channel_id=inbound.userid, thread_id=None)

    async def _bind_origin_mirror(self, session_key: str, inbound: "WeComInbound") -> None:
        """Mirror this conversation's dashboard tab back to WeCom, unasked.

        The rule, the re-assert and the opt-out all live in the shared
        ``bind_origin_mirror``; this only supplies WeCom's spelling of "this
        conversation". Reachable at all only because ``aibot_send_msg`` exists —
        before it there was nothing to deliver a mirrored reply with.

        The shared bind is OFFLOADED because it consults
        ``SessionManager.mirror_opt_out``, and that read WRITES: a refusal stored
        under an older generation key is promoted to the bucket inside
        ``batched_save``, whose block exit rewrites the whole session map inline on
        the calling thread. On the loop that is a disk write on the turn path — this
        runs on EVERY inbound message — so a one-time migration would stall every
        other conversation and the WS heartbeat behind it.
        """
        location = self._origin_mirror_link(inbound)
        await asyncio.to_thread(
            bind_origin_mirror, self.sessions, key=session_key, location=location
        )
        # ALSO record it as the session's ORIGIN. The auto-compaction notice
        # resolves its delivery target from ``get_origin_link`` alone and returns
        # early when that is unset, so a WeCom user whose context the backend
        # autocompactor collapsed would watch their earlier turns become a summary
        # with no explanation -- the exact confusion that notice exists to prevent.
        # Origin and mirror are the same place for WeCom (the conversation IS the
        # session), and set_origin_link is in-memory and bounded, so this costs the
        # turn path nothing.
        # Guarded by the SAME key-based test ``bind_origin_mirror`` applies, and for
        # the same reason: under ``dm_scope="unified"`` every allowed user's DMs
        # collapse into one ``unified:{agent}`` bucket, so "the origin conversation"
        # has no single answer. Recording one user as the shared session's origin
        # would send unattended output — a subagent completion, a cron result — into
        # whichever user happened to write last. The test reads the KEY rather than
        # this channel's config, because the key is what the binding hangs off.
        setter = getattr(self.sessions, "set_origin_link", None)
        if setter is not None and channel_namespace_of(session_key) != DM_SCOPE_UNIFIED:
            setter(session_key, location)

    async def _handle_link(self, inbound: "WeComInbound") -> None:
        """Re-enable mirroring of this conversation's dashboard tab back here.

        Mirroring is automatic, so this is the withdrawal of a previous
        ``/unlink`` rather than the only way to turn it on. Clearing the opt-out is
        the load-bearing half: rebinding without it would be undone by the next
        automatic bind.
        """
        assert self.client is not None
        key = self._session_key(inbound.userid)
        # ``set_mirror_opt_out`` opens ``batched_save`` INTERNALLY (it writes two
        # flags atomically), and a batch block's exit writes the whole map inline on
        # whatever thread left the block — so calling it from the loop stalls every
        # gateway task for a disk write. Offloaded, the write lands on the worker
        # thread instead: ``SessionMap._save`` takes its "no running loop" branch
        # there and writes inline, which is what a worker thread is for.
        # The two calls below need no such treatment: they reach ``_save`` directly,
        # whose loop-aware branch marks the map dirty and schedules ONE debounced
        # flush that itself writes in a worker thread. Wrapping THOSE in a batch
        # would reintroduce the inline write this line exists to avoid.
        await asyncio.to_thread(self.sessions.set_mirror_opt_out, key, False)
        self.sessions.set_mirror_link(
            key,
            self._origin_mirror_link(inbound),
            reason=UNBIND_REASON_ORIGIN_REBIND,
        )
        # Drop any pre-unification row so a stale binding cannot outlive the rebind.
        self.sessions.clear_mirror_link(
            legacy_dashboard_mirror_key(key), reason=UNBIND_REASON_ORIGIN_REBIND
        )
        await self.client.say(
            inbound, "✅ 已连接。dashboard 上这个会话的回复也会同步到这里。发送 /unlink 可停止。"
        )

    async def _handle_unlink(self, inbound: "WeComInbound") -> None:
        assert self.client is not None
        key = self._session_key(inbound.userid)
        # Persist the refusal BEFORE releasing: mirroring is re-asserted on every
        # inbound turn, so a release alone would be undone by the next message.
        # Both offloaded, for the reason spelled out in _handle_link: this one
        # batches internally, and ``release_conversation_location`` opens a batch of
        # its own to free the location atomically. Ordering survives the offload
        # because each ``to_thread`` is awaited before the next begins.
        await asyncio.to_thread(self.sessions.set_mirror_opt_out, key, True)
        reply, _swept = await asyncio.to_thread(
            release_conversation_location,
            self.sessions,
            key=key,
            location=self._origin_mirror_link(inbound),
            channel="wecom",
        )
        await self.client.say(inbound, reply)

    async def _handle_stop(self, inbound: "WeComInbound") -> None:
        """Hard cancel: abort the reply that is running.

        The cancel is COOPERATIVE — an ACP ``session/cancel`` notification with no
        ack wait, so the acknowledgement to the user is immediate and the turn
        stops at its next safe point. It is fire-and-forget for the same reason the
        shared contract gives: on a shared runtime this cannot force-kill a
        co-tenant process, and waiting would make ``/stop`` feel broken.

        WeCom holds no queue (see ``_handle_busy``), so there is nothing else to
        clear — the running turn is the whole of what ``/stop`` owns here.
        """
        assert self.client is not None
        session_key = self._session_key(inbound.userid)
        if not self.sessions.is_busy(session_key):
            await self.client.say(inbound, "ℹ️ 当前没有正在生成的回复。")
            return
        provider = self.sessions.get_provider(session_key)
        cancel = getattr(provider, "cancel", None)
        if cancel is None:
            await self.client.say(inbound, "⚠️ 当前会话不支持停止，请稍后重试。")
            return
        try:
            await cancel(wait_ack_timeout=0)
        except Exception:
            logger.warning("WeCom /stop: cancel failed for %s", session_key, exc_info=True)
            await self.client.say(inbound, "⚠️ 停止失败，请稍后重试。")
            return
        await self.client.say(inbound, "🛑 已停止本次回复。")

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
                await self.client.say(inbound, "⏳ 正在处理上一条消息，请稍后再试 /compact。")
            else:
                await self.client.say(inbound, "ℹ️ 当前没有可压缩的对话。")
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self.client.say(inbound, "ℹ️ 当前没有可压缩的对话。")
                return
            # Capability gate (#8156, mirroring the dashboard's #7800 gate): a
            # backend that cannot serve a manual /compact treats the prompt as
            # ordinary text and never answers, so dispatching would strand the
            # unbounded wait below. Informational (this surface speaks Chinese;
            # the wording translates ``compact_unsupported_reply``), never an
            # error.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("WeCom: manual /compact declined — %s compacts itself", unsupported)
                await self.client.say(inbound, "ℹ️ 当前后端会自动压缩上下文，无需手动 /compact。")
                return
            await provider.compact()
            await provider.wait_for_compaction()
            await self.client.say(inbound, "🗜️ 已压缩上下文。")
        except Exception:
            logger.exception("WeCom /compact failed for %s", session_key)
            await self.client.say(inbound, "⚠️ 压缩失败，请重试。")
        finally:
            self.sessions.release(session_key)
