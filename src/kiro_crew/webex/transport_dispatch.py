"""Full new-path dispatch: WebexTransport -> TurnDriver -> WebexRenderer.

``WebexTransport.receive()`` authorizes + normalizes an inbound message and
hands the ``WebexInbound`` (carrying ``room_id``) to
:meth:`WebexDispatcher.handle_message`, which mirrors the Telegram/WeCom
transport dispatch:

    card-press intercept (an Adaptive Card submit carries no text)
    -> @mention strip (a space delivers the bot's own name in the text)
    -> approval-reply intercept (while a turn holds the session)
    -> command intercept (/new, /compact, /help, /stop, /link, …)
    -> mid-turn queue or steer
    -> construct WebexRenderer + on_turn_start (immediate placeholder)
    -> drive_turn: session acquire -> origin/mirror bind -> context build
                   -> TurnDriver.run   # shared redaction + approval ladder
                   -> guarded post-turn -> renderer.close() + release
    -> drain whatever queued during the turn

Webex stays ON the shared ``drive_turn`` rather than forking the pipeline the
way Telegram and Discord did. ``drive_turn`` is awaited and releases the session
semaphore in its ``finally``, so by the time the call returns the session is
free and the drain can simply re-enter ``handle_message``. Leaving the shared
pipeline would mean re-deriving mute substitution, identity publication, the
PreToolUse gate, the auto-approve hook and four independently guarded post-turn
steps — five chances to get a security-relevant step subtly wrong, for one
feature.

An approval's PRIMARY affordance is a typed ``1``/``2`` reply, with an Adaptive
Card riding alongside it. That order is deliberate and it is not a shortfall:
Webex refuses to edit a message once it carries an attachment, so a resolved
card's buttons stay clickable forever, and the inbound half of a press travels
over the undocumented device websocket. A typed reply is an ordinary inbound
message, needs nothing from the platform, and arrives on the path this dispatcher
already owns — so it is what the prompt asks for and what always works. The card
is guarded by a nonce minted against the pending decision, which is why a press
on a retired card is inert instead of answering whatever prompt is open now.
Either affordance answers only under INTERACTIVE: ``ChannelTurn.auto_approve_session``
still carries the process-global safety-override grant, so an operator's YOLO — and
``auto``/``trust`` — approves without ever posting a prompt.

The security ``tool_gate`` and the ``spawn_run`` auto-approve are wired
by the shared pipeline off ``ctx_builder.hooks`` (channel-neutral) so this
module never imports ``kiro_crew.slack``.

Dependency direction is ``webex -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.history import mint_row_mid, transcript_stem
from kiro_crew.messaging.approval import PendingApprovals, SessionApprovalDecider
from kiro_crew.messaging.attachments import append_attachment_context
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.commands import compact_unsupported_backend, compact_unsupported_reply
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
    DM_SCOPE_PER_CHANNEL_PEER,
    UNBIND_REASON_ORIGIN_REBIND,
    ChannelLink,
    build_dm_session_key,
    legacy_dashboard_mirror_key,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.messaging.pre_turn import resolve_pre_turn
from kiro_crew.messaging.queue_receipt import ReceiptQueue, ReceiptSurface
from kiro_crew.platform.context import redact_via_context
from kiro_crew.safety_override import describe_grant_lifetime, safety_override
from kiro_crew.sel import sel
from kiro_crew.webex import cards
from kiro_crew.webex.attachments import process_webex_attachments
from kiro_crew.webex.cards import LiveChoices, read_press
from kiro_crew.webex.client import WebexInbound
from kiro_crew.webex.commands import (
    ConversationState,
    build_help_text,
    is_bare_mid_turn_override,
    is_unknown_command,
    parse_command,
    parse_command_argument,
    parse_mid_turn_override,
    strip_bot_mention,
)
from kiro_crew.webex.renderer import WebexRenderer, webex_display_safe
from kiro_crew.webex.transport import ROOM_DIRECT, WEBEX_CAPABILITIES

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.webex.client import WebexClient

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Webex sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the Slack /
# Telegram / WeCom paths' _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"

# How many queued messages collapse into one drained turn. A burst answered as
# one turn keeps the conversation coherent; past this the surplus is deferred in
# order rather than dropped, so the combined prompt stays bounded. Same value as
# the Telegram and Discord drains for the same reason — a single human will not
# realistically burst past it mid-turn — rather than a third number for one
# concept.
_MAX_COLLAPSE = 50

# Bound on an in-place compaction so a backend that streams text without ever
# reporting a compaction result cannot hold the session semaphore for the
# provider's own multi-hour prompt deadline.
_COMPACT_TIMEOUT_S = 120.0

# Compaction result types that mean nothing was compacted. Enumerated as
# FAILURES rather than allow-listing success: an unrecognised type is far more
# likely a renamed or added success spelling than a new failure, and reporting
# failure there would tell a user whose context did shrink to start over.
_COMPACT_FAILURE_TYPES = frozenset(("failed", "timeout"))

# What a queue receipt shows for a message whose only content is an attachment.
# An uncaptioned screenshot has no text, and a blank receipt line would read as a
# bug rather than as "your file is waiting".
_QUEUED_ATTACHMENT_LABEL = "(attachment)"

# How many conversations ``/sessions`` prints. A Webex message is byte-capped,
# and a list longer than this stops being scannable anyway.
_SESSIONS_LIST_MAX = 15

# What to say when an answer arrives with no prompt waiting. Deliberately does
# NOT claim a denial: this is reached both when the window timed out (denied) and
# when the OTHER affordance already answered (possibly approved) — and the second
# is the common case, because Webex refuses to edit a message carrying an
# attachment, so a resolved card's buttons stay clickable forever. Telling a user
# their approved tool was denied is worse than telling them nothing changed.
_APPROVAL_NOT_PENDING = (
    "⌛ That prompt is already answered or timed out — no new decision was sent."
)

# The typed answers an approval prompt accepts: EXACTLY what the prompt asks for,
# and nothing else.
#
# The temptation is to be generous — "ok", "yes", "y", "sure" — and it is the
# wrong instinct in this one place, asymmetrically so. A too-narrow set costs the
# user a retry after an explicit "that is not 1 or 2". A too-wide one silently
# APPROVES A TOOL: "ok" is an extremely common thing to type into a chat, a
# pending prompt makes any message a candidate answer, and the user who typed it
# as conversation gets no signal that they just authorised a write. So the
# accepted words are the two digits the prompt names, plus the two verbs that
# cannot be mistaken for conversational filler.
_APPROVE_REPLIES = frozenset(("1", "approve"))
_DENY_REPLIES = frozenset(("2", "deny"))

#: Process-global approval registry for this channel. Module scope because a
#: pending prompt must outlive the dispatcher method that created it and be
#: resolvable from a later inbound frame, which is a different call stack.
_APPROVALS = PendingApprovals("webex")

#: Scope-path segment marking a route that addresses a SPACE, not a person. A
#: Webex room id is an opaque base64 blob with no colon, so this prefix is what
#: tells the two route kinds apart everywhere one is read.
_SPACE_ROUTE_PREFIX = "space:"


def _route_of(inbound: "WebexInbound") -> str:
    """The conversation *inbound* belongs to.

    A direct room is the SENDER's conversation. A group space is the SPACE's,
    shared by everyone in it — the same semantics a Telegram group Topic gets,
    and the reason a space must never resolve to a participant's private DM key:
    that key carries their private history, is the target a mid-turn DM steers
    into, and is what their ``/new`` resets.

    Returned as ONE string that carries both the namespace and the identity, so
    the ``ConversationState`` bucket, the generation seed and the session key
    cannot disagree about which conversation a message routes to. A second
    parameter beside the id would be forgettable at exactly one call site, and
    the failure there is silent: the space quietly borrows a DM.
    """
    if inbound.room_type == ROOM_DIRECT:
        return inbound.person_email
    return f"{_SPACE_ROUTE_PREFIX}{inbound.room_id}"


def _chat_type_of(route: str) -> str:
    """The session-key namespace *route* belongs in.

    A space is ``forum``, which is what keeps it out of the ``unified`` DM bucket:
    ``build_dm_session_key`` collapses only ``direct`` routes, so a shared space
    can never merge into one person's cross-surface DM continuity.
    """
    return CHAT_TYPE_FORUM if route.startswith(_SPACE_ROUTE_PREFIX) else CHAT_TYPE_DIRECT


class WebexDispatcher:
    """Coordinates Webex turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-route conversation state
    (generation counter + soft-threshold flag) and the mid-turn queue receipts.
    ``handle_message`` is wired as the transport's dispatch callback. ``client``
    is set by the gateway after construction.
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
        self._queue = ReceiptQueue()
        # What the newest options card offered, per conversation. Owned HERE and
        # not by the renderer: that card is the last thing a turn sends, so every
        # press arrives after the turn — and the renderer — is gone.
        self._choices = LiveChoices()
        # Per-user model preference, applied when the NEXT session is created.
        self._model_pref: dict[str, str] = {}

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    def _reply_parent(self, inbound: "WebexInbound") -> str:
        """The thread root a reply to *inbound* belongs under, or ``""``.

        ONE derivation, shared by the dispatcher's own sends and by the renderer's
        answer, because they land in the same conversation: if they disagree, the
        answer sits in a thread while every ack, receipt and notice about it lands
        in the room root. Webex threads are FLAT, so the inbound's ``parentId`` is
        already the root and there is no nesting to resolve.
        """
        return inbound.parent_id if self.cfg.webex.reply_in_thread else ""

    async def _reply(
        self, inbound: "WebexInbound", text: str, *, self_minted: bool = False
    ) -> str | None:
        """Post a dispatcher-originated message; return its id.

        The single choke point for every send this dispatcher makes on its own
        behalf (acks, notices, prompts, command output) — as opposed to the
        renderer's, which owns the answer. Holding the client-liveness assert
        here is why the handlers below do not each repeat it.

        Takes the ENVELOPE rather than a room id: the room and the thread are both
        properties of the message being answered, and a signature that asks only
        for the room makes losing the thread the default at 30-odd call sites.

        Redacted HERE, at the choke point, for the same reason. Most of what this
        sends is our own copy, but not all of it: an options-card press echoes a
        MODEL-authored label, ``/sessions`` prints conversation titles that are the
        opening words of user messages, and a queue receipt quotes the message it
        queued. The display scan is a no-op on text with nothing to find — it only
        downgrades markup when the canonical form actually reveals a credential —
        so applying it to everything costs the intended formatting nothing and
        removes the question of which call site remembered.

        ``self_minted`` is the one exemption, and it exists because the scan is
        right about the text: a presigned dashboard URL looks exactly like the
        credential-bearing link the exfiltration redactor is built to catch, so
        redacting it delivers a login link that cannot log in. The exemption is
        legitimate only for a value THIS PROCESS minted — never for anything that
        passed through the model — and there is exactly one such caller. Named on
        the call rather than inferred, so an exempt send is visible in review.
        """
        assert self.client is not None, "WebexDispatcher.client must be set"
        return await self.client.send_message(
            inbound.room_id,
            text if self_minted else webex_display_safe(text),
            parent_id=self._reply_parent(inbound) or None,
        )

    async def handle_message(
        self,
        inbound: "WebexInbound",
        *,
        interpret_commands: bool = True,
        drain: bool = True,
    ) -> None:
        """Drive one authorized inbound Webex message through TurnDriver.

        ``interpret_commands=False`` is used by the drain: a queued ``/new`` is
        turn content the user typed mid-turn, so it must reach the model as
        literal text rather than executing a command on replay.

        ``drain=False`` is also the drain's, and it is what keeps the pump FLAT.
        The drain already loops, so letting the replayed turn start a drain of its
        own would nest one Python frame per burst — a sustained burst would grow
        the stack without bound. The outer loop owns the pumping.
        """
        assert self.client is not None, "WebexDispatcher.client must be set"
        # Inbound channels-governance gate (off-loop) — recheck per message so a
        # host-profile deny added after connect stops dispatch without a restart
        # (the startup gate only blocks CONNECTING). Silently drop on deny.
        permitted = await inbound_permitted("webex")
        email = inbound.person_email
        room_id = inbound.room_id
        text = inbound.text

        # ── Card press intercept ──
        # A press is not a message: it carries no text, so every path below would
        # read it as empty. Routed first, and by INDEX/nonce rather than by any
        # text it carries, so a crafted press cannot become an instruction.
        if inbound.card_inputs is not None:
            await self._handle_card_press(inbound, permitted=permitted)
            return

        # ── @mention strip ──
        # In a group space Webex only delivers messages that @mention the bot, and
        # it does NOT strip the mention from the text. Left in, every group turn
        # would start with the bot's own name as if the user had typed it — and
        # every path that READS the text would see it: this runs ahead of the
        # approval intercept and the command parse because "@Kiro 1" is an
        # approval answer and "@Kiro /new" is a command, and matching either
        # against the raw text fails in exactly the room where the mention is
        # mandatory.
        if inbound.room_type != ROOM_DIRECT:
            text = strip_bot_mention(text, self._bot_name())
            inbound = replace(inbound, text=text)

        # ── Approval reply intercept ──
        # Ahead of everything else, because the session semaphore is held for the
        # whole turn: an approval answer necessarily arrives while the session is
        # busy, and the steer path below would otherwise fold "1" into the prompt
        # as if it were a mid-turn instruction.
        route = _route_of(inbound)
        session_key = self._session_key(route)
        if _APPROVALS.has_pending(self._approval_key(route)):
            answered = await self._maybe_answer_approval(inbound, session_key, permitted=permitted)
            if answered:
                return
        if not permitted:
            return
        logger.info(
            "Webex inbound from %s: %d chars",
            email[:3] + "***" if email else "?",
            len(text or ""),
        )

        # ── Command intercept (no LLM session needed) ──
        if interpret_commands:
            if is_bare_mid_turn_override(text):
                await self._reply(
                    inbound,
                    "ℹ️ `/queue` and `/steer` need a message after them — "
                    "e.g. `/queue also check the logs`.",
                )
                return
            cmd = parse_command(text)
            if cmd == "new":
                self._conv.bump_gen(route)
                await self._reply(inbound, "✅ Started a fresh conversation.")
                return
            if cmd == "compact":
                self._conv.clear_awaiting(route)
                await self._handle_compact(inbound)
                return
            if cmd == "help":
                await self._reply(inbound, build_help_text())
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
            if cmd == "yolo":
                await self._handle_yolo(inbound)
                return
            if cmd == "dashboard":
                await self._handle_dashboard(inbound)
                return
            if cmd == "model":
                await self._handle_model(inbound)
                return
            if cmd == "sessions":
                await self._handle_sessions(inbound)
                return
            if is_unknown_command(text):
                # Answer with the card rather than spending a whole turn having
                # the model explain that it does not know what "/nwe" means.
                await self._reply(inbound, f"❓ Unknown command.\n\n{build_help_text()}")
                return

        # Busy check, then rotation, then a re-derived key -- ``resolve_pre_turn``
        # owns that sequence (messaging.pre_turn) and its ordering reasons. The
        # override mode is parsed BEFORE it so a ``/queue``/``/steer`` prefix on
        # the mid-turn message forces the busy path for THIS message; the shared
        # owner only knows the session key, so ``inbound``/``body``/``override_mode``
        # are captured in ``on_busy``. Keyed on the ROUTE (see ``_route_of``), not
        # the email, so a group space cannot land in the sender's private session.
        override_mode, body = parse_mid_turn_override(text) if interpret_commands else (None, text)
        # Distinct name: ``session_key`` above is the pre-rotation key the approval
        # intercept read (``str``); ``resolve_pre_turn`` returns ``str | None``
        # (None = folded into a running turn), so narrow before reusing the name.
        resolved_key = await resolve_pre_turn(
            conv=self._conv,
            sessions=self.sessions,
            key=route,
            session_key_for=self._session_key,
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
            on_busy=lambda sk: self._handle_busy(inbound, sk, body, override_mode),
        )
        if resolved_key is None:
            return  # folded into the running turn
        session_key = resolved_key
        conversation_id = f"webex:{route}"
        # Per-conversation key for the approval + choice registries, so a
        # collapsed unified session key cannot let one user answer another's
        # prompt. Room id is stable across rotation, and the session key is this
        # turn's, so reserve here and resolve (decider or busy-path intercept)
        # address the same entry within the turn.
        approval_scope = self._approval_key(route)
        agent = self._resolve_agent()
        # The SAME derivation the dispatcher's own sends use, so the answer and
        # every ack about it cannot end up in different places.
        reply_parent = self._reply_parent(inbound)

        # A decider only exists under INTERACTIVE: in auto/trust the driver's own
        # ladder approves without ever asking, and posting a prompt nobody needs
        # to answer would be noise. Without one the driver denies by default,
        # which is what made this channel effectively read-only.
        decider = (
            SessionApprovalDecider(_APPROVALS, session_key=approval_scope)
            if self.approval_mode == APPROVAL_INTERACTIVE
            else None
        )
        # Files become prompt material before the turn starts, so the agent sees
        # them as context rather than as a separate event. A message carrying
        # files is never steered (steer forwards TEXT ONLY and would drop every
        # file), which the busy path above already guarantees by queueing it.
        temp_paths: list[str] = []
        if inbound.file_urls:
            ingested = await process_webex_attachments(self.client, inbound)
            temp_paths = list(ingested.temp_paths)
            body = append_attachment_context(body, ingested)

        renderer = WebexRenderer(
            self.client,
            room_id,
            WEBEX_CAPABILITIES,
            thread_id=reply_parent,
            # A file posted into a space is readable by every member of it,
            # including people the email allow-list excludes, so a group turn keeps
            # printing the markdown path (the honest degradation) instead of
            # shipping bytes. Positive membership: a room type this code does not
            # know about does not inherit what a direct room gets.
            uploads_allowed=inbound.room_type == ROOM_DIRECT,
            # The registry both OPENS the decision window and mints its nonce, at
            # prompt-render time. Opening it here rather than in ``decide`` is what
            # makes an answer that arrives while the prompt is still being sent
            # resolvable — the driver dispatches the prompt and only then awaits
            # the decider. The nonce lives and dies with that window, so the guard
            # runs as a precondition of resolving rather than a check afterwards.
            mint_approval_nonce=lambda rid: _APPROVALS.reserve(approval_scope, rid),
            publish_choices=lambda nonce, choices: self._choices.publish(
                approval_scope, nonce, choices
            ),
        )

        # The turn skeleton (acquire -> identity -> origin/mirror bind -> context
        # -> TurnDriver -> guarded post-turn -> finally close/release) lives once
        # in messaging.dispatch. Only the webex-specific pieces are injected.
        # Immediately surface a newly-created channel session in the dashboard
        # (don't wait for the ~30s reconciler). Circular import — dashboard boot
        # imports channel packages — so import lazily.
        async def _surface_new_session() -> None:
            from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

            await surface_dispatcher_session(self)

        try:
            await drive_turn(
                ChannelTurn(
                    channel_type="webex",
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
                    user_text=body,
                    renderer=renderer,
                    # A GROUP space is a shared audience: the sender is allow-listed
                    # but the other members are not, and the operator's memory,
                    # lessons, skills and prior history are injected into the PROMPT
                    # before any tool runs -- so a full-context turn could quote
                    # the operator's private notes into a room they do not control.
                    # Build a space turn WITHOUT that context (parity with the
                    # WhatsApp/Feishu group path); a direct DM keeps it.
                    minimal_context=inbound.room_type != ROOM_DIRECT,
                    approval_mode=self.approval_mode,
                    decider=decider,
                    # Read per request, not captured here, so a grant taken or
                    # revoked mid-turn takes effect on the next tool.
                    auto_approve_session=lambda: safety_override().is_active(),
                    origin_conversation=self._origin_mirror_link(room_id),
                    # The upload root has to be the provider's OWN resolved cwd, so
                    # extraction is confined to the directory this session actually
                    # works in. Read through the pipeline's hook rather than from
                    # the session map up here: on the FIRST turn of a generation no
                    # session exists yet, so reading it early leaves uploads off
                    # for exactly that turn. Without a root they stay off.
                    bind_provider=lambda p: renderer.authorize_upload_root(
                        getattr(p, "cwd", "") or ""
                    ),
                    persist=lambda user_text, reply, is_new: self._persist_turn(
                        session_key, user_text, reply, is_new, agent
                    ),
                    notice=lambda sk, provider: self._maybe_notice(inbound, sk, provider),
                    model=self._model_pref.get(email) or None,
                    audit_caller=f"webex:{email}",
                    after_persist=_surface_new_session,
                ),
                sessions=self.sessions,
                ctx_builder=self.ctx_builder,
            )
        finally:
            # A reservation the driver never awaited (the prompt rendered, then the
            # turn failed before the decider) would otherwise outlive this turn and
            # be resolved by a stray answer to a later prompt.
            _APPROVALS.discard_reservations(approval_scope)
            if temp_paths:
                # Off-loop: unlinking a run of files is blocking filesystem work,
                # and this runs on the shared gateway loop.
                await asyncio.to_thread(cleanup_attachments, temp_paths)
        # drive_turn released the semaphore in its finally, so the session is free
        # and anything queued during the turn can run now.
        if drain:
            await self._drain_queue(inbound, session_key)

    async def _handle_busy(
        self,
        inbound: "WebexInbound",
        session_key: str,
        text: str,
        override_mode: str | None = None,
    ) -> None:
        """A message arrived mid-turn: queue it for after, or steer the turn.

        ``override_mode`` (``"queue"`` / ``"steer"`` / ``None``) forces the path
        for THIS message, overriding ``messaging.queue_mode``.

        ``is_busy`` stays True through post-turn bookkeeping, so it alone cannot
        tell a live turn from one that just finished. Gate steer on
        ``has_active_turn`` (parity with Telegram/WeCom): steering a prompt that
        already ended would falsely acknowledge a merge.
        """
        if not self.sessions.is_busy(session_key):
            await self.handle_message(inbound)
            return
        mode = override_mode or self.cfg.messaging.queue_mode
        # Steer forwards TEXT ONLY, so steering a message that carries files would
        # acknowledge a fold while silently dropping every attachment. Queue it
        # instead — even under an explicit ``/steer`` — so the files reach the
        # agent one turn later rather than never.
        if inbound.file_urls:
            mode = "queue"
        if mode != "queue":
            provider = self.sessions.get_provider(session_key)
            steer = getattr(provider, "steer", None)
            has_active = getattr(provider, "has_active_turn", None)
            live = has_active is None or bool(has_active())
            steered = bool(
                live
                and getattr(provider, "supports_steer", False)
                and steer is not None
                and await steer(text)
            )
            if steered:
                await self._reply(inbound, "⏳ Folded into the reply in progress.")
                return
        # Queue mode, a /queue override, or steer was unavailable. The enqueue and
        # its receipt happen atomically under the queue lock so the end-of-turn
        # drain — which takes the same lock to dequeue and flip — cannot
        # interleave and orphan a receipt bubble.
        if not await self._enqueue_with_receipt(session_key, text, inbound):
            # The turn finished inside the window, so ``enqueue`` was a no-op.
            # Running it now is safe: is_busy is False, so there is no re-entry
            # loop, and stranding the message would lose it silently.
            await self.handle_message(inbound)

    # ── Approvals (typed reply) ────────────────────────────────────────────

    async def _maybe_answer_approval(
        self, inbound: "WebexInbound", session_key: str, *, permitted: bool
    ) -> bool:
        """Try to read *inbound* as the answer to a pending approval prompt.

        Returns whether the message was consumed. A message that is not a
        recognised answer falls through to the ordinary mid-turn path, so a user
        who ignores the prompt and sends a real instruction still gets it
        steered or queued rather than swallowed.

        A channels-governance deny blocks an APPROVE but still resolves a DENY.
        Dropping the deny too would leave the provider's permission request
        stranded for the whole approval window with the turn holding the
        semaphore — and a policy that forbids this channel has no interest in
        keeping a tool request alive.
        """
        answer = (inbound.text or "").strip().lower()
        approved = answer in _APPROVE_REPLIES
        denied = answer in _DENY_REPLIES
        if not approved and not denied:
            return False
        if approved and not permitted:
            logger.info("Webex: approve reply dropped by channels governance policy")
            return True
        resolved = _APPROVALS.resolve(self._approval_key(_route_of(inbound)), approved)
        sel().log_api_access(
            caller=f"webex:{inbound.person_email}",
            operation="webex.tool_approval",
            outcome="approved" if approved else "denied",
            source="webex",
            resources=f"session={session_key} resolved={resolved}",
        )
        if not resolved:
            await self._reply(inbound, _APPROVAL_NOT_PENDING)
            return True
        await self._reply(inbound, "✅ Approved." if approved else "🚫 Denied.")
        return True

    # ── /model (pick from what the backend advertised) ─────────────────────

    def _model_choices(self, session_key: str) -> list[tuple[str, str]]:
        """``(model_id, label)`` rows this session may actually use.

        The ONLY source is what the session's backend advertised at
        ``session/new`` — the set THIS account is entitled to, carrying the
        backend's own ids. A static catalogue would offer models the account
        cannot reach, which surfaces as a refusal mid-conversation, and its
        display keys would need per-backend translation before the wire.
        """
        rows: list[tuple[str, str]] = [("", "Auto (let the backend choose)")]
        provider = self.sessions.get_provider(session_key)
        advertised = getattr(provider, "available_models", None)
        if not callable(advertised):
            return rows
        try:
            entries = [m for m in advertised() if isinstance(m, dict)]
        except Exception:
            logger.warning("Webex /model: available_models failed", exc_info=True)
            return rows
        for entry in entries:
            model_id = str(entry.get("modelId") or "").strip()
            # "auto" is already the first row; listing it twice offers the same
            # choice two numbers.
            if not model_id or model_id == "auto":
                continue
            rows.append((model_id, str(entry.get("name") or model_id)))
        return rows

    async def _handle_model(self, inbound: "WebexInbound") -> None:
        """List the advertised models, or apply a numbered pick.

        Numbered rather than free-text: a model id is not something the user can
        enumerate, and a typo would land as a rejected ``set_model`` mid-
        conversation. The number indexes the list this reply printed.
        """
        email = inbound.person_email
        route = _route_of(inbound)
        session_key = self._session_key(route)
        choices = self._model_choices(session_key)
        arg = parse_command_argument(inbound.text)
        if not arg:
            current = self._model_pref.get(email, "")
            lines = [
                f"**Model** — currently {self._model_label(choices, current)}",
                "",
            ]
            lines += [
                f"{index}. {label}{' ·  current' if mid == current else ''}"
                for index, (mid, label) in enumerate(choices, start=1)
            ]
            if len(choices) == 1:
                lines.append("")
                lines.append("_No model list yet — send a message first, then `/model`._")
            lines += ["", "Reply `/model <number>` to switch."]
            await self._reply(inbound, "\n".join(lines))
            return
        if not arg.isdigit() or not 1 <= int(arg) <= len(choices):
            await self._reply(
                inbound,
                f"❌ Pick a number between 1 and {len(choices)} — send `/model` for the list.",
            )
            return
        model_id, label = choices[int(arg) - 1]
        await self._reply(inbound, await self._apply_model(inbound, model_id, label))

    async def _apply_model(self, inbound: "WebexInbound", model_id: str, label: str) -> str:
        """Record *model_id* for the caller and push it to the live session.

        *model_id* comes verbatim from the session's advertised list, so it is
        already the id this backend accepts — no canonical translation, which
        would differ per backend and could mangle an id that was correct.

        The preference is stored unconditionally so it reaches the NEXT session
        even when there is nothing live to switch. When a session does exist the
        switch is attempted IN PLACE: ``session/set_model`` carries the
        conversation across, so a user who picks a model mid-conversation gets the
        model they picked rather than a promise about their next one. The session
        semaphore is taken atomically, because the switch and a turn share one
        stdio channel and interleaving JSON-RPC on it would corrupt both.

        Returns the user-facing outcome line — every branch says what actually
        happened, since a claimed switch the user cannot verify is worse than a
        deferral they can act on.
        """
        # The preference is per PERSON: which model to use is an individual
        # choice, while the conversation it applies to may be a shared space.
        self._model_pref[inbound.person_email] = model_id
        session_key = self._session_key(_route_of(inbound))
        live = self.sessions.has_session(session_key)
        # Two different promises, because the preference reaches a session only at
        # CREATION: ``get_or_create`` returns a reused session from its fast path
        # before it consults ``model=``. With nothing live the next message starts
        # the session, so it genuinely lands then.
        deferred = f"✅ Model set to **{label}** — it applies to your next message."
        next_new = (
            f"✅ Model set to **{label}** — this conversation keeps its current "
            f"model; the switch applies to your next one (`/new`)."
        )
        # "Auto" has no ACP id meaning "let the backend choose", so it can only be
        # recorded; the next session start resolves it. Claiming a live switch here
        # would be a lie.
        if not model_id:
            return next_new if live else deferred
        if not live:
            return deferred
        if not await self.sessions.try_acquire(session_key):
            return (
                f"✅ Model set to **{label}**, but a reply is still running — this "
                f"conversation keeps its current model; the switch applies to your "
                f"next one (`/new`)."
            )
        try:
            provider = self.sessions.get_provider(session_key)
            set_model = getattr(getattr(provider, "client", None), "set_model", None)
            if set_model is None:
                return next_new
            await set_model(model_id)
        except Exception as exc:
            logger.warning(
                "Webex /model: live set_model failed for %s: %s",
                session_key,
                type(exc).__name__,
                exc_info=True,
            )
            # The stored preference still stands, so the next session gets it —
            # but do not claim the running conversation switched when it did not.
            return (
                f"⚠️ Couldn't switch this conversation to **{label}** "
                f"({type(exc).__name__}) — it applies to your next conversation "
                f"(`/new`)."
            )
        finally:
            self.sessions.release(session_key)
        return f"✅ Now using **{label}**."

    @staticmethod
    def _model_label(choices: list[tuple[str, str]], model_id: str) -> str:
        """The label for a stored preference, falling back to the raw id."""
        return next((label for mid, label in choices if mid == model_id), model_id or "Auto")

    # ── /sessions (this channel's own conversations) ────────────────────────

    def _bucket_stem(self, route: str) -> str:
        """The filename stem shared by every generation of *route*'s conversation.

        ``list_sessions`` reports a key as the transcript's filename STEM, which
        is the session key with every character outside ``[\\w\\-.]`` folded to
        ``_`` — so a colon-bearing session key never matches a raw prefix test
        against it, and a filter written that way silently lists nothing at all.
        Folded through ``transcript_stem`` so this side uses the store's own rule
        rather than a second copy of it.
        """
        bucket = build_dm_session_key(
            "webex",
            self._resolve_agent(),
            route,
            gen=0,
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=_chat_type_of(route),
        )
        return transcript_stem(bucket)

    async def _handle_sessions(self, inbound: "WebexInbound") -> None:
        """List THIS conversation's past generations, newest first.

        Scoped to the caller's own durable bucket — their DM, or the space the
        command was sent in — and not to the channel. A channel-wide list would
        print one person's conversation TITLES, which are the opening words of
        their messages, into whatever room asked; scoping to the bucket means the
        audience of the list is exactly the audience of the conversations in it.

        Deliberately not a dashboard-session picker: resuming one of those needs
        the durable resume-expectation store and an inbound path that resolves the
        mirror binding, which is why ``supports_session_resume`` is Discord-only
        and the capability ledger pins it that way.
        """
        if self.conv_log is None:
            await self._reply(inbound, "ℹ️ Conversation history is not available.")
            return
        route = _route_of(inbound)
        stem = self._bucket_stem(route)
        try:
            rows = [
                row
                for row in await asyncio.to_thread(self.conv_log.list_sessions)
                # The bucket itself (generation 0) or one of its generations. The
                # separator is required so a bucket cannot match a longer route
                # that merely starts with the same characters.
                if (key := str(row.get("key", ""))) == stem or key.startswith(f"{stem}_gen")
            ]
        except Exception:
            logger.warning("Webex /sessions: listing failed", exc_info=True)
            await self._reply(inbound, "⚠️ Couldn't read the conversation list.")
            return
        sel().log_api_access(
            caller=f"webex:{inbound.person_email}",
            operation="webex.sessions_list",
            outcome="success",
            source="webex",
            resources=f"bucket={stem} rows={len(rows)}",
        )
        if not rows:
            await self._reply(inbound, "ℹ️ No earlier conversations here yet.")
            return
        current = transcript_stem(self._session_key(route))
        lines = ["**This conversation's history**", ""]
        for row in rows[:_SESSIONS_LIST_MAX]:
            title = str(row.get("title") or "").strip() or "(untitled)"
            marker = " ·  current" if str(row.get("key", "")) == current else ""
            lines.append(f"- {title}{marker}")
        if len(rows) > _SESSIONS_LIST_MAX:
            lines.append(f"- _…and {len(rows) - _SESSIONS_LIST_MAX} older_")
        lines += ["", "Send `/new` to start a fresh one."]
        await self._reply(inbound, "\n".join(lines))

    # ── Adaptive Card presses ──────────────────────────────────────────────

    async def _handle_card_press(self, inbound: "WebexInbound", *, permitted: bool) -> None:
        """Route an Adaptive Card submit to whatever it was rendered for.

        A press is authorized the same way a message is — by the sender's EMAIL
        against the allow-list — because the card lives in a room and Webex lets
        anyone in that room press it. The transport's room gate has already run;
        this adds the sender check, which for a press has to resolve a person id
        to an email first.

        The nonce is what makes a press safe to honour at all: Webex will not let
        a card carrying an attachment be edited into a terminal state, so a
        resolved prompt's buttons stay clickable forever. Only the live card's
        nonce is accepted, so a later press is inert instead of answering
        whatever prompt is open now. It is validated INSIDE the approval registry,
        as a precondition of resolving — checking it here, around the resolve call,
        would approve the tool first and only then discover the press was stale.
        """
        kind, choice, nonce, request_id = read_press(inbound.card_inputs)
        if not kind:
            return
        if not self._sender_allowed(inbound.person_email):
            sel().log_api_access(
                caller=inbound.person_email or "unknown",
                operation="webex.card_press",
                outcome="denied",
                source="webex",
                # Bounded and quoted: ``kind`` is attacker-chosen here (the press
                # came from a sender this branch is refusing), and an audit record
                # is read by an operator and by log tooling. An unbounded value can
                # pad the record, and an unescaped newline can forge a second
                # line in it.
                resources=f"kind={kind[:32]!r}",
            )
            return
        if kind == cards.KIND_APPROVAL:
            approved = choice == "approve"
            # Governance may block an approve while still letting a deny through:
            # a policy that forbids this channel has no interest in keeping a tool
            # request alive for its whole window.
            if approved and not permitted:
                logger.info("Webex: card approve dropped by channels governance policy")
                return
            await self._resolve_card_approval(inbound, approved, nonce, request_id)
            return
        if kind == cards.KIND_OPTIONS:
            # A choice is turn CONTENT, so a policy-denied channel drops it whole
            # rather than echoing it. Unlike an approval there is nothing waiting
            # to be released: `drive_turn` would silently drop the turn anyway, and
            # the echo alone would claim the channel is answering.
            if not permitted:
                logger.info("Webex: card choice dropped by channels governance policy")
                return
            text = self._choices.take(self._approval_key(_route_of(inbound)), choice, nonce)
            if not text:
                await self._reply(inbound, "⌛ Those options are no longer current — just reply.")
                return
            # Echo the choice first: a press leaves no trace in the room, so
            # without this the next answer arrives with nothing above it saying
            # which option it is answering.
            await self._reply(inbound, f"> {text}")
            # Run the chosen option as turn CONTENT, never as a command. The label
            # is model-authored, so an `[OPTIONS: Keep going | /yolo on]` trailer
            # would otherwise render a button whose single press takes the
            # process-global auto-approve grant — and more commonly, any label
            # whose first token merely looks like `/…` would be answered with the
            # unknown-command card and the choice dropped on the floor.
            await self.handle_message(
                replace(inbound, text=text, card_inputs=None), interpret_commands=False
            )

    async def _resolve_card_approval(
        self, inbound: "WebexInbound", approved: bool, nonce: str, request_id: str
    ) -> None:
        """Apply one authorized approval-card press, and say what happened.

        Every path here emits a SEL record. A press that is refused is exactly the
        event an operator needs in the audit log — a forged or replayed press
        leaves no other trace, since the reply it draws is indistinguishable from
        the one a genuinely expired card draws.
        """
        session_key = self._session_key(_route_of(inbound))
        approval_scope = self._approval_key(_route_of(inbound))
        # Fail CLOSED on a press that carries no nonce or no request id. Both are
        # minted by us and every real press echoes them back, so their absence is
        # either a forgery or a card from a build that predates them; an empty
        # nonce means "typed answer" to the registry and would skip the guard.
        if not nonce or not request_id:
            outcome, resolved = "denied", False
        else:
            resolved = _APPROVALS.resolve(
                approval_scope, approved, request_id=request_id, expected_nonce=nonce
            )
            # Only a resolve that actually landed can record an approval: a press
            # the registry refused (stale nonce, wrong request id) decided nothing.
            outcome = "approved" if resolved and approved else "denied"
        sel().log_api_access(
            caller=f"webex:{inbound.person_email}",
            operation="webex.tool_approval",
            outcome=outcome,
            source="webex",
            resources=f"session={session_key} via=card resolved={resolved}",
        )
        if resolved:
            await self._reply(inbound, "✅ Approved." if approved else "🚫 Denied.")
            return
        # A prompt still waiting means the CARD went stale, not the decision — so
        # point at the affordance that still works instead of claiming the request
        # is over, which would leave the turn hanging with no way to answer it.
        if _APPROVALS.has_pending(approval_scope):
            await self._reply(
                inbound,
                "⌛ That card is no longer current — reply **1** to approve or **2** to deny.",
            )
            return
        await self._reply(inbound, _APPROVAL_NOT_PENDING)

    def _sender_allowed(self, email: str) -> bool:
        """Whether *email* is on the channel's allow-list. Deny-by-default.

        Re-derived here rather than read off the transport because a press does
        not flow through ``receive``; an empty allow-list authorizes nobody.
        """
        allowed = {e.lower() for e in (self.cfg.webex.allowed_emails or []) if e}
        return bool(email) and email.lower() in allowed

    def _bot_name(self) -> str:
        """The bot's display name, for stripping its own @mention in a space."""
        client = self.client
        return getattr(client, "bot_name", "") if client is not None else ""

    # ── Mid-turn queue receipt (single, in-place, persistent record) ────────

    def _receipt_surface(self, inbound: "WebexInbound") -> ReceiptSurface:
        """A receipt surface with this conversation's address already bound.

        The receipt lives on its OWN message, so its edits draw from that
        message's own 10-edit allowance and never compete with the answer
        placeholder's reserved final edit. It threads with the turn it is a
        receipt FOR: an edit needs no parent, but the first send does, or the
        bubble tracking a threaded turn appears in the room root.
        """
        # cast, not assert: mypy does not carry an assert-narrowed local into the
        # nested class body below, so the closure would still see
        # ``WebexClient | None``. The caller path always has a live client.
        client = cast("WebexClient", self.client)
        room_id = inbound.room_id
        parent_id = self._reply_parent(inbound) or None

        class _Surface:
            label = "webex"

            async def send_receipt(self, body: str) -> Any | None:
                # A receipt quotes the message it queued, so it carries user text
                # and needs the same display scan the answer gets.
                return await client.send_message(
                    room_id, webex_display_safe(body), parent_id=parent_id
                )

            async def edit_receipt(self, msg_id: Any, body: str) -> None:
                await client.edit_message(str(msg_id), room_id, webex_display_safe(body))

        return _Surface()

    async def _enqueue_with_receipt(
        self, session_key: str, text: str, inbound: "WebexInbound"
    ) -> bool:
        """Atomically enqueue a mid-turn message and create/grow its receipt.

        Holding the lock across BOTH the enqueue and the receipt bookkeeping is
        what makes this race-free against the end-of-turn drain (which takes the
        same lock to dequeue + flip). Returns True if queued; False if the turn
        finished in the window, so the caller runs the message as a fresh turn.

        The attachment URLS ride the entry, not the downloaded bytes: a Webex file
        lives as long as the message that carries it, so the drained turn can fetch
        it then. Ingesting here would hold temp files for the whole of the running
        turn and put their cleanup on every discard path (cancel, ``/stop``, the
        cancelled-skip inside ``dequeue``).
        """
        async with self._queue.lock:
            if not self.sessions.enqueue(
                session_key,
                str(time.time()),
                text,
                force=False,
                webex_file_urls=list(inbound.file_urls),
                # The room this message arrived in rides the entry, because the
                # drain replays it and the reply goes wherever the replayed
                # envelope points. Under ``dm_scope="unified"`` two allowed people
                # share ONE session key (and therefore one queue), so without this
                # a message queued by B during A's turn would be answered into A's
                # room -- B's text disclosed to A, and B's answer never delivered.
                webex_room_id=inbound.room_id,
                webex_person_email=inbound.person_email,
                # The THREAD too, not just the room: with
                # ``webex.reply_in_thread`` the reply parent is the inbound's own
                # ``parent_id``, and two threads share one room id — so a room-only
                # envelope would answer thread B inside thread A.
                webex_parent_id=inbound.parent_id,
            ):
                return False
            await self._queue.create_or_grow_locked(
                session_key,
                self._receipt_surface(inbound),
                # An uncaptioned attachment has no text at all; the receipt still
                # has to show the user that SOMETHING was received.
                text or _QUEUED_ATTACHMENT_LABEL,
            )
            return True

    async def _drain_queue(self, inbound: "WebexInbound", session_key: str) -> None:
        """Answer everything queued during the just-finished turn.

        Collapses up to ``_MAX_COLLAPSE`` messages into ONE combined turn (order
        preserved, blank-line joined) rather than replaying each separately, and
        re-enqueues the surplus IN ORDER so FIFO stays exact.

        Iterates rather than recurses: one burst can span several capped turns,
        and a deferred message must drain in THIS pump rather than waiting for
        unrelated future input.
        """
        while True:
            texts: list[str] = []
            files: list[str] = []
            remainder: list[tuple[str, str, dict]] = []
            # The CONVERSATION this drained turn answers into: room AND thread
            # root, because that pair is what the reply envelope carries. Taken
            # from the FIRST entry rather than from *inbound*, whose turn may have
            # been opened by another person (a shared unified key puts two humans
            # on one queue) or in another thread.
            convo: tuple[str, str] | None = None
            email = ""
            async with self._queue.lock:
                while True:
                    item = self.sessions.dequeue(session_key)
                    if item is None:
                        break
                    item_convo = (
                        str(item[2].get("webex_room_id") or ""),
                        str(item[2].get("webex_parent_id") or ""),
                    )
                    if convo is None:
                        convo = item_convo
                        email = str(item[2].get("webex_person_email") or "")
                    # Collapse only messages from the SAME conversation -- same
                    # room AND same thread root. One combined turn gets ONE
                    # envelope, so mixing either would answer text into a chat or a
                    # thread it did not come from: a different room is reachable
                    # whenever a unified key puts two humans on one queue, and a
                    # different thread whenever ``reply_in_thread`` is on. Anything
                    # else defers itself AND everything behind it, so FIFO stays
                    # exact and the outer loop drains it next as its own turn in its
                    # own envelope.
                    if len(texts) < _MAX_COLLAPSE and item_convo == convo:
                        texts.append(item[1])
                        # Collapsed messages contribute their attachments too, in
                        # order, so a burst of "here, and here" screenshots all
                        # reach the one turn that answers them.
                        files.extend(str(u) for u in (item[2].get("webex_file_urls") or []))
                    else:
                        # Once one message no longer fits, defer it AND
                        # everything behind it, so queue order stays exact.
                        remainder.append(item)
                for _ts, rtext, rkw in remainder:
                    # ``**rkw`` and not a bare re-enqueue: a deferred entry must
                    # keep its attachments, or a burst past the collapse cap
                    # silently loses the files of everything after the cap.
                    self.sessions.enqueue(session_key, str(time.time()), rtext, force=True, **rkw)
                if texts:
                    await self._queue.flip_answering_locked(
                        session_key,
                        self._receipt_surface(inbound),
                        texts,
                        len(remainder),
                    )
            if not texts:
                return
            if remainder:
                logger.debug(
                    "Webex: drain deferred %d message(s) for %s to respect the "
                    "collapse cap (%d); they drain in the next iteration of this pump",
                    len(remainder),
                    session_key,
                    _MAX_COLLAPSE,
                )
            # A copy, not a mutation: the queued original is still referenced by
            # the log line above. ``replace`` carries every field, so a new one
            # added to WebexInbound cannot be silently dropped on this path.
            #
            # ``file_urls`` is REPLACED unconditionally, including with an empty
            # tuple. *inbound* is the message that OPENED the finished turn, so
            # inheriting its attachments would re-download and re-summarize files
            # the agent has already been shown, once per drain iteration.
            # The envelope comes from the QUEUED messages, not from *inbound*: the
            # reply is delivered to whatever this carries, and on a shared unified
            # key the finished turn's opener can be a different person in a
            # different room. Falls back to *inbound* only when an entry predates
            # this field (a queue persisted by an older build).
            drained = replace(
                inbound,
                text="\n\n".join(texts),
                file_urls=tuple(files),
                room_id=(convo[0] if convo and convo[0] else inbound.room_id),
                parent_id=(convo[1] if convo else inbound.parent_id),
                person_email=email or inbound.person_email,
            )
            # interpret_commands=False: drained payloads are pure turn content, so
            # a queued "/new" must reach the model as literal text rather than
            # executing as a command on replay.
            # drain=False: this loop is the pump. Letting the replayed turn drain
            # too would nest a frame per burst and re-enter with the queue in a
            # state this iteration has already read.
            await self.handle_message(drained, interpret_commands=False, drain=False)

    # ── Commands ───────────────────────────────────────────────────────────

    async def _handle_stop(self, inbound: "WebexInbound") -> None:
        """Hard cancel: abort the in-flight turn and clear the queue.

        The cancel is cooperative (ACP cannot force-kill a co-tenant), so the
        turn stops at the next safe point; the ack is sent without waiting for it
        so it stays snappy.
        """
        session_key = self._session_key(_route_of(inbound))
        cancelled_turn = False
        if self.sessions.is_busy(session_key):
            provider = self.sessions.get_provider(session_key)
            # ``cancel`` is declared on the LLMProvider ABC, so the guard is for
            # a session with no live provider, not for a provider missing it.
            if provider is not None:
                try:
                    await provider.cancel(wait_ack_timeout=0)
                    cancelled_turn = True
                except Exception:
                    logger.warning("Webex /stop: cancel failed for %s", session_key, exc_info=True)
        async with self._queue.lock:
            self.sessions.clear_queue(session_key)
            await self._queue.finish_cancelled_locked(session_key, self._receipt_surface(inbound))
        await self._reply(
            inbound,
            "🛑 Stopped." if cancelled_turn else "🛑 Nothing was running — queue cleared.",
        )

    def _origin_mirror_link(self, room_id: str) -> ChannelLink:
        """The mirror location for the room a conversation is being read in.

        ONE definition shared by the automatic bind (through
        ``ChannelTurn.origin_conversation``), ``/link`` and ``/unlink``: a
        release matches an occupied location by VALUE, so a second spelling of
        "this room" would let the unlink miss the binding the bind wrote.

        The ROOM id, not the ``webex:{email}`` attribution bucket — the room is
        what a send actually addresses.
        """
        return ChannelLink("webex", channel_id=room_id, thread_id=None)

    async def _handle_link(self, inbound: "WebexInbound") -> None:
        """Re-enable mirroring of this conversation's dashboard tab back here.

        Mirroring is automatic, so this is the withdrawal of a previous
        ``/unlink`` rather than the only way to turn it on. Clearing the opt-out
        is the load-bearing half: rebinding without it would be undone by the
        next automatic bind check.
        """
        key = self._session_key(_route_of(inbound))
        link = self._origin_mirror_link(inbound.room_id)

        # One write for the whole sequence (``batched_save`` rewrites the session
        # map once on the way out), and off-loop because that rewrite is blocking
        # I/O -- same posture as ``_handle_yolo``'s mutators and the turn-path
        # bind. Each mutation would otherwise rewrite the entire map, stalling the
        # loop three times for one user-visible action.
        def _rebind() -> None:
            with self.sessions.batched_save():
                self.sessions.set_mirror_opt_out(key, False)
                self.sessions.set_mirror_link(key, link, reason=UNBIND_REASON_ORIGIN_REBIND)
                # Drop any pre-unification row so a stale binding cannot outlive
                # the rebind (reads prefer the channel key, but a leftover row
                # would still answer a clear).
                self.sessions.clear_mirror_link(
                    legacy_dashboard_mirror_key(key), reason=UNBIND_REASON_ORIGIN_REBIND
                )

        await asyncio.to_thread(_rebind)
        await self._reply(
            inbound,
            "✅ Linked. Replies from the dashboard for this conversation will "
            "also show up here. Send `/unlink` to stop.",
        )

    async def _handle_unlink(self, inbound: "WebexInbound") -> None:
        key = self._session_key(_route_of(inbound))

        # Persist the refusal BEFORE releasing: mirroring is re-asserted on every
        # inbound turn, so a release alone would be undone by the user's next
        # message. One batched write, off-loop because it rewrites the session map
        # (blocking I/O) -- same posture as /link and the turn-path bind.
        def _unbind() -> str:
            with self.sessions.batched_save():
                self.sessions.set_mirror_opt_out(key, True)
                reply, _swept = release_conversation_location(
                    self.sessions,
                    key=key,
                    location=self._origin_mirror_link(inbound.room_id),
                    channel="webex",
                )
            return reply

        reply = await asyncio.to_thread(_unbind)
        await self._reply(inbound, reply)

    async def _handle_yolo(self, inbound: "WebexInbound") -> None:
        """Report or change the global auto-approve grant.

        Reads and writes the process-wide :func:`safety_override` grant — the
        SAME one the dashboard toggle and the other channels drive, so a grant
        taken here shows up (and expires) everywhere. Reachable only by an
        allow-listed Webex user, because ``transport.receive`` is deny-by-default
        before dispatch ever runs.

        Turning it on does NOT weaken the PreToolUse security gate: the
        sensitive-path keystone, governance ceiling and deny-list all run ahead
        of the auto-approve ladder in ``TurnDriver``, so a hard DENY still wins.

        The three grant mutators run off-loop: ``activate`` resolves the ad-hoc
        duration through a live config read and each writes a SEL record, so
        calling them inline would put filesystem latency on the event loop.
        """
        so = safety_override()
        arg = parse_command_argument(inbound.text)
        action = arg.lower().split()[0] if arg else ""

        if action in ("on", "off", "renew"):
            outcome = "allowed"
            if action == "on":
                if so.is_active():
                    reply = f"🟢 YOLO is already ON ({describe_grant_lifetime()})."
                elif (await asyncio.to_thread(so.activate, "webex")).active:
                    reply = (
                        f"🟢 YOLO ON ({describe_grant_lifetime()}) — every tool "
                        f"auto-approves. Denied-by-policy tools are still blocked."
                    )
                else:
                    reply = "❌ Couldn't turn YOLO on (audit system unavailable)."
                    outcome = "denied"
            elif action == "off":
                # Unconditional: deactivate() also zeroes the deadline of a grant
                # that already lapsed, which closes the renew grace window so a
                # later "/yolo renew" cannot resurrect it, and records the
                # operator's decision either way.
                await asyncio.to_thread(so.deactivate, "webex")
                reply = "🔴 YOLO OFF — tools ask for approval again."
            else:
                renewed = (await asyncio.to_thread(so.renew, "webex")).renewed
                reply = (
                    f"🟢 YOLO renewed ({describe_grant_lifetime()})."
                    if renewed
                    else "🔴 YOLO is not active — use `/yolo on` first."
                )
            sel().log_api_access(
                caller=f"webex:{inbound.person_email}",
                operation="webex.yolo_mode",
                outcome=outcome,
                source="webex",
                resources=f"yolo_{action}",
            )
            await self._reply(inbound, reply)
            return

        status = f"ON 🟢 ({describe_grant_lifetime()})" if so.is_active() else "OFF 🔴"
        await self._reply(
            inbound,
            f"YOLO is {status}.\nUsage: `/yolo on | off | renew`",
        )

    async def _handle_dashboard(self, inbound: "WebexInbound") -> None:
        """Generate and send a presigned dashboard login link.

        Calls ``generate_token`` directly (never via shell) and builds the URL
        from the ``dashboard.url`` config (``KIROCREW_PORT`` overrides the port,
        matching every other link producer).

        The direct-room assertion is kept explicit rather than inherited: a
        presigned link is a credential and every member of a space can read it,
        while ``transport.receive``'s room gate deliberately DOES admit an
        allow-listed space. So this command needs its own narrower rule, not that
        layer's.
        """
        # Deferred deliberately: the dashboard package drags in ~370 transitive
        # modules, and this command is the only thing in the channel that needs
        # it. At module scope every gateway that merely enables Webex would pay
        # that import.
        from kiro_crew.dashboard.token_auth import (
            MAX_SESSION_TTL_SECS,
            generate_token,
            parse_duration,
        )
        from kiro_crew.dashboard.urls import dashboard_origin, parse_dashboard_url

        # Positive test against the named constant, not ``!= "direct"``: a Webex
        # room type this code has not seen must not inherit what a direct room
        # gets, and a presigned dashboard login is the worst thing to hand a room
        # whose audience nobody enumerated.
        if inbound.room_type != ROOM_DIRECT:
            await self._reply(
                inbound,
                "🔒 Dashboard links are only sent in a direct message.",
            )
            return
        arg = parse_command_argument(inbound.text).lower()
        if not arg.startswith("dashboard"):
            await self._reply(
                inbound,
                "Usage: `/kirocrew dashboard [<N>h|<N>m]`",
            )
            return
        # parse_duration owns the <N>h / <N>m grammar and already clamps to the
        # server maximum; the outer min() keeps the clamp explicit at the call
        # site. A malformed duration falls back rather than erroring: the user
        # asked for a link, and refusing one over a typo trades a working link
        # for a lecture.
        ttl_secs = min(parse_duration(_ttl_spec(arg)) or _DEFAULT_TTL_SECS, MAX_SESSION_TTL_SECS)
        caller = f"webex:{inbound.person_email}"
        try:
            token = generate_token(inbound.person_email, ttl_seconds=ttl_secs)
            origin = dashboard_origin(self.cfg.dashboard.url)
            if not origin:
                # No configured dashboard.url: fall back to the local port
                # (parse_dashboard_url applies the KIROCREW_PORT override).
                _, port = parse_dashboard_url(self.cfg.dashboard.url)
                origin = f"http://localhost:{port}"
            # Credential issuance MUST be audited (backend-security-controls).
            sel().log_api_access(
                caller=caller,
                operation="webex.dashboard_token",
                outcome="ok",
                source="webex",
                resources=f"ttl={ttl_secs}",
            )
            # self_minted: the URL carries a token THIS process just generated, and
            # it looks exactly like the credential-bearing link the exfiltration
            # redactor exists to catch — scanning it would deliver a login link
            # that cannot log in. Nothing here passed through the model.
            await self._reply(
                inbound,
                f"🔗 Dashboard link (valid {_format_ttl(ttl_secs)}):\n" f"{origin}/?token={token}",
                self_minted=True,
            )
        except Exception as exc:
            logger.warning("Webex /kirocrew dashboard: token generation failed", exc_info=True)
            try:
                sel().log_api_access(
                    caller=caller,
                    operation="webex.dashboard_token",
                    outcome="error",
                    source="webex",
                    resources=f"ttl={ttl_secs}",
                )
            except Exception:
                # The audit trail must never turn a user-facing failure reply
                # into a crash; the warning above already captured the error.
                pass
            await self._reply(
                inbound,
                f"⚠️ Could not generate dashboard link: {type(exc).__name__}",
            )

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def current_session_key(self, email: str) -> str:
        """This user's CURRENT DM session key, as the turn path derives it.

        Public because an out-of-band injector (the AutoNudge fire path) must be
        able to tell whether the conversation it is bound to still exists: a
        ``/new`` bumps the generation, and firing into the rotated key would run a
        synthetic turn in a fresh session with none of the loop's context.

        A GROUP SPACE is not addressable here, by design: its conversation belongs
        to the space, so it is keyed by room id and an email cannot name it.
        """
        return self._session_key(email)

    def _approval_key(self, route: str) -> str:
        """Registry key for approvals and live choices, isolated per CONVERSATION.

        Route-inclusive regardless of ``dm_scope``: under ``unified`` the session
        key collapses every allowed user's DIRECT DM into one ``unified:{agent}``
        bucket (the channel and user drop out), so keying the approval/choice
        registries on the session key would let user B's ``1`` resolve user A's
        pending tool approval and B's card press take A's option. Forcing
        per-channel-peer keeps the route (email for a DM, ``space:{id}`` for a
        space) in the key. Under the default scope — and for a forum route under
        ANY scope — this is byte-identical to :meth:`_session_key`, so it changes
        nothing there and diverges only where the session key would otherwise
        collapse a distinct conversation onto a shared one.
        """
        gen = self._conv.current_gen(route)
        return build_dm_session_key(
            "webex",
            self._resolve_agent(),
            route,
            gen=gen,
            dm_scope=DM_SCOPE_PER_CHANNEL_PEER,
            chat_type=_chat_type_of(route),
        )

    def _session_key(self, route: str) -> str:
        """The session key for *route* at its current generation.

        Takes a ROUTE (see :func:`_route_of`), not an email, so a group space
        cannot land in the sender's private conversation.
        """
        gen = self._conv.current_gen(route)
        return build_dm_session_key(
            "webex",
            self._resolve_agent(),
            route,
            gen=gen,
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=_chat_type_of(route),
        )

    def _seed_gen(self, route: str) -> int:
        return seed_generation(
            self.sessions,
            channel="webex",
            agent=self._resolve_agent(),
            user_id=route,
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=_chat_type_of(route),
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
        # This row is an EGRESS: persisted, then served to dashboard readers. The turn
        # has already run, so scrubbing here cannot rewrite the prompt the model saw.

        user_text = redact_via_context(user_text)
        self.conv_log.append(session_key, "user", user_text, agent=agent, mid=mint_row_mid())
        if reply_text:
            self.conv_log.append(
                session_key, "assistant", reply_text, agent=agent, mid=mint_row_mid()
            )
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
        route = _route_of(inbound)
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.webex.soft_threshold_pct:
            # Capability gate (#8156): no forced compaction to run and the
            # soft nudge's /compact advice cannot work — the backend compacts
            # on its own as context fills.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("Webex: context notice skipped — %s compacts itself", unsupported)
                return
        if pct >= self.cfg.webex.hard_threshold_pct:
            self._conv.clear_awaiting(route)
            ok, detail = await self._compact_provider(provider)
            await self._reply(
                inbound,
                (
                    "🗜️ Context was near its limit, so it was compacted automatically."
                    if ok
                    else f"⚠️ Context is near its limit and automatic compaction {detail}. "
                    "Reply `/new` to start fresh."
                ),
            )
        elif pct >= self.cfg.webex.soft_threshold_pct and not self._conv.is_awaiting(route):
            self._conv.set_awaiting(route)
            await self._reply(
                inbound,
                "⚠️ This conversation's context is getting long — reply `/compact` "
                "to compress it, or `/new` to start fresh.",
            )

    async def _compact_provider(self, provider: Any) -> tuple[bool, str]:
        """Compact in place and report what actually happened.

        Returns ``(ok, detail)`` where *detail* explains a failure. The result is
        READ rather than assumed: the ACP client synthesizes a completion event
        whenever text streamed, so ``compact()`` can return normally having
        compacted nothing, and ``wait_for_compaction()`` then reports a timeout
        or a failure. Announcing success off the back of "no exception" tells the
        user their context shrank when it did not, and they stop taking the one
        action that would have helped.

        Bounded because the provider's own prompt deadline is measured in hours,
        and the caller holds the session semaphore for the whole wait.
        """
        try:
            await asyncio.wait_for(provider.compact(), timeout=_COMPACT_TIMEOUT_S)
            result = await provider.wait_for_compaction()
        except asyncio.TimeoutError:
            logger.warning("Webex: compaction timed out after %.0fs", _COMPACT_TIMEOUT_S)
            return False, "timed out"
        except Exception:
            logger.warning("Webex: compaction failed", exc_info=True)
            return False, "failed"
        kind = str(result.get("type") or "") if isinstance(result, dict) else ""
        if kind in _COMPACT_FAILURE_TYPES:
            logger.warning("Webex: compaction reported %s", kind)
            return False, "timed out" if kind == "timeout" else "failed"
        return True, ""

    async def _handle_compact(self, inbound: "WebexInbound") -> None:
        """In-place ACP ``/compact`` on the user's current session."""
        session_key = self._session_key(_route_of(inbound))
        # Serialize compaction against the turn semaphore: compacting while a
        # turn is mutating the same session races the transcript. Distinguish
        # a busy session (ask the user to retry) from an absent one (nothing
        # to compact), and always release what we acquired.
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self._reply(
                    inbound,
                    "⏳ Still working on the previous message — try `/compact` again shortly.",
                )
            else:
                await self._reply(inbound, "ℹ️ There's no conversation to compact yet.")
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self._reply(inbound, "ℹ️ There's no conversation to compact yet.")
                return
            # Capability gate (#8156, mirroring the dashboard's #7800 gate): a
            # backend that cannot serve a manual /compact treats the prompt as
            # ordinary text and never answers, so dispatching would strand the
            # bounded wait. Informational, never an error.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                await self._reply(inbound, compact_unsupported_reply(unsupported))
                return
            ok, detail = await self._compact_provider(provider)
            await self._reply(
                inbound,
                (
                    "🗜️ Context compacted."
                    if ok
                    else f"⚠️ Compaction {detail} — reply `/new` to start fresh instead."
                ),
            )
        finally:
            self.sessions.release(session_key)


# How long a dashboard link lives when the user names no duration.
_DEFAULT_TTL_SECS = 3600


def _ttl_spec(arg: str) -> str:
    """The duration token out of ``dashboard [<N>h|<N>m]``, or ``""``.

    Only the SPLITTING is local; the grammar itself belongs to
    ``token_auth.parse_duration``, so a unit added there reaches this command
    too instead of diverging silently.
    """
    parts = arg.split()
    return parts[1].strip().lower() if len(parts) > 1 else ""


def _format_ttl(ttl_secs: int) -> str:
    """A short human duration for the link's validity line."""
    mins = max(1, ttl_secs // 60)
    if mins % 60 == 0:
        return f"{mins // 60}h"
    if mins > 60:
        return f"{mins // 60}h {mins % 60}m"
    return f"{mins}m"
