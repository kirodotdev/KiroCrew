"""Full new-path dispatch: SignalTransport -> TurnDriver -> SignalRenderer.

``SignalTransport.receive()`` drops own traffic and the owner's syncs to third
parties, fails closed on groups, authorizes the sender (or the account itself
for Note to Self), runs the echo ledger, then hands the ``SignalInbound`` to
:meth:`SignalDispatcher.handle_message`, which mirrors the iMessage dispatch:

    approval answer (a typed 1/2/3 while a prompt is open)
    -> command intercept (/new, /compact, /help, /status, /stop)
    -> construct SignalRenderer + on_turn_start (read receipt + typing)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft/hard threshold notice)  # guarded
    -> renderer.close() + session release   # in finally

Signal has no interactive buttons, so tool approval is WhatsApp's typed-reply
ladder (``messaging.approval``): in ``interactive`` mode an OPERATOR's turn
(``SignalTransport.is_operator``, narrower than the allow-list) carries a
:class:`TextReplyApprovalDecider`, the renderer sends a numbered prompt, and the
operator's next message, when it is ENTIRELY an answer, resolves it before the
command table and before the model ever sees it. No answer within
``TEXT_APPROVAL_TIMEOUT_S`` denies. ``ChannelTurn.auto_approve_session`` honours
the session's own Trust (answer 3) and the process-global safety-override grant.
Every message this module sends is plain text.

Dependency direction is ``signal_cli -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kiro_crew.config import live
from kiro_crew.config.sections import _normalize_threshold_pair
from kiro_crew.history import mint_row_mid
from kiro_crew.messaging.approval import (
    TEXT_APPROVAL_TIMEOUT_S,
    TextReplyApprovalDecider,
    deliver_verdict,
    parse_approval_reply,
    pending_for,
)
from kiro_crew.messaging.attachments import cleanup_offloaded
from kiro_crew.messaging.commands import (
    compact_refusal_plain_text,
    compact_unsupported_backend,
    context_recycle_warning,
    recycle_backend,
    recycle_warning_should_send,
)
from kiro_crew.messaging.conversation import reserve_new_generation
from kiro_crew.messaging.dispatch import (
    ChannelTurn,
    admit_inbound_callback,
    build_directive_consumer,
    drive_turn,
    inbound_permitted,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.inbound_spool import InboundRoute
from kiro_crew.messaging.link import (
    DM_SCOPE_UNIFIED,
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    channel_namespace_of,
    seed_generation,
)
from kiro_crew.messaging.pre_turn import resolve_pre_turn
from kiro_crew.safety_override import safety_override
from kiro_crew.session_lifecycle import (
    STOP_DECLINED_COMPACTING_TEXT,
    compaction_in_flight,
    consume_stop_declined,
    decline_stop,
)
from kiro_crew.signal_cli.client import KIND_DATA, redact_identifier
from kiro_crew.signal_cli.commands import (
    HELP_TEXT,
    STATUS_UNAVAILABLE_TEXT,
    STOP_NOTHING_RUNNING_TEXT,
    STOPPED_TEXT,
    ConversationState,
    is_operator_only,
    parse_command,
)
from kiro_crew.signal_cli.files import provider_cwd
from kiro_crew.signal_cli.renderer import SignalRenderer, display_safe_text
from kiro_crew.signal_cli.rpc import RpcError, RpcTransportError
from kiro_crew.signal_cli.transport import SIGNAL_CAPABILITIES
from kiro_crew.start_priority import person_priority

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.signal_cli.client import SignalClient, SignalInbound
    from kiro_crew.signal_cli.transport import SignalTransport

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Signal sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the other
# channels' _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"


def _route(inbound: "SignalInbound") -> str:
    """The conversation a Signal inbound belongs to, and where replies go.

    The identity the transport matched against the allow-list (normalized), or
    the account itself for Note to Self -- set by ``SignalTransport.receive``
    before dispatch, so it always names a roster entry.
    """
    return inbound.conversation_id


@dataclass
class _TurnFiles:
    """Staged attachment files a running Signal turn owns besides its own message's.

    A message steered into a running turn is folded in as text, and that text
    names the message's staged files, so they must outlive the dispatch that
    delivered them: the transport would delete them as soon as the steer
    returned, while the turn has yet to read them. The steer hands them here
    instead, and the turn deletes them when it ends -- normally, on an error or
    on a cancel.
    """

    paths: list[str] = field(default_factory=list)


def _compact_refusal_text(backend: str) -> str:
    """Which of the shared plain-text refusals *backend* gets, with its glyph.

    This channel carries no markdown, so it uses the shared plain sentences
    (``messaging.commands.compact_refusal_plain_text``) as iMessage does.
    """
    return "ℹ️ " + compact_refusal_plain_text(backend)


class SignalDispatcher:
    """Coordinates Signal turns onto the shared ``TurnDriver``.

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
        #: How long a typed approval waits before denying. An attribute so a
        #: test can close the window without waiting it out.
        self.approval_timeout_s = TEXT_APPROVAL_TIMEOUT_S
        self.client: "SignalClient | None" = None
        # Set by maybe_start_signal after construction (the client<->transport
        # construction cycle forbids doing it here); the config applier pushes
        # the reloaded allow-list at it.
        self.transport: "SignalTransport | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)
        #: The running turn per session key that owns files steered into it.
        self._turn_files: dict[str, _TurnFiles] = {}
        # Held on self: the watcher holds the owner WEAKLY, so a subscription
        # dropped here would be collected and the applier would silently stop
        # firing.
        self._config_sub = live.watch_section(
            self, "signal", "messaging", target="transport", name="SignalDispatcher"
        )

    # -- Live config ---------------------------------------------------------

    def _live_cfg(self) -> "KiroCrewConfig":
        """The config in force NOW, for a per-turn read.

        The watcher's snapshot when it is armed, else a fingerprint-cached
        ``load()`` (two stats on a hit), else the boot copy. Falling back to
        ``self.cfg`` rather than raising keeps a turn running when the config
        file is momentarily unreadable -- a threshold or a rotation window is
        not an authorization decision, and the boot value is the one the
        operator last had in force.
        """
        return live.current(self.cfg, log_prefix="signal")

    def _thresholds(self) -> tuple[int, int]:
        """``(soft, hard)`` context thresholds from the live config.

        Re-runs the loader's own pair normalization, because reading the two
        fields live without it can leave ``soft > hard`` and make the soft nudge
        unreachable -- ``_maybe_notice`` tests ``pct >= hard`` first.
        """
        # ``getattr``: the section is read the same way the boot factory reads
        # it, so a config without one falls back to the shared defaults.
        section = getattr(self._live_cfg(), "signal", None)
        return _normalize_threshold_pair(
            int(getattr(section, "soft_threshold_pct", 80)),
            int(getattr(section, "hard_threshold_pct", 95)),
        )

    # -- Advisory delivery ---------------------------------------------------

    async def _notify(self, handle: str, text: str) -> bool:
        """Deliver an advisory notice, best-effort.

        Command acknowledgements, help text and compaction notices are status
        chatter, not the answer, so a bridge failure while sending one must not
        abort the dispatch that produced it. ``client.send`` raises on a real
        delivery failure (it does not collapse failure into an empty guid), so
        the tolerance lives here, at the call sites that genuinely want it,
        rather than inside the client where it would also hide a lost reply.

        Returns whether the notice was sent: the declined-stop path arms its
        escalation only on an explicit ``True`` (``session_lifecycle.decline_stop``).

        Screened with ``display_safe_text`` like every other Signal sink: a
        notice can quote text the channel did not author (a status line, a
        compaction or signal-cli error), and it bypasses the renderer.
        """
        try:
            assert self.client is not None, "SignalDispatcher.client must be set"
            await self.client.send(handle, display_safe_text(text))
        except (RpcError, RpcTransportError) as exc:
            logger.warning(
                "signal: advisory notice to %s not delivered: %s",
                redact_identifier(handle),
                exc,
            )
            return False
        return True

    # -- Operator + approval -------------------------------------------------

    @staticmethod
    def _is_operator(inbound: "SignalInbound") -> bool:
        """Whether *inbound* comes from the operator.

        Reads the verdict ``SignalTransport.receive`` reached when it admitted
        the sender (``SignalTransport.is_operator``): the account itself when it
        is on the allow-list, else the sole allow-list entry, else nobody. That
        is NARROWER than admission on purpose -- every allowed contact may chat,
        but under a unified ``dm_scope`` they all share the operator's session,
        so only the operator may approve a tool (3 trusts the whole session),
        run a session-acting command or steer a running turn. An inbound that
        did not pass the transport's gate has no principal and is never the
        operator.
        """
        return bool(inbound.principal) and inbound.operator

    def _consume_approval_reply(self, inbound: "SignalInbound") -> str:
        """The receipt for an approval answer, or ``""`` if this is not one.

        Nothing is consumed unless a request is actually open for THIS
        conversation, so ``no`` is an ordinary message at every other moment.
        The lookup is narrowed to the conversation the prompt was posted in,
        so under a unified ``dm_scope`` (one session for every DM) another
        allow-listed person's "1" cannot answer the operator's prompt.
        """
        verdict = parse_approval_reply(inbound.text)
        if not verdict:
            return ""
        handle = _route(inbound)
        session_key = self._session_key(handle)
        if pending_for(session_key, handle) is None:
            return ""
        return deliver_verdict(session_key, verdict, handle)

    # -- Turn dispatch (transport's dispatch callback) -----------------------

    async def handle_message(self, inbound: "SignalInbound") -> None:
        """Drive one authorized inbound Signal message through TurnDriver."""
        assert self.client is not None, "SignalDispatcher.client must be set"
        # Inbound channels-governance gate (off-loop) -- recheck per message so
        # a host-profile deny added after connect stops dispatch without a
        # restart (the startup gate only blocks CONNECTING). Silently drop.
        if not await inbound_permitted("signal"):
            return
        handle = _route(inbound)
        text = inbound.text
        inbound_route = InboundRoute(
            conversation_id=handle,
            text=inbound.spool_text,
            user_id=handle,
            message_id=str(inbound.timestamp or ""),
            # The files cannot be replayed after a restart, but the notice
            # names them, so an uncaptioned picture is not silently dropped.
            attachments_dropped=inbound.attachments,
        )
        if not await admit_inbound_callback(
            self.sessions,
            channel_type="signal",
            route=inbound_route,
        ):
            return
        logger.info("Signal inbound from %s: %d chars", redact_identifier(handle), len(text or ""))
        is_operator = self._is_operator(inbound)

        # -- Approval answer: BEFORE the command table and the model, because a
        # bare "1" or "no" is meaningless as either. The running turn holds the
        # session (busy), so this must also precede the busy/steer path, or the
        # answer would be folded into the very turn that is waiting for it.
        if is_operator:
            receipt = self._consume_approval_reply(inbound)
            if receipt:
                await self._notify(handle, receipt)
                return

        # -- Command intercept (no LLM session needed) --
        cmd = parse_command(text)
        if cmd is not None and is_operator_only(cmd) and not is_operator:
            return
        if cmd == "new":
            self._conv.bump_gen(handle)
            saved = await reserve_new_generation(
                self.sessions,
                self._session_key(handle),
                channel_type="Signal",
            )
            message = "✅ Started a fresh conversation."
            if not saved:
                message += "\n⚠️ The new conversation could not be saved for restart."
            await self._notify(handle, message)
            return
        if cmd == "compact":
            self._conv.clear_awaiting(handle)
            await self._handle_compact(inbound)
            return
        if cmd == "help":
            await self._notify(handle, HELP_TEXT)
            return
        if cmd == "status":
            await self._notify(handle, self._status_text())
            return
        if cmd == "stop":
            await self._handle_stop(handle)
            return

        # Busy check, then rotation, then a re-derived key -- the ordering and the
        # reasons it matters live in messaging.pre_turn.
        session_key = await resolve_pre_turn(
            conv=self._conv,
            sessions=self.sessions,
            key=handle,
            session_key_for=self._session_key,
            idle_minutes=self._live_cfg().messaging.idle_reset_minutes,
            daily_reset_hour=self._live_cfg().messaging.daily_reset_hour,
            on_busy=lambda sk: self._handle_busy(inbound, sk),
        )
        if session_key is None:
            return  # folded into the running turn
        conversation_id = f"signal:{handle}"
        agent = self._resolve_agent()
        # This conversation is the session's own mirror and origin, so a
        # dashboard turn, a file_send, a cron result or a compaction notice for
        # it is delivered here rather than left in the dashboard outbox.
        await self._bind_origin_mirror(session_key, handle)

        # Interactive approval by typed reply, for the operator only. A sender
        # who is not one keeps the driver's deny-by-default (decider None).
        decider = (
            TextReplyApprovalDecider(
                session_key, sessions=self.sessions, timeout_s=self.approval_timeout_s
            )
            if self.approval_mode == APPROVAL_INTERACTIVE and is_operator
            else None
        )
        renderer = SignalRenderer(
            self.client,
            handle,
            SIGNAL_CAPABILITIES,
            reply_timestamp=inbound.timestamp if inbound.kind == KIND_DATA else 0,
            session_key=session_key,
            approval_session_key=session_key if decider is not None else None,
            react_author=inbound.sender,
            react_timestamp=inbound.timestamp,
            upload_root=lambda: provider_cwd(self.sessions, session_key),
        )

        def _auto_approve() -> bool:
            # The process-global grant the dashboard toggle and Slack's
            # `/kirocrew yolo` drive, read per request so arming it (or letting
            # it lapse) takes effect on the next tool; or this session's own
            # Trust, recorded by answering 3. Neither weakens the PreToolUse
            # gate: TurnDriver runs the sensitive-path keystone, the governance
            # ceiling and the deny-list ahead of this rung, so a hard deny
            # still wins.
            if safety_override().is_active():
                return True
            return decider is not None and decider.trusted()

        # The turn skeleton (acquire -> identity -> context -> TurnDriver ->
        # guarded post-turn -> finally close/release) lives once in
        # messaging.dispatch. Only the Signal-specific pieces are injected.
        # Immediately surface a newly-created channel session in the dashboard
        # rather than waiting for the reconciler. Circular import -- dashboard
        # boot imports channel packages -- so import lazily.
        async def _surface_new_session() -> None:
            from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

            await surface_dispatcher_session(self)

        # This turn owns whatever a message steered into it hands over (see
        # ``_TurnFiles``), unless another turn on the key already does.
        files = _TurnFiles()
        owns_files = self._turn_files.setdefault(session_key, files) is files
        try:
            await drive_turn(
                ChannelTurn(
                    start_priority=person_priority(inbound.person_origin),
                    channel_type="signal",
                    session_key=session_key,
                    inbound_route=inbound_route,
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
                    decider=decider,
                    auto_approve_session=_auto_approve,
                    persist=lambda user_text, reply, is_new: self._persist_turn(
                        session_key, user_text, reply, is_new, agent
                    ),
                    notice=lambda sk, provider: self._maybe_notice(inbound, sk, provider),
                    audit_caller=f"signal:{redact_identifier(handle)}",
                    after_persist=_surface_new_session,
                ),
                sessions=self.sessions,
                ctx_builder=self.ctx_builder,
            )
        finally:
            if owns_files:
                await self._release_turn_files(session_key, files)

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
        # Steering injects this text into a turn that is ALREADY RUNNING. Under
        # messaging.dm_scope="unified" every allowed contact shares one session,
        # so a non-operator could otherwise redirect the turn the operator is
        # waiting on (WhatsApp's rule). They still get the busy receipt.
        may_steer = self._is_operator(inbound)
        provider = self.sessions.get_provider(session_key)
        steer = getattr(provider, "steer", None)
        has_active = getattr(provider, "has_active_turn", None)
        live = has_active is None or bool(has_active())
        can_steer = bool(
            may_steer and live and getattr(provider, "supports_steer", False) and steer is not None
        )
        handed: list[str] = []
        if can_steer and inbound.temp_paths:
            # The steered text names this message's staged files, so the turn
            # it folds into must own them (``_TurnFiles``). Handed over BEFORE
            # the steer: the steer can land and that turn end before it returns.
            # With no turn of ours holding the key (a dashboard turn on this
            # session) nothing would delete them after it read them, so such a
            # message is not steered (the receipt asks for a resend), as
            # Telegram never steers one carrying files.
            files = self._turn_files.get(session_key)
            if files is None:
                can_steer = False
            else:
                handed, inbound.temp_paths = inbound.temp_paths, []
                files.paths.extend(handed)
        steered = bool(can_steer and steer is not None and await steer(inbound.text))
        if handed and not steered:
            # Declined: the message is not in the turn, so its files go back to
            # the transport, which deletes them now. A steer that RAISED (or was
            # cancelled) may have reached the turn: the turn keeps them.
            owner = self._turn_files.get(session_key)
            if owner is not None:
                owner.paths = [p for p in owner.paths if p not in handed]
            inbound.temp_paths = handed
        if steered:
            await self._notify(_route(inbound), "⏳ Folded into the reply in progress.")
        else:
            await self._notify(
                _route(inbound),
                "⏳ Still working on the previous message — please resend in a moment.",
            )

    # -- Commands -----------------------------------------------------------

    def _status_text(self) -> str:
        """The shared runtime summary (WhatsApp's and Slack's ``status``).

        Guarded: a summary that cannot be built must not fail the command.
        """
        try:
            from kiro_crew.stats import Stats

            return Stats().summary()
        except Exception:  # noqa: BLE001: a status read must not fail the turn
            logger.warning("signal: could not build the status summary", exc_info=True)
            return STATUS_UNAVAILABLE_TEXT

    async def _handle_stop(self, handle: str) -> None:
        """Cooperative cancel, then report which of the two things happened.

        With no buttons this is the channel's ONLY cancel affordance, so the
        receipt says whether a running turn was interrupted or nothing was
        running (the queue was cleared). A turn waiting on an approval prompt is
        a running turn: stopping it cancels the wait, which denies the tool.
        """
        session_key = self._session_key(handle)
        # A repeat within the window after a declined stop is the second press
        # and forces (WhatsApp's escalation). The conversation names the presser.
        force = consume_stop_declined(session_key, handle) and compaction_in_flight(
            self.sessions, session_key
        )
        try:
            outcome = await (
                self.sessions.stop_turn(session_key, force=True, preserve_queue=True)
                if force
                else self.sessions.stop_turn(session_key)
            )
        except Exception:  # noqa: BLE001: the receipt below still applies
            logger.warning("signal: stop_turn failed", exc_info=True)
            outcome = None
        kind = str(getattr(outcome, "kind", outcome) or "")
        if kind == "compacting":
            # Sent before the marker is armed: an undelivered warning plus an
            # armed escalation would hard-reset the session uninformed.
            await decline_stop(
                session_key,
                handle,
                lambda: self._notify(handle, STOP_DECLINED_COMPACTING_TEXT),
            )
            return
        stopped = kind in ("soft", "hard")
        await self._notify(handle, STOPPED_TEXT if stopped else STOP_NOTHING_RUNNING_TEXT)

    # -- Helpers ------------------------------------------------------------

    async def _release_turn_files(self, session_key: str, files: _TurnFiles) -> None:
        """Delete the files steered into the turn on *session_key*, which has ended.

        Unregistered first, in the same step as the list is taken, so a steer
        arriving after this cannot hand files to a turn that will never delete
        them (it finds no owner and does not steer them).
        """
        if self._turn_files.get(session_key) is files:
            del self._turn_files[session_key]
        paths, files.paths = files.paths, []
        if paths:
            await cleanup_offloaded(paths)

    async def _bind_origin_mirror(self, session_key: str, handle: str) -> None:
        """Bind this conversation as the session's mirror and record it as origin.

        The rule, the re-assert and the opt-out live in the shared
        ``bind_origin_mirror`` (as for Telegram, Discord and WeCom); this only
        supplies Signal's spelling of "this conversation": the peer's number or
        ACI, which is also what ``SignalTransport`` sends to (the account itself
        meaning Note to Self). Offloaded because ``mirror_opt_out`` can promote
        a legacy opt-out and rewrite the session map inline.

        The origin is what unattended output (a subagent completion, a cron
        result, the auto-compaction notice) resolves its target from. Guarded by
        the same key-based test ``bind_origin_mirror`` applies: a
        ``unified:{agent}`` bucket holds every allowed user's DMs, so it has no
        single origin, and recording one would deliver one user's output into
        another user's chat.
        """
        location = ChannelLink("signal", channel_id=handle, thread_id=None)
        await asyncio.to_thread(
            bind_origin_mirror, self.sessions, key=session_key, location=location
        )
        setter = getattr(self.sessions, "set_origin_link", None)
        if setter is not None and channel_namespace_of(session_key) != DM_SCOPE_UNIFIED:
            setter(session_key, location)

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _session_key(self, handle: str) -> str:
        gen = self._conv.current_gen(handle)
        return build_dm_session_key(
            "signal",
            self._resolve_agent(),
            handle,
            gen=gen,
            dm_scope=str(self.cfg.messaging.dm_scope),
        )

    def _seed_gen(self, handle: str) -> int:
        return seed_generation(
            self.sessions,
            channel="signal",
            agent=self._resolve_agent(),
            user_id=handle,
            dm_scope=str(self.cfg.messaging.dm_scope),
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
        form Signal received: the dashboard renders markdown, and flattening
        the archive would lose the code fences the user may want to copy later.
        """
        if self.conv_log is None:
            return
        self.conv_log.append(session_key, "user", user_text, agent=agent, mid=mint_row_mid())
        if reply_text:
            self.conv_log.append(
                session_key, "assistant", reply_text, agent=agent, mid=mint_row_mid()
            )
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Signal"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(
        self, inbound: "SignalInbound", session_key: str, provider: Any
    ) -> None:
        """Context-length handling, surfaced as a separate message post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold
        forces a compaction so the window never overflows. Notices go out as
        their own messages and are kept out of the persisted turn so they're
        never replayed as assistant speech.
        """
        assert self.client is not None
        handle = _route(inbound)
        pct = self.sessions.check_context_usage(session_key, provider)
        soft, hard = self._thresholds()
        if recycle_backend(provider):
            # Crew restarts this backend's session at the threshold; warn once
            # before it, offering a fresh start instead of a compaction.
            if recycle_warning_should_send(self.sessions, self._conv, handle, session_key, pct):
                await self._notify(handle, context_recycle_warning())
            return
        if pct >= soft:
            # Capability gate: no forced compaction to run and the
            # soft nudge's /compact advice cannot work — the backend compacts
            # on its own as context fills.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("signal: context notice skipped — %s compacts itself", unsupported)
                return
        if pct >= hard:
            self._conv.clear_awaiting(handle)
            try:
                await provider.compact()
                # A failed or timed-out compaction is a RETURNED result, not an
                # exception, so the notice is posted only for a completed one.
                cr = await provider.wait_for_compaction(
                    timeout=self.sessions.compact_wait_budget_secs()
                )
                if cr["type"] == "completed":
                    await self._notify(
                        handle,
                        "🗜️ Context was near its limit, so it was compacted automatically.",
                    )
                else:
                    logger.warning("signal hard-threshold compaction reported %s", cr["type"])
            except Exception:
                logger.debug("signal hard-threshold compaction failed", exc_info=True)
        elif pct >= soft and not self._conv.is_awaiting(handle):
            self._conv.set_awaiting(handle)
            await self._notify(
                handle,
                "⚠️ This conversation's context is getting long — reply /compact "
                "to compress it, or /new to start fresh.",
            )

    async def _handle_compact(self, inbound: "SignalInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        assert self.client is not None
        handle = _route(inbound)
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
            # Capability gate (mirroring the dashboard's gate): a
            # backend that cannot serve a manual /compact treats the prompt as
            # ordinary text and never answers, so dispatching would strand the
            # unbounded wait below. Informational, never an error — and plain
            # text, because this channel sends no markdown.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                await self._notify(handle, _compact_refusal_text(unsupported))
                return
            await provider.compact()
            # Failure and timeout come back as the result's ``type``, not as an
            # exception, so the receipt is read off it rather than assumed.
            cr = await provider.wait_for_compaction(
                timeout=self.sessions.compact_wait_budget_secs()
            )
            if cr["type"] == "completed":
                await self._notify(handle, "🗜️ Context compacted.")
            elif cr["type"] == "failed":
                await self._notify(handle, "⚠️ Compaction failed — please try again.")
            else:
                await self._notify(handle, "⚠️ Compaction timed out.")
        except Exception:
            logger.exception("signal /compact failed for %s", session_key)
            await self._notify(handle, "⚠️ Compaction failed — please try again.")
        finally:
            self.sessions.release(session_key)
