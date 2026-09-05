"""Dispatch glue: WhatsAppTransport -> shared drive_turn -> WhatsAppRenderer.

Mirrors the weixin dispatcher, plus the WhatsApp group flow: an unprompted
rules-mode turn injects the group's rules and the silence contract, delivers
nothing when the model answers the sentinel, and only starts the group
cooldown after an actually-delivered unprompted reply.

Dependency direction is ``whatsapp -> messaging`` (allowed).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from kiro_crew.history import mint_row_mid
from kiro_crew.messaging.approval import (
    TextReplyApprovalDecider,
    deliver_verdict,
    parse_approval_reply,
    pending_for,
)
from kiro_crew.messaging.commands import compact_unsupported_backend
from kiro_crew.messaging.conversation import ConversationState
from kiro_crew.messaging.dispatch import (
    ChannelTurn,
    delivery_is_muted,
    drive_turn,
    inbound_permitted,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import build_dm_session_key, seed_generation
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.platform.context import redact_via_context
from kiro_crew.whatsapp.commands import (
    COMPACT_AUTO_MANAGED_TEXT,
    COMPACT_AUTO_TEXT,
    COMPACT_BUSY_TEXT,
    COMPACT_FAILED_TEXT,
    COMPACT_NOTHING_TEXT,
    COMPACTED_TEXT,
    CONTEXT_LONG_TEXT,
    NEW_SESSION_TEXT,
    STATUS_UNAVAILABLE_TEXT,
    STOP_NOTHING_RUNNING_TEXT,
    STOPPED_TEXT,
    help_text,
    is_operator_only,
    parse_command,
)
from kiro_crew.whatsapp.group_gate import build_silence_contract
from kiro_crew.whatsapp.jids import is_group_jid, wa_id_to_user_jid
from kiro_crew.whatsapp.transport import WHATSAPP_CAPABILITIES, WhatsAppTransport
from kiro_crew.whatsapp.turn_renderer import WhatsAppRenderer

if TYPE_CHECKING:
    from kiro_crew.whatsapp.client import WhatsAppClient

logger = logging.getLogger(__name__)

_DEFAULT_AGENT = "kirocrew"

#: Phase reactions placed on the OPERATOR'S OWN inbound message, which is the
#: same affordance Slack draws with its status reactions. Reacting to their
#: message rather than to ours is deliberate: whether a linked device may react
#: to a message the account itself sent is not something this repo could verify,
#: and this direction needs no such assumption.
#:
#: These are chat content, not dashboard UI, so the no-emoji-in-UI rule does not
#: reach them; they live here as the owning module's constants rather than inline.
REACTION_WORKING = "\N{HOURGLASS WITH FLOWING SAND}"
REACTION_DONE = "\N{WHITE HEAVY CHECK MARK}"
REACTION_FAILED = "\N{WARNING SIGN}"
#: Clearing is a reaction with an empty body, per the WhatsApp convention.
REACTION_CLEAR = ""


class WhatsAppDispatcher:
    """Owns per-conversation state and drives turns for the WhatsApp channel."""

    def __init__(
        self,
        cfg: Any,
        sessions: Any,
        ctx_builder: Any,
        *,
        approval_mode: str,
        agent: str = "",
    ) -> None:
        self.cfg = cfg
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.approval_mode = approval_mode
        self.agent = agent
        self.client: "WhatsAppClient | None" = None
        self.transport: WhatsAppTransport | None = None
        self.conv_log: Any = None
        # Seeded from the persisted session map, as every sibling channel is: the
        # counter is in-memory, so without a seed it restarts at 0 after a gateway
        # restart and the next ``/new`` advances 0 -> 1 straight back onto the
        # ``:gen1`` still on disk, resuming the conversation the operator
        # explicitly discarded.
        self._conv: ConversationState[str] = ConversationState(seed_fn=self._seed_gen)

    async def handle_message(self, inbound: InboundMessage) -> None:
        """Transport dispatch callback: one normalized inbound message."""
        # A bare cancel survives a channel deny, which is the one documented
        # exemption: a policy added while a turn is in flight must not take away
        # the only way to stop it, and with `max_buttons=0` `/stop` IS the only
        # cancel affordance here. Attachment-bearing messages stay gated, because
        # media is fetched after authorize and a denied channel must not trigger
        # a download.
        if not await inbound_permitted(
            "whatsapp",
            text=inbound.text,
            has_attachments=bool(inbound.attachments),
        ):
            return
        assert self.transport is not None
        verdict = self.transport.pending_verdicts.get(id(inbound))
        group = is_group_jid(inbound.conversation_id)
        may_steer = verdict.may_steer if verdict is not None else not group

        # An approval answer is consumed BEFORE the command table and before the
        # model, because a bare "1" or "no" is meaningless as either. Gated on
        # is_operator, not may_steer: a group member typing "1" must never
        # resolve the operator's pending tool.
        if self.transport.is_operator(inbound):
            receipt = self._consume_approval_reply(inbound)
            if receipt:
                await self._say(inbound.conversation_id, receipt)
                return

        command = parse_command(inbound.text)
        if command is not None:
            # Operator-only, not merely steer-allowed, for anything that acts ON
            # the session: with messaging.dm_scope="unified" every direct DM
            # shares one bucket, so under dm_policy allowlist/open a peer's /new
            # would discard the conversation the operator is using. The table
            # decides per command, which is what lets /help stay answerable: it
            # discloses the command list and nothing about the operator or host.
            if not may_steer:
                return
            if is_operator_only(command) and not self.transport.is_operator(inbound):
                return
            await self._handle_command(inbound, command)
            return

        await self._drive(inbound, verdict)

    def _consume_approval_reply(self, inbound: InboundMessage) -> str:
        """The receipt for an approval answer, or ``""`` if this is not one.

        Nothing is consumed unless a request is actually open for this
        conversation's session, so ``no`` is an ordinary message at every other
        moment -- the alternative, treating it as a verdict whenever it parses,
        would silently swallow a real reply.
        """
        verdict = parse_approval_reply(inbound.text)
        if not verdict:
            return ""
        transport = self.transport
        if transport is None:
            return ""
        session_key = self._session_key(
            inbound.conversation_id, is_operator=transport.is_operator(inbound)
        )
        if pending_for(session_key, inbound.conversation_id) is None:
            return ""
        return deliver_verdict(session_key, verdict, inbound.conversation_id)

    async def _handle_command(self, inbound: InboundMessage, command: str) -> None:
        """Run one command. Reply strings come from the command table's module,
        which is also what ``/help`` is derived from, so a command cannot ship
        with prose that contradicts its own help line."""
        scope = inbound.conversation_id
        if command == "new":
            self._conv.bump_gen(scope)
            await self._say(scope, NEW_SESSION_TEXT)
        elif command == "compact":
            # Clear the nudge flag first, so the soft-threshold nudge can fire
            # again once the context refills after this compaction.
            self._conv.clear_awaiting(scope)
            await self._handle_compact(scope)
        elif command == "help":
            await self._say(scope, help_text())
        elif command == "status":
            await self._say(scope, self._status_text())
        elif command == "stop":
            await self._handle_stop(scope)

    def _status_text(self) -> str:
        """The shared runtime summary Slack's own ``status`` keyword posts.

        Guarded because it is a convenience, not a capability: a summary that
        cannot be built must not turn a status request into a failed turn.
        """
        try:
            from kiro_crew.stats import Stats

            return Stats().summary()
        except Exception:  # noqa: BLE001: a status read must not fail the turn
            logger.warning("whatsapp: could not build the status summary", exc_info=True)
            return STATUS_UNAVAILABLE_TEXT

    async def _handle_stop(self, scope: str) -> None:
        """Cooperative cancel, then report which of the two things happened.

        With no buttons this is the channel's ONLY cancel affordance, so the
        distinction matters to the operator: a running turn was interrupted, or
        there was nothing running and the queue was cleared. Reporting "stopped"
        for both would leave them unsure whether the reply they were waiting on
        is still coming.
        """
        session_key = self._session_key(scope)
        try:
            outcome = await self.sessions.stop_turn(session_key)
        except Exception:  # noqa: BLE001: the queue clear below still applies
            logger.warning("whatsapp: stop_turn failed", exc_info=True)
            outcome = None
        stopped = str(getattr(outcome, "kind", outcome) or "") in ("soft", "hard")
        await self._say(scope, STOPPED_TEXT if stopped else STOP_NOTHING_RUNNING_TEXT)

    async def _handle_compact(self, scope: str) -> None:
        """In-place ACP ``/compact`` on this conversation's current session.

        Serialized against the turn semaphore with ``try_acquire``: compacting
        while a turn is mutating the same session interleaves JSON-RPC on one
        provider and races the transcript. A refused acquire is two different
        situations to the operator -- a turn holds the session (ask again) or
        there is no session at all (nothing to compact) -- and telling them the
        wrong one leaves them waiting for a compaction that will never happen.
        Whatever is acquired is always released.
        """
        session_key = self._session_key(scope)
        if not await self.sessions.try_acquire(session_key):
            has_session = False
            try:
                has_session = bool(self.sessions.has_session(session_key))
            except Exception:  # noqa: BLE001: a lookup failure must not eat the receipt
                logger.warning("whatsapp: session lookup failed", exc_info=True)
            await self._say(scope, COMPACT_BUSY_TEXT if has_session else COMPACT_NOTHING_TEXT)
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self._say(scope, COMPACT_NOTHING_TEXT)
                return
            # Capability gate (#8156, mirroring the dashboard's #7800 gate): a
            # backend that cannot serve a manual /compact treats the prompt as
            # ordinary text and never answers, so dispatching would strand the
            # unbounded wait below. Informational, never an error.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("whatsapp: manual /compact declined — %s compacts itself", unsupported)
                await self._say(scope, COMPACT_AUTO_MANAGED_TEXT)
                return
            await provider.compact()
            await provider.wait_for_compaction()
            await self._say(scope, COMPACTED_TEXT)
        except Exception:
            logger.exception("whatsapp: /compact failed for %s", session_key)
            await self._say(scope, COMPACT_FAILED_TEXT)
        finally:
            self.sessions.release(session_key)

    async def _drive(self, inbound: InboundMessage, verdict: Any) -> None:
        assert self.transport is not None and self.client is not None
        transport = self.transport
        client = self.client
        scope = inbound.conversation_id
        is_operator = transport.is_operator(inbound)
        # A group's session key IS the group, so one session serves every member.
        # That makes "shared" a property of the conversation rather than of the
        # sender, which is what the context decision below has to key on.
        group = is_group_jid(scope)
        session_key = self._session_key(scope, is_operator=is_operator)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(inbound, session_key)
            return
        m = self.cfg.messaging
        self._conv.maybe_rotate(
            scope,
            time.time(),
            idle_minutes=m.idle_reset_minutes,
            daily_reset_hour=m.daily_reset_hour,
        )
        session_key = self._session_key(scope, is_operator=is_operator)
        agent = self._resolve_agent()
        unprompted = bool(verdict is not None and verdict.unprompted)
        # Interactive approval by typed reply, only for a sender allowed to
        # approve and only for a turn someone is actually watching. An
        # unprompted group turn nobody asked for must not stop to request
        # permission: there is no one waiting to answer, so it would sit until
        # the deny-on-timeout fires. Leaving the decider None keeps that turn at
        # the driver's deny-by-default, which is the honest outcome.
        # Two separate conditions, kept apart because they gate different things:
        # WHO sent it decides whether the approval ladder applies at all, and
        # whether anyone is WATCHING decides whether a prompt can be answered.
        may_approve = is_operator and not unprompted
        # A non-operator's turn runs at INTERACTIVE with no decider, which is the
        # driver's deny-by-default. Passing the configured mode through would let
        # `auto` or `trust` approve tool calls for a sender who is merely allowed
        # to CHAT: dm_policy="open" admits a stranger and a configured group
        # admits its members. The approval ladder is not theirs to inherit.
        approval_mode = self.approval_mode if is_operator else APPROVAL_INTERACTIVE
        # Setting the mode is not enough on its own: the PreToolUse hook can answer
        # `auto_approve` and a session carrying Trust short-circuits, both ahead of
        # the interactive ladder. So an untrusted sender's turn also carries the
        # explicit switch. They can talk to the agent; they cannot make it act.
        deny_all_tools = not is_operator
        decider = (
            TextReplyApprovalDecider(session_key, sessions=self.sessions)
            if approval_mode == APPROVAL_INTERACTIVE and may_approve
            else None
        )
        renderer = WhatsAppRenderer(
            transport,
            client,
            inbound.conversation_id,
            WHATSAPP_CAPABILITIES,
            unprompted=unprompted,
            session_key=session_key,
            approval_session_key=session_key if decider is not None else None,
            # Resolved at delivery time: the shared pipeline acquires the
            # provider inside the turn, so its cwd is not known here yet.
            upload_root=lambda: self._provider_cwd(session_key),
        )
        user_text = inbound.text
        if unprompted:
            user_text = build_silence_contract(verdict.rules) + "\n\n" + user_text
        await self._react(inbound, REACTION_WORKING, session_key=session_key)
        await drive_turn(
            ChannelTurn(
                channel_type="whatsapp",
                session_key=session_key,
                conversation_id=f"whatsapp:{scope}",
                agent=agent,
                user_text=user_text,
                renderer=renderer,
                approval_mode=approval_mode,
                decider=decider,
                auto_approve_session=(decider.trusted if decider is not None else None),
                deny_all_tools=deny_all_tools,
                # Private context is withheld whenever the SESSION is shared, not
                # merely when the current sender is not the operator. Denying
                # their tools does not reach this: the disclosure is in the
                # assembled context, before any tool runs.
                #
                # A group's key is the group, so one session serves every member.
                # Keying this on the sender alone leaked anyway, one turn later:
                # the operator addressing the agent in a group injected their
                # memory, lessons and skills into that shared session, and ACP
                # replays native history, so a member's own minimal-context turn
                # could still be answered out of it. The property that matters
                # belongs to the conversation, so a group turn is minimal for
                # everyone, INCLUDING the operator. The self-chat and DMs are
                # where context-rich work belongs; anything the agent says in a
                # group is visible to the group anyway.
                minimal_context=group or not is_operator,
                persist=(
                    None if unprompted else lambda u, r, n: self._persist_turn(session_key, u, r, n)
                ),
                # The channel's ONLY reach into context accounting: this is what
                # calls check_context_usage, which is in turn the sole trigger
                # for the backend autocompactor. Omitting it leaves a
                # shared-pipeline channel with no compaction of any kind.
                notice=lambda sk, provider: self._maybe_notice(
                    scope, sk, provider, unprompted=unprompted
                ),
                audit_caller=f"whatsapp:{inbound.user_id}",
            ),
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
        )
        if unprompted and renderer.delivered:
            transport.group_gate.record_unprompted_reply(scope)
        # The OUTCOME, which is not the complement of "something reached the
        # chat": a failed turn delivers the apology notice, so ``delivered`` is
        # True there too and reading it alone stamps a failed turn with the
        # success marker. On a phone the reaction is the compact signal the
        # operator scans, so a wrong one is worse than none.
        succeeded = renderer.delivered and not renderer.failed
        await self._react(
            inbound,
            REACTION_DONE if succeeded else REACTION_FAILED,
            session_key=session_key,
        )

    async def _react(self, inbound: InboundMessage, emoji: str, *, session_key: str = "") -> None:
        """Draw a phase reaction on the operator's inbound message.

        The one chokepoint for every reaction, so the two cases that must stay
        unmarked are guarded here rather than at each call site:

        * an unprompted group turn -- the silence contract exists so the agent
          can stay out of a conversation it was not addressed in, and a reaction
          is still a visible mark in that group;
        * a muted conversation -- ``drive_turn`` swaps in ``SilentRenderer``, so
          this renderer's flags describe nothing that was sent, and a mute the
          operator asked for must not answer back with a warning sign.
        """
        transport = self.transport
        if self.client is None or transport is None:
            return
        message_id = transport.pending_message_id.get(id(inbound), "")
        if not message_id:
            return
        if is_group_jid(inbound.conversation_id) and not inbound.is_mention:
            return
        if session_key and delivery_is_muted(self.sessions, session_key, "whatsapp"):
            return
        # Through the TRANSPORT, which owns the echo tracker: a reaction is a
        # message and echoes back with from_me set.
        await transport.react(
            inbound.conversation_id,
            wa_id_to_user_jid(inbound.user_id),
            message_id,
            emoji,
        )

    def _provider_cwd(self, session_key: str) -> str:
        """The acquired provider's working directory, or "" when unknown.

        The one tree an outbound file reference may name. Empty disables uploads
        rather than falling back to anything wider.
        """
        try:
            provider = self.sessions.get_provider(session_key)
        except Exception:  # noqa: BLE001
            return ""
        return str(getattr(provider, "cwd", "") or "")

    async def _handle_busy(self, inbound: InboundMessage, session_key: str) -> None:
        if not self.sessions.is_busy(session_key):
            await self.handle_message(inbound)
            return
        # Steering injects this text into a turn that is ALREADY RUNNING. With
        # messaging.dm_scope="unified" every direct DM shares one session, so an
        # admitted non-operator could otherwise redirect the turn the operator is
        # waiting on. They still get the busy receipt below; they just cannot
        # change what is running.
        may_steer_session = self.transport is not None and self.transport.is_operator(inbound)
        provider = self.sessions.get_provider(session_key)
        steer = getattr(provider, "steer", None)
        has_active = getattr(provider, "has_active_turn", None)
        live = has_active is None or bool(has_active())
        ok = bool(
            may_steer_session
            and live
            and getattr(provider, "supports_steer", False)
            and steer is not None
            and await steer(inbound.text)
        )
        if ok:
            note = "Folded into the current reply."
        else:
            note = "Still working on the last message; please resend shortly."
        await self._say(inbound.conversation_id, note)

    async def _maybe_notice(
        self, scope: str, session_key: str, provider: Any, *, unprompted: bool
    ) -> None:
        """Context accounting for the turn that just ended.

        Three separate things happen here, and only the last is a message:

        * ``check_context_usage`` records the reading and is what arms the
          backend autocompactor -- it is the sole caller of the session
          manager's compaction trigger, and this callback is the only way a
          shared-pipeline channel reaches it;
        * past ``whatsapp.hard_threshold_pct`` the context is compacted in place
          now, rather than waiting for the backend threshold, which sits higher;
        * past ``whatsapp.soft_threshold_pct`` the operator is nudged once.

        The reading and the compaction are session hygiene and run whatever the
        conversation is, because a window that overflows costs the operator the
        conversation. Only the TEXT is conditional, on the same two rules
        ``_react`` carries: an unprompted group turn may still be choosing
        silence, and a muted conversation is one the operator switched off.
        """
        pct = self.sessions.check_context_usage(session_key, provider)
        may_speak = not unprompted and not delivery_is_muted(self.sessions, session_key, "whatsapp")
        wa = self.cfg.whatsapp
        if pct >= wa.soft_threshold_pct:
            # Capability gate (#8156): no forced compaction to run and the
            # soft nudge's /compact advice cannot work — the backend compacts
            # on its own as context fills.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug(
                    "whatsapp: context notice skipped — %s compacts itself",
                    unsupported,
                )
                return
        if pct >= wa.hard_threshold_pct:
            self._conv.clear_awaiting(scope)
            try:
                await provider.compact()
                await provider.wait_for_compaction()
            except Exception:  # noqa: BLE001: the reply already landed
                logger.debug("whatsapp: hard-threshold compaction failed", exc_info=True)
                return
            if may_speak:
                await self._say(scope, COMPACT_AUTO_TEXT)
        elif pct >= wa.soft_threshold_pct and not self._conv.is_awaiting(scope):
            # The flag records that the nudge WAS SENT, so it is set only when
            # one goes out: setting it while suppressed would spend the single
            # nudge this conversation gets on a message nobody read.
            if may_speak:
                self._conv.set_awaiting(scope)
                await self._say(scope, CONTEXT_LONG_TEXT)

    def _persist_turn(
        self, session_key: str, user_text: str, reply_text: str, is_new: bool
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart)."""
        if self.conv_log is None:
            return
        # This row is an EGRESS: persisted, then served to dashboard readers.
        user_text = redact_via_context(user_text)
        self.conv_log.append(session_key, "user", user_text, mid=mint_row_mid())
        if reply_text:
            self.conv_log.append(session_key, "assistant", reply_text, mid=mint_row_mid())
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "WhatsApp"
            self.conv_log.set_title(session_key, title)

    async def _say(self, chat_jid: str, text: str) -> None:
        if self.transport is None:
            return
        try:
            await self.transport.send_message(chat_jid, text)
        except Exception:
            logger.warning("whatsapp: out-of-band send failed", exc_info=True)

    def _session_key(self, scope: str, *, is_operator: bool = True) -> str:
        """The session address for *scope*.

        ``messaging.dm_scope="unified"`` collapses every direct DM into one
        ``unified:{agent}`` bucket for cross-surface continuity, which is the
        right behaviour for the OPERATOR: their WhatsApp and dashboard
        conversations are the same conversation. It is the wrong behaviour for
        anyone else, because the bucket carries the operator's history: an
        admitted peer could ask what was discussed earlier and be told. A
        non-operator therefore always gets their own per-peer bucket, whatever
        the global setting says.
        """
        from kiro_crew.messaging.link import (
            CHAT_TYPE_DIRECT,
            CHAT_TYPE_FORUM,
            DM_SCOPE_PER_CHANNEL_PEER,
        )

        chat_type = CHAT_TYPE_FORUM if is_group_jid(scope) else CHAT_TYPE_DIRECT
        dm_scope = self.cfg.messaging.dm_scope if is_operator else DM_SCOPE_PER_CHANNEL_PEER
        return build_dm_session_key(
            "whatsapp",
            self._resolve_agent(),
            scope,
            gen=self._conv.current_gen(scope),
            dm_scope=dm_scope,
            chat_type=chat_type,
        )

    def _resolve_agent(self) -> str:
        """The agent every session key for this channel is built under."""
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_AGENT

    def _seed_gen(self, scope: str) -> int:
        """The highest generation already persisted for *scope*'s durable bucket.

        Must address the SAME bucket ``_session_key`` builds, so the chat type is
        re-derived from the JID here: a group scope always keeps its full forum
        bucket, and reading a direct-chat bucket for it would answer 0 for a
        conversation that has generations on disk.

        The operator's ``dm_scope`` is the one used, because ``seed_fn`` is handed
        a scope with no sender attached and the two possible readings fail in
        opposite directions. Reading the operator's bucket for what turns out to
        be a peer's scope over-seeds, which only skips generations and still
        yields a fresh session; reading a peer bucket for the OPERATOR under
        ``dm_scope="unified"`` would look at a bucket their conversation does not
        live in, answer 0, and reintroduce exactly the resurrection this seeding
        exists to prevent.
        """
        from kiro_crew.messaging.link import CHAT_TYPE_DIRECT, CHAT_TYPE_FORUM

        return seed_generation(
            self.sessions,
            channel="whatsapp",
            agent=self._resolve_agent(),
            user_id=scope,
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=CHAT_TYPE_FORUM if is_group_jid(scope) else CHAT_TYPE_DIRECT,
        )
