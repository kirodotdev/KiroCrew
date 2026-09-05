"""Full new-path dispatch: IMessageTransport -> TurnDriver -> IMessageRenderer.

``IMessageTransport.receive()`` suppresses own messages, fails closed on group
chats, authorizes the handle, then hands the ``IMessageInbound`` (carrying the
chat selector) to :meth:`IMessageDispatcher.handle_message`, which mirrors the
Webex/WeCom transport dispatch:

    command intercept (/new, /compact, /help)
    -> construct IMessageRenderer + on_turn_start (read receipt + typing)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft/hard threshold notice)  # guarded
    -> renderer.close() + session release   # in finally

iMessage has no interactive buttons, so the dispatcher runs the driver
``decider``-less. That alone would leave the ``INTERACTIVE`` ladder denying
every tool, so ``ChannelTurn.auto_approve_session`` carries the process-global
safety-override grant (``auto``/``trust`` modes still auto-approve on their
own). Every message this module sends is plain text, because iMessage renders no
markup.

Dependency direction is ``imessage -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from kiro_crew.history import mint_row_mid
from kiro_crew.imessage.client import redact_handle
from kiro_crew.imessage.commands import HELP_TEXT, ConversationState, parse_command
from kiro_crew.imessage.renderer import IMessageRenderer
from kiro_crew.imessage.rpc import RpcError, RpcTransportError
from kiro_crew.imessage.transport import IMESSAGE_CAPABILITIES
from kiro_crew.messaging.commands import compact_unsupported_backend
from kiro_crew.messaging.dispatch import (
    ChannelTurn,
    build_directive_consumer,
    drive_turn,
    inbound_permitted,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import build_dm_session_key, seed_generation
from kiro_crew.messaging.pre_turn import resolve_pre_turn
from kiro_crew.platform.context import redact_via_context
from kiro_crew.safety_override import safety_override

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog
    from kiro_crew.imessage.client import IMessageClient, IMessageInbound
    from kiro_crew.session import SessionManager

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so iMessage sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the other
# channels' _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"


class IMessageDispatcher:
    """Coordinates iMessage turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-handle conversation state
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
        self.client: "IMessageClient | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)

    # -- Advisory delivery ---------------------------------------------------

    async def _notify(self, handle: str, text: str) -> None:
        """Deliver an advisory notice, best-effort.

        Command acknowledgements, help text and compaction notices are status
        chatter, not the answer, so a bridge failure while sending one must not
        abort the dispatch that produced it. ``client.send`` raises on a real
        delivery failure (it no longer collapses failure into an empty guid), so
        the tolerance lives here, at the call sites that genuinely want it,
        rather than inside the client where it would also hide a lost reply.
        """
        try:
            assert self.client is not None, "IMessageDispatcher.client must be set"
            await self.client.send(handle, text)
        except (RpcError, RpcTransportError) as exc:
            logger.warning(
                "imessage: advisory notice to %s not delivered: %s",
                redact_handle(handle),
                exc,
            )

    # -- Turn dispatch (transport's dispatch callback) -----------------------

    async def handle_message(self, inbound: "IMessageInbound") -> None:
        """Drive one authorized inbound iMessage through TurnDriver."""
        assert self.client is not None, "IMessageDispatcher.client must be set"
        # Inbound channels-governance gate (off-loop) -- recheck per message so
        # a host-profile deny added after connect stops dispatch without a
        # restart (the startup gate only blocks CONNECTING). Silently drop.
        if not await inbound_permitted("imessage"):
            return
        handle = inbound.handle
        text = inbound.text
        logger.info("iMessage inbound from %s: %d chars", redact_handle(handle), len(text or ""))

        # -- Command intercept (no LLM session needed) --
        cmd = parse_command(text)
        if cmd == "new":
            self._conv.bump_gen(handle)
            await self._notify(handle, "✅ Started a fresh conversation.")
            return
        if cmd == "compact":
            self._conv.clear_awaiting(handle)
            await self._handle_compact(inbound)
            return
        if cmd == "help":
            await self._notify(handle, HELP_TEXT)
            return

        # Busy check, then rotation, then a re-derived key -- the ordering and the
        # reasons it matters live in messaging.pre_turn.
        session_key = await resolve_pre_turn(
            conv=self._conv,
            sessions=self.sessions,
            key=handle,
            session_key_for=self._session_key,
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
            on_busy=lambda sk: self._handle_busy(inbound, sk),
        )
        if session_key is None:
            return  # folded into the running turn
        conversation_id = f"imessage:{handle}"
        agent = self._resolve_agent()

        renderer = IMessageRenderer(
            self.client,
            handle,
            IMESSAGE_CAPABILITIES,
            chat_selector=inbound.chat_selector,
            session_key=session_key,
        )

        # The turn skeleton (acquire -> identity -> context -> TurnDriver ->
        # guarded post-turn -> finally close/release) lives once in
        # messaging.dispatch. Only the iMessage-specific pieces are injected.
        # Immediately surface a newly-created channel session in the dashboard
        # rather than waiting for the reconciler. Circular import -- dashboard
        # boot imports channel packages -- so import lazily.
        async def _surface_new_session() -> None:
            from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

            await surface_dispatcher_session(self)

        await drive_turn(
            ChannelTurn(
                channel_type="imessage",
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
                decider=None,  # iMessage can't render approve/deny buttons
                # No buttons means no way to approve a tool in band, so without
                # an out-of-band grant the INTERACTIVE ladder denies every tool
                # and the agent can only talk. This is the SAME process-global
                # grant the dashboard toggle and Slack's `/kirocrew yolo` drive,
                # so it needs no iMessage command of its own and it still
                # expires. Read per request, not captured at boot, so arming it
                # (or letting it lapse) takes effect on the next tool rather than
                # after a gateway restart. It does NOT weaken the PreToolUse
                # gate: TurnDriver runs the sensitive-path keystone, the
                # governance ceiling and the deny-list ahead of this rung, so a
                # hard deny still wins. With no grant the predicate is False and
                # every tool still needs an approval this channel cannot give.
                auto_approve_session=lambda: safety_override().is_active(),
                persist=lambda user_text, reply, is_new: self._persist_turn(
                    session_key, user_text, reply, is_new, agent
                ),
                notice=lambda sk, provider: self._maybe_notice(inbound, sk, provider),
                audit_caller=f"imessage:{redact_handle(handle)}",
                after_persist=_surface_new_session,
            ),
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
        )

    async def _handle_busy(self, inbound: Any, session_key: str) -> None:
        """Mid-turn message: fold into the running turn via steer.

        ``is_busy`` stays True through post-turn bookkeeping, so it alone can't
        tell a live turn from one that just finished. Gate steer on
        ``has_active_turn`` (parity with the other channels): steering a prompt
        that already ended would falsely acknowledge a merge. If the turn
        already finished, run the message as a fresh turn (safe -- is_busy is
        now False, so no re-entry loop); if a turn is in flight but steer isn't
        possible (cold start), ask the user to resend rather than silently
        dropping the message.
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
            await self._notify(inbound.handle, "⏳ Folded into the reply in progress.")
        else:
            await self._notify(
                inbound.handle,
                "⏳ Still working on the previous message — please resend in a moment.",
            )

    # -- Helpers ------------------------------------------------------------

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _session_key(self, handle: str) -> str:
        gen = self._conv.current_gen(handle)
        return build_dm_session_key(
            "imessage",
            self._resolve_agent(),
            handle,
            gen=gen,
            dm_scope=self.cfg.messaging.dm_scope,
        )

    def _seed_gen(self, handle: str) -> int:
        return seed_generation(
            self.sessions,
            channel="imessage",
            agent=self._resolve_agent(),
            user_id=handle,
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
        """Record the turn to conversation_log (dashboard visibility + restart).

        The markdown reply is stored as the agent produced it, not the flattened
        form iMessage received: the dashboard renders markdown, and flattening
        the archive would lose the code fences the user may want to copy later.
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
            title = (user_text or "").strip().replace("\n", " ")[:40] or "iMessage"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(
        self, inbound: "IMessageInbound", session_key: str, provider: Any
    ) -> None:
        """Context-length handling, surfaced as a separate message post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold
        forces a compaction so the window never overflows. Notices go out as
        their own messages and are kept out of the persisted turn so they're
        never replayed as assistant speech.
        """
        assert self.client is not None
        handle = inbound.handle
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.imessage.soft_threshold_pct:
            # Capability gate (#8156): no forced compaction to run and the
            # soft nudge's /compact advice cannot work — the backend compacts
            # on its own as context fills.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("imessage: context notice skipped — %s compacts itself", unsupported)
                return
        if pct >= self.cfg.imessage.hard_threshold_pct:
            self._conv.clear_awaiting(handle)
            try:
                await provider.compact()
                await provider.wait_for_compaction()
                await self._notify(
                    handle,
                    "🗜️ Context was near its limit, so it was compacted automatically.",
                )
            except Exception:
                logger.debug("imessage hard-threshold compaction failed", exc_info=True)
        elif pct >= self.cfg.imessage.soft_threshold_pct and not self._conv.is_awaiting(handle):
            self._conv.set_awaiting(handle)
            await self._notify(
                handle,
                "⚠️ This conversation's context is getting long — reply /compact "
                "to compress it, or /new to start fresh.",
            )

    async def _handle_compact(self, inbound: "IMessageInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        assert self.client is not None
        handle = inbound.handle
        session_key = self._session_key(handle)
        # Serialize compaction against the turn semaphore: compacting while a
        # turn is mutating the same session races the transcript. Distinguish a
        # busy session (ask the user to retry) from an absent one (nothing to
        # compact), and always release what we acquired.
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self._notify(
                    handle,
                    "⏳ Still working on the previous message — try /compact again shortly.",
                )
            else:
                await self._notify(handle, "ℹ️ There's no conversation to compact yet.")
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self._notify(handle, "ℹ️ There's no conversation to compact yet.")
                return
            # Capability gate (#8156, mirroring the dashboard's #7800 gate): a
            # backend that cannot serve a manual /compact treats the prompt as
            # ordinary text and never answers, so dispatching would strand the
            # unbounded wait below. Informational, never an error — and plain
            # text, because iMessage speech carries no markdown.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                await self._notify(
                    handle,
                    "ℹ️ This backend manages compaction automatically — it "
                    "summarizes the conversation on its own as context fills, "
                    "so manual /compact isn't needed (and isn't supported) here.",
                )
                return
            await provider.compact()
            await provider.wait_for_compaction()
            await self._notify(handle, "🗜️ Context compacted.")
        except Exception:
            logger.exception("imessage /compact failed for %s", session_key)
            await self._notify(handle, "⚠️ Compaction failed — please try again.")
        finally:
            self.sessions.release(session_key)
