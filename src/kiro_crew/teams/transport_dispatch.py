"""Full new-path dispatch: TeamsTransport -> TurnDriver -> TeamsRenderer.

``TeamsTransport.receive()`` scope-gates + authorizes + normalizes an inbound
activity and hands the ``TeamsInbound`` (carrying ``conversation_id`` +
``service_url``) to :meth:`TeamsDispatcher.handle_message`, which mirrors the
Webex/WeCom transport dispatch:

    command intercept (/new, /compact, /help)
    -> construct TeamsRenderer + on_turn_start (typing indicator)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft/hard threshold notice)  # guarded
    -> renderer.close() + session release   # in finally

Teams (MVP) has no interactive buttons, so the dispatcher runs the driver
``decider``-less (deny-by-default for INTERACTIVE mode; ``auto``/``trust``
still work). The security ``tool_gate`` and the ``spawn_run`` auto-approve are
wired inline off ``ctx_builder.hooks`` (channel-neutral) so this module never
imports ``kiro_crew.slack``.

Dependency direction is ``teams -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.messaging.dispatch import ChannelTurn, drive_turn, inbound_permitted
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import build_dm_session_key, seed_generation
from kiro_crew.teams.commands import HELP_TEXT, ConversationState, parse_command
from kiro_crew.teams.renderer import TeamsRenderer
from kiro_crew.teams.transport import TEAMS_CAPABILITIES

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.teams.client import TeamsClient, TeamsInbound

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Teams sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the Slack /
# Telegram / WeCom / Webex paths' _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"


class TeamsDispatcher:
    """Coordinates Teams turns onto the shared ``TurnDriver``.

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
        self.client: "TeamsClient | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(self, inbound: "TeamsInbound") -> None:
        """Drive one authorized inbound Teams message through TurnDriver."""
        assert self.client is not None, "TeamsDispatcher.client must be set"
        # Inbound channels-governance gate (off-loop) — recheck per message so a
        # host-profile deny added after connect stops dispatch without a restart
        # (the startup gate only blocks CONNECTING). Silently drop on deny.
        if not await inbound_permitted("teams"):
            return
        email = inbound.user_email or inbound.aad_object_id
        conversation_id = inbound.conversation_id
        service_url = inbound.service_url
        text = inbound.text
        logger.info("Teams inbound from %s: %d chars", email[:3] + "***" if email else "?", len(text or ""))

        # ── Command intercept (no LLM session needed) ──
        cmd = parse_command(text)
        if cmd == "new":
            self._conv.bump_gen(email)
            await self.client.send_message(
                conversation_id, "✅ Started a fresh conversation.", service_url
            )
            return
        if cmd == "compact":
            self._conv.clear_awaiting(email)
            await self._handle_compact(inbound)
            return
        if cmd == "help":
            await self.client.send_message(conversation_id, HELP_TEXT, service_url)
            return

        # ── Mid-turn concurrency: check the CURRENT-generation key for an
        # in-flight turn BEFORE any idle/daily rotation, then fold the message
        # into the running turn via steer.
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
        agent = self._resolve_agent()

        # Teams has no interactive buttons -> no decider (deny-by-default for
        # INTERACTIVE; auto/trust still auto-approve via the driver ladder).
        renderer = TeamsRenderer(
            self.client,
            conversation_id,
            service_url,
            TEAMS_CAPABILITIES,
            session_key=session_key,
        )

        # The turn skeleton (acquire -> identity -> context -> TurnDriver ->
        # guarded post-turn -> finally close/release) lives once in
        # messaging.dispatch. Only the teams-specific pieces are injected.
        # NOTE: ChannelTurn.conversation_id is the SESSION-attribution id
        # (``teams:{email}``, what sessions.* has always been given here), not
        # ``inbound.conversation_id`` -- the Teams platform conversation id the
        # renderer replies into. The two are deliberately different; passing the
        # platform id would silently repoint every existing Teams session.
        # Immediately surface a newly-created channel session in the dashboard
        # (feature: don't wait for the ~30s reconciler). Circular import —
        # dashboard boot imports channel packages — so import lazily.
        async def _surface_new_session() -> None:
            from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

            await surface_dispatcher_session(self)

        await drive_turn(
            ChannelTurn(
                channel_type="teams",
                session_key=session_key,
                conversation_id=f"teams:{email}",
                agent=agent,
                user_text=text,
                renderer=renderer,
                approval_mode=self.approval_mode,
                decider=None,  # Teams can't render approve/deny buttons (MVP)
                persist=lambda user_text, reply, is_new: self._persist_turn(
                    session_key, user_text, reply, is_new
                ),
                notice=lambda sk, provider: self._maybe_notice(inbound, sk, provider),
                audit_caller=f"teams:{email}",
                after_persist=_surface_new_session,
            ),
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
        )

    async def _handle_busy(self, inbound: Any, session_key: str) -> None:
        """Mid-turn message: fold into the running turn via steer.

        Gate steer on ``has_active_turn`` (parity with Webex/WeCom): steering a
        prompt that already ended would falsely acknowledge a merge. If the
        turn already finished, run the message as a fresh turn; if a turn is in
        flight but steer isn't possible (cold start), ask the user to resend.
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
            await self.client.send_message(
                inbound.conversation_id, "⏳ Folded into the reply in progress.", inbound.service_url
            )
        else:
            await self.client.send_message(
                inbound.conversation_id,
                "⏳ Still working on the previous message — please resend in a moment.",
                inbound.service_url,
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _session_key(self, email: str) -> str:
        gen = self._conv.current_gen(email)
        return build_dm_session_key(
            "teams",
            self._resolve_agent(),
            email,
            gen=gen,
            dm_scope=self.cfg.messaging.dm_scope,
        )

    def _seed_gen(self, email: str) -> int:
        return seed_generation(
            self.sessions,
            channel="teams",
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
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Teams"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(self, inbound: "TeamsInbound", session_key: str, provider: Any) -> None:
        """Context-length handling, surfaced as a separate message post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold
        forces a compaction so the window never overflows.
        """
        assert self.client is not None
        email = inbound.user_email or inbound.aad_object_id
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.teams.hard_threshold_pct:
            self._conv.clear_awaiting(email)
            try:
                await provider.compact()
                await provider.wait_for_compaction(timeout=120.0)
                await self.client.send_message(
                    inbound.conversation_id,
                    "🗜️ Context was near its limit, so it was compacted automatically.",
                    inbound.service_url,
                )
            except Exception:
                logger.debug("Teams hard-threshold compaction failed", exc_info=True)
        elif pct >= self.cfg.teams.soft_threshold_pct and not self._conv.is_awaiting(email):
            self._conv.set_awaiting(email)
            await self.client.send_message(
                inbound.conversation_id,
                "⚠️ This conversation's context is getting long — reply `/compact` "
                "to compress it, or `/new` to start fresh.",
                inbound.service_url,
            )

    async def _handle_compact(self, inbound: "TeamsInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        assert self.client is not None
        session_key = self._session_key(inbound.user_email or inbound.aad_object_id)
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self.client.send_message(
                    inbound.conversation_id,
                    "⏳ Still working on the previous message — try `/compact` again shortly.",
                    inbound.service_url,
                )
            else:
                await self.client.send_message(
                    inbound.conversation_id,
                    "ℹ️ There's no conversation to compact yet.",
                    inbound.service_url,
                )
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self.client.send_message(
                    inbound.conversation_id,
                    "ℹ️ There's no conversation to compact yet.",
                    inbound.service_url,
                )
                return
            await provider.compact()
            await provider.wait_for_compaction(timeout=120.0)
            await self.client.send_message(
                inbound.conversation_id, "🗜️ Context compacted.", inbound.service_url
            )
        except Exception:
            logger.exception("Teams /compact failed for %s", session_key)
            await self.client.send_message(
                inbound.conversation_id, "⚠️ Compaction failed — please try again.", inbound.service_url
            )
        finally:
            self.sessions.release(session_key)
