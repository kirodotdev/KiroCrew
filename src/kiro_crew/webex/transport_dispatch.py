"""Full new-path dispatch: WebexTransport -> TurnDriver -> WebexRenderer.

``WebexTransport.receive()`` authorizes + normalizes an inbound message and
hands the ``WebexInbound`` (carrying ``room_id``) to
:meth:`WebexDispatcher.handle_message`, which mirrors the Telegram/WeCom
transport dispatch:

    command intercept (/new, /compact, /help)
    -> construct WebexRenderer + on_turn_start (immediate placeholder)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft/hard threshold notice)  # guarded
    -> renderer.close() + session release   # in finally

Webex has no interactive buttons here, so the dispatcher runs the driver
``decider``-less (deny-by-default for INTERACTIVE mode; ``auto``/``trust``
still work). The security ``tool_gate`` and the ``spawn_run`` auto-approve
are wired inline off ``ctx_builder.hooks`` (channel-neutral) so this module
never imports ``kiro_crew.slack``.

Dependency direction is ``webex -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn, inbound_permitted
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import build_dm_session_key, seed_generation
from kiro_crew.webex.commands import HELP_TEXT, ConversationState, parse_command
from kiro_crew.webex.renderer import WebexRenderer
from kiro_crew.webex.transport import WEBEX_CAPABILITIES

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.webex.client import WebexClient, WebexInbound

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Webex sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the Slack /
# Telegram / WeCom paths' _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"


class WebexDispatcher:
    """Coordinates Webex turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-email conversation state
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
        self.client: "WebexClient | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(self, inbound: "WebexInbound") -> None:
        """Drive one authorized inbound Webex message through TurnDriver."""
        assert self.client is not None, "WebexDispatcher.client must be set"
        # Inbound channels-governance gate (off-loop) — recheck per message so a
        # host-profile deny added after connect stops dispatch without a restart
        # (the startup gate only blocks CONNECTING). Silently drop on deny.
        if not await inbound_permitted("webex"):
            return
        email = inbound.person_email
        room_id = inbound.room_id
        text = inbound.text
        logger.info("Webex inbound from %s: %d chars", email[:3] + "***" if email else "?", len(text or ""))

        # ── Command intercept (no LLM session needed) ──
        cmd = parse_command(text)
        if cmd == "new":
            self._conv.bump_gen(email)
            await self.client.send_message(room_id, "✅ Started a fresh conversation.")
            return
        if cmd == "compact":
            self._conv.clear_awaiting(email)
            await self._handle_compact(inbound)
            return
        if cmd == "help":
            await self.client.send_message(room_id, HELP_TEXT)
            return

        # ── Mid-turn concurrency: check the CURRENT-generation key for an
        # in-flight turn BEFORE any idle/daily rotation (rotating first could
        # mint a new key and miss the running turn, letting a second concurrent
        # turn bypass steer). Fold the message into the running turn via steer.
        session_key = self._session_key(email)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(inbound, session_key)
            return

        self._conv.maybe_rotate(
            email,
            time.time(),
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
        )
        session_key = self._session_key(email)
        conversation_id = f"webex:{email}"
        agent = self._resolve_agent()

        # Webex has no interactive buttons -> no decider (deny-by-default for
        # INTERACTIVE; auto/trust still auto-approve via the driver ladder).
        renderer = WebexRenderer(
            self.client,
            room_id,
            WEBEX_CAPABILITIES,
            session_key=session_key,
        )

        # The turn skeleton (acquire -> identity -> context -> TurnDriver ->
        # guarded post-turn -> finally close/release) lives once in
        # messaging.dispatch. Only the webex-specific pieces are injected.
        # Immediately surface a newly-created channel session in the dashboard
        # (feature: don't wait for the ~30s reconciler). Circular import —
        # dashboard boot imports channel packages — so import lazily.
        async def _surface_new_session() -> None:
            from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

            await surface_dispatcher_session(self)

        await drive_turn(
            ChannelTurn(
                channel_type="webex",
                session_key=session_key,
                conversation_id=conversation_id,
                agent=agent,
                user_text=text,
                renderer=renderer,
                approval_mode=self.approval_mode,
                decider=None,  # Webex can't render approve/deny buttons
                persist=lambda user_text, reply, is_new: self._persist_turn(
                    session_key, user_text, reply, is_new
                ),
                notice=lambda sk, provider: self._maybe_notice(inbound, sk, provider),
                audit_caller=f"webex:{email}",
                after_persist=_surface_new_session,
            ),
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
        )

    async def _handle_busy(self, inbound: Any, session_key: str) -> None:
        """Mid-turn message: fold into the running turn via steer.

        ``is_busy`` stays True through post-turn bookkeeping, so it alone
        can't tell a live turn from one that just finished. Gate steer on
        ``has_active_turn`` (parity with Telegram/WeCom): steering a prompt
        that already ended would falsely acknowledge a merge. If the turn
        already finished, run the message as a fresh turn (safe -- is_busy is
        now False, so no re-entry loop); if a turn is in flight but steer
        isn't possible (cold start), ask the user to resend rather than
        silently dropping the message.
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
            await self.client.send_message(inbound.room_id, "⏳ Folded into the reply in progress.")
        else:
            await self.client.send_message(
                inbound.room_id,
                "⏳ Still working on the previous message — please resend in a moment.",
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _session_key(self, email: str) -> str:
        gen = self._conv.current_gen(email)
        return build_dm_session_key(
            "webex",
            self._resolve_agent(),
            email,
            gen=gen,
            dm_scope=self.cfg.messaging.dm_scope,
        )

    def _seed_gen(self, email: str) -> int:
        return seed_generation(
            self.sessions,
            channel="webex",
            agent=self._resolve_agent(),
            user_id=email,
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
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Webex"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(self, inbound: "WebexInbound", session_key: str, provider: Any) -> None:
        """Context-length handling, surfaced as a separate message post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold
        forces a compaction so the window never overflows. Notices go out as
        their own messages (Webex supports proactive send) and are kept out of
        the persisted turn so they're never replayed as assistant speech.
        """
        assert self.client is not None
        email = inbound.person_email
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.webex.hard_threshold_pct:
            self._conv.clear_awaiting(email)
            try:
                await provider.compact()
                await provider.wait_for_compaction(timeout=120.0)
                await self.client.send_message(
                    inbound.room_id,
                    "🗜️ Context was near its limit, so it was compacted automatically.",
                )
            except Exception:
                logger.debug("Webex hard-threshold compaction failed", exc_info=True)
        elif pct >= self.cfg.webex.soft_threshold_pct and not self._conv.is_awaiting(email):
            self._conv.set_awaiting(email)
            await self.client.send_message(
                inbound.room_id,
                "⚠️ This conversation's context is getting long — reply `/compact` "
                "to compress it, or `/new` to start fresh.",
            )

    async def _handle_compact(self, inbound: "WebexInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        assert self.client is not None
        session_key = self._session_key(inbound.person_email)
        # Serialize compaction against the turn semaphore: compacting while a
        # turn is mutating the same session races the transcript. Distinguish
        # a busy session (ask the user to retry) from an absent one (nothing
        # to compact), and always release what we acquired.
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self.client.send_message(
                    inbound.room_id,
                    "⏳ Still working on the previous message — try `/compact` again shortly.",
                )
            else:
                await self.client.send_message(
                    inbound.room_id, "ℹ️ There's no conversation to compact yet."
                )
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self.client.send_message(
                    inbound.room_id, "ℹ️ There's no conversation to compact yet."
                )
                return
            await provider.compact()
            await provider.wait_for_compaction(timeout=120.0)
            await self.client.send_message(inbound.room_id, "🗜️ Context compacted.")
        except Exception:
            logger.exception("Webex /compact failed for %s", session_key)
            await self.client.send_message(
                inbound.room_id, "⚠️ Compaction failed — please try again."
            )
        finally:
            self.sessions.release(session_key)
