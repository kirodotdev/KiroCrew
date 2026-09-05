"""Full new-path dispatch: TeamsTransport -> TurnDriver -> TeamsRenderer.

``TeamsTransport.receive()`` scope-gates + authorizes + normalizes an inbound
activity and hands the ``TeamsInbound`` (carrying ``conversation_id`` +
``service_url``) to :meth:`TeamsDispatcher.handle_message`:

    command intercept (COMMAND_SPEC: /new /compact /stop /yolo /link /unlink
                       /dashboard /help,
                       plus the /queue and /steer per-message directives)
    -> mid-turn routing: steer the running turn, or queue it behind a single
       in-place receipt bubble and drain the burst as ONE combined turn
    -> construct TeamsRenderer + on_turn_start (typing indicator)
    -> session acquire -> context build
    -> drive_turn -> TurnDriver   # shared redaction + approval ladder + audit
    -> post-turn (record_success, persist, soft/hard threshold notice)  # guarded
    -> renderer.close() + session release   # in finally

Tool approval is an Adaptive Card. Its ``Action.Submit`` returns as an ordinary
message activity carrying ``value``, which :meth:`_handle_card_action` resolves
against the awaiting ``TeamsApprovalDecider`` -- so a click is never a turn.
``ChannelTurn.auto_approve_session`` additionally honours a "Trust session" grant
and the operator's process-wide grant, letting a user stop being asked per tool.

The security ``tool_gate`` and the ``spawn_run`` auto-approve are wired by the
shared pipeline off ``ctx_builder.hooks`` (channel-neutral), so this module never
imports ``kiro_crew.slack``.

Dependency direction is ``teams -> messaging`` (allowed).
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.history import mint_row_mid
from kiro_crew.messaging.attachments import IngestLimits
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.commands import (
    YOLO_PHRASING_MARKDOWN,
    compact_unsupported_backend,
    compact_unsupported_reply,
    format_ttl,
    parse_dashboard_ttl,
    run_yolo_command,
    stop_running_turn,
)
from kiro_crew.messaging.conversation import ConversationState
from kiro_crew.messaging.dispatch import (
    ChannelTurn,
    build_directive_consumer,
    drive_turn,
    inbound_permitted,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.link import (
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    rebind_conversation_location,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.messaging.queue_receipt import (
    ATTACHMENT_PLACEHOLDER,
    MAX_COLLAPSE,
    STEER_ACK_EMOJI,
    ReceiptQueue,
    ReceiptSurface,
)
from kiro_crew.platform.context import redact_via_context
from kiro_crew.safety_override import safety_override
from kiro_crew.sel import sel
from kiro_crew.teams.approvals import TeamsApprovalDecider
from kiro_crew.teams.attachments import append_attachment_context, process_teams_attachments
from kiro_crew.teams.cards import (
    DECISION_APPROVE,
    DECISION_DENY,
    DECISION_TRUST,
    KIND_APPROVAL,
    KIND_SESSION,
    parse_submit,
)
from kiro_crew.teams.client import TeamsSendError
from kiro_crew.teams.commands import (
    DIRECTIVE_USAGE,
    HELP_TEXT,
    command_argument,
    parse_command,
    parse_directive,
)
from kiro_crew.teams.renderer import TeamsRenderer
from kiro_crew.teams.session_resume import (
    ResumeReleaseError,
    RoutingDecision,
    TeamsSessionResume,
)
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

# Keep a collapsed drain within the shared ingest layer's per-turn file cap, so a
# burst of uploads is answered across turns instead of having its surplus refused.
_MAX_COLLAPSED_ATTACHMENTS = IngestLimits().max_attachments

#: Commands that must stay reachable while resume routing would REFUSE the message.
#: Everything else targets a session or is a plain turn, so it must not run until the
#: user has been told the link is gone -- but a user whose link broke needs a way out,
#: and `/sessions` is the way back in.
_RESUME_EXEMPT_COMMANDS = frozenset({"new", "unlink", "sessions", "help"})

#: A release that could not be made durable changes nothing and says so.
_RELEASE_FAILURE = (
    "⚠️ Couldn't save the session release, so the command was NOT completed. Fix the "
    "gateway's storage problem, then retry."
)

# Re-exported so callers keep importing ConversationState from this module's
# command surface, matching the Telegram/WeCom/Webex packages.
__all__ = ["ConversationState", "TeamsDispatcher"]


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
        allowed_emails: "set[str] | None" = None,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        self.client: "TeamsClient | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)
        # Mid-turn queue receipts. Teams can edit a bot's own activity, so unlike
        # WeCom/Weixin (whose reply is bound to the inbound request) it can carry
        # the single collapsing receipt bubble the shared module implements.
        self._queue = ReceiptQueue()
        # Live renderer per session, so a card click can settle the prompt it
        # answered and resolve an option chip against the labels that turn
        # actually offered. Read by _handle_card_action.
        self._active_renderers: dict[str, TeamsRenderer] = {}
        # Owner-only dashboard-session picker + the durable resume binding. Constructed
        # here (not in the gateway) so `handle_message` can route every message through
        # it; the gateway only attaches `dashboard_state` afterwards.
        self._session_resume = TeamsSessionResume(sessions, conv_log, set(allowed_emails or ()))

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(
        self,
        inbound: "TeamsInbound",
        *,
        interpret_commands: bool = True,
        drain: bool = True,
    ) -> None:
        """Drive one authorized inbound Teams message through TurnDriver.

        ``interpret_commands=False`` is used by the queue drain: a drained payload
        is turn content, so a queued ``/new`` must reach the model as literal text
        rather than executing on drain.

        ``drain=False`` is used by that same replay so the drained turn does not
        re-enter :meth:`_drain_queue` at its tail. The drain's own loop pumps
        whatever arrived during the combined turn; without this the replay would
        nest one drain inside another for every burst round.
        """
        assert self.client is not None, "TeamsDispatcher.client must be set"
        # Inbound channels-governance gate (off-loop) — recheck per message so a
        # host-profile deny added after connect stops dispatch without a restart
        # (the startup gate only blocks CONNECTING). Silently drop on deny.
        if not await inbound_permitted("teams"):
            return
        # The ONE reader of the identity in this module. Re-deriving it with a fixed
        # preference order is what made a turn key on the UPN while a card click keyed on
        # the object id, for a user the allow-list authorized by object id: the approval
        # card then resolved against a different session and expired, and `/new` rotated a
        # generation the turn was not using.
        email = self._identity(inbound)
        text = inbound.text
        logger.info(
            "Teams inbound from %s: %d chars",
            email[:3] + "***" if email else "?",
            len(text or ""),
        )

        # An Adaptive Card click is not a turn: it answers a prompt this process
        # is already awaiting. Handled before command parsing because its payload
        # carries no text to parse.
        if inbound.is_card_action:
            await self._handle_card_action(inbound)
            return

        override_mode: str | None = None
        # An attachment-bearing message is never a command. Teams puts the caption
        # in ``text``, so "/stop here is the log" would otherwise cancel the turn AND
        # discard the file; and a caption is prose the user wrote about the upload,
        # not an instruction to this dispatcher. Discord and Telegram draw the line
        # in the same place.
        cmd = parse_command(text) if (interpret_commands and not inbound.attachments) else None
        # Resume routing runs BEFORE the intercept, and its answer is used by the
        # commands below. `/compact` and `/stop` act on the RESOLVED session, so after a
        # binding was destroyed they would compact or cancel the native Teams session
        # while the user believes they drive the resumed one; deciding here makes that
        # structural rather than a thing each handler has to remember.
        route = RoutingDecision()
        if cmd not in _RESUME_EXEMPT_COMMANDS:
            route = await self._session_resume.route(inbound.conversation_id)
            if route.refusal is not None:
                # Settle only once the refusal actually LANDED: an unsettled record owes
                # the same refusal again, which is the safe direction -- settling a
                # refusal the user never saw would route their next message into a
                # transcript they never chose. Same gate Discord's copy applies.
                if await self._reply(inbound, route.refusal):
                    await self._session_resume.settle(inbound.conversation_id, route)
                return
        if interpret_commands and not inbound.attachments:
            # ── Command intercept (no LLM session needed) ──
            if cmd == "sessions":
                await self._session_resume.show_picker(
                    cast("TeamsClient", self.client),
                    self._identity(inbound),
                    inbound.conversation_id,
                    inbound.service_url,
                    command_argument(text),
                )
                return
            if cmd == "new":
                await self._handle_new(inbound)
                return
            if cmd == "compact":
                self._conv.clear_awaiting(email)
                await self._handle_compact(inbound, route.resumed_key)
                return
            if cmd == "help":
                await self._reply(inbound, HELP_TEXT)
                return
            if cmd == "stop":
                await self._handle_stop(inbound, route.resumed_key)
                return
            if cmd == "yolo":
                await self._handle_yolo(inbound, command_argument(text))
                return
            if cmd == "link":
                await self._handle_link(inbound)
                return
            if cmd == "unlink":
                await self._handle_unlink(inbound)
                return
            if cmd == "dashboard":
                await self._handle_dashboard(inbound, command_argument(text))
                return
            # A /queue or /steer prefix forces that path for THIS message only.
            override_mode, payload = parse_directive(text)
            if override_mode is not None:
                if not payload:
                    # A bare directive matches neither parser. Answering with
                    # usage beats handing the literal "/queue" to the model, which
                    # would reply to it as chat text -- indistinguishable, to the
                    # user, from the feature not existing.
                    await self._reply(inbound, DIRECTIVE_USAGE)
                    return
                text = payload

        # ── Mid-turn concurrency: check the CURRENT-generation key for an
        # in-flight turn BEFORE any idle/daily rotation, then steer or queue.
        # ``route.resumed_key`` is NOT re-resolved here: re-reading it would let the
        # binding change between the decision and its use, which is the silent mis-route
        # the single-decision shape exists to prevent.
        session_key = route.resumed_key or self._session_key(email)
        if self.sessions.is_busy(session_key):
            await self._handle_busy(inbound, session_key, text, override_mode)
            return

        # Attachments are downloaded HERE -- after the governance gate, after the
        # command intercept, and after the busy check -- so nothing is fetched for a
        # message that ends up queued, and the temp files are unlinked by the same
        # frame that awaits the turn reading them. ``temp_paths`` therefore outlive
        # their only reader and never outlive it.
        temp_paths: list[str] = []
        if inbound.attachments and TEAMS_CAPABILITIES.files_inbound:
            result = await process_teams_attachments(self.client, inbound.attachments)
            temp_paths = list(result.temp_paths)
            text = append_attachment_context(text, result)
        try:
            await self._run_turn(inbound, email, text, drain=drain, resumed_key=route.resumed_key)
        finally:
            if temp_paths:
                # In a worker: one syscall per file on a directory that is not
                # guaranteed to be local disk.
                await asyncio.to_thread(cleanup_attachments, temp_paths)

    async def _run_turn(
        self,
        inbound: "TeamsInbound",
        email: str,
        text: str,
        *,
        drain: bool,
        resumed_key: str | None = None,
    ) -> None:
        """Rotate, build the renderer, drive one turn, then drain what arrived.

        Split out of :meth:`handle_message` only so the attachment temp-path cleanup
        can wrap the whole turn in one ``finally`` without indenting it.
        """
        assert self.client is not None
        conversation_id = inbound.conversation_id
        service_url = inbound.service_url
        if not text:
            # Teams attaches a rich-text body as ``text/html``, which the ingest
            # skips as a duplicate of ``text``; an activity carrying only that has
            # no prompt behind it, and an empty turn would answer a message the
            # user never wrote.
            return
        if resumed_key is None:
            # A RESUMED turn must not rotate: rotation is a property of this
            # conversation's own generation, and applying it to a dashboard session
            # would move the user off the transcript they just attached to.
            self._conv.maybe_rotate(
                email,
                time.time(),
                idle_minutes=self.cfg.messaging.idle_reset_minutes,
                daily_reset_hour=self.cfg.messaging.daily_reset_hour,
            )
        # Decided ONCE, upstream, and not re-resolved: re-reading the binding here would
        # let it change between the decision and its use.
        session_key = resumed_key or self._session_key(email)
        agent = self._resolve_agent()

        # Adaptive Card approvals: the decider awaits the click and denies by
        # default on timeout, and the renderer posts the card that resolves it.
        decider = TeamsApprovalDecider(session_key=session_key)
        renderer = TeamsRenderer(
            self.client,
            conversation_id,
            service_url,
            TEAMS_CAPABILITIES,
            session_key=session_key,
            decider=decider,
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

        # Mirror this conversation's dashboard tab back to Teams, unasked, using
        # the shared rule/opt-out/re-assert helper. Bounded: one whole-map write
        # on a conversation's first turn.
        self._bind_origin_mirror(session_key, inbound)

        self._active_renderers[session_key] = renderer
        try:
            await drive_turn(
                ChannelTurn(
                    channel_type="teams",
                    session_key=session_key,
                    # Session-directive consumer: monitor_start / autonudge_stop /
                    # ... return a marker TurnDriver decodes; apply it against THIS
                    # turn's session key (dashboard-only directives stay refused
                    # for channel sessions).
                    directive_consumer=build_directive_consumer(
                        session_key=session_key, sessions=self.sessions, dispatcher=self
                    ),
                    conversation_id=f"teams:{email}",
                    agent=agent,
                    user_text=text,
                    renderer=renderer,
                    approval_mode=self.approval_mode,
                    decider=decider,
                    # The ONE process-wide grant, re-read per permission request so
                    # arming or expiring it takes effect on the next tool rather
                    # than after a restart. Same predicate every other channel
                    # passes -- Teams keeps no grant of its own. It does not weaken
                    # the PreToolUse gate: the sensitive-path keystone, the
                    # governance ceiling and the deny-list all run ahead of this
                    # rung in TurnDriver, so a hard DENY still wins.
                    auto_approve_session=lambda: safety_override().is_active(),
                    persist=lambda user_text, reply, is_new: self._persist_turn(
                        session_key, user_text, reply, is_new, agent
                    ),
                    notice=lambda sk, provider: self._maybe_notice(inbound, sk, provider),
                    audit_caller=f"teams:{email}",
                    after_persist=_surface_new_session,
                ),
                sessions=self.sessions,
                ctx_builder=self.ctx_builder,
            )
        finally:
            # A Trust click is granted by the decider the moment it resolves, so
            # the rest of THIS turn stops prompting. Nothing to promote here.
            # A renderer that posted [OPTIONS:] chips must OUTLIVE its turn: the
            # chips are posted at on_done and tapped whenever the user gets to
            # them, so retiring the renderer here would make every chip resolve
            # against nothing and answer "those choices are from an earlier reply"
            # -- an advertised capability that never works. Bounded: the entry is
            # one renderer per session and the next turn's assignment replaces it.
            # An approval card needs no such reprieve; it resolves while the turn
            # is still blocked on the decider.
            if not renderer.has_pending_choices:
                self._active_renderers.pop(session_key, None)

        # Answer anything that arrived mid-turn. Runs after the semaphore is
        # released so the drained turn can acquire it.
        if drain:
            await self._drain_queue(session_key, inbound)

    async def _handle_busy(
        self,
        inbound: "TeamsInbound",
        session_key: str,
        text: str,
        override_mode: str | None = None,
    ) -> None:
        """A message arrived mid-turn: steer the running turn, or queue for after.

        A mid-turn message is never dropped and the user is never asked to resend
        it -- losing it is the one outcome a queue exists to prevent. Teams can
        edit its own activities, so the held message gets the shared collapsing
        receipt bubble rather than a fire-and-forget notice.
        """
        assert self.client is not None
        if not self.sessions.is_busy(session_key):
            # The turn finished inside the window; run it as a fresh turn rather
            # than stranding it.
            await self.handle_message(inbound)
            return
        mode = override_mode or self.cfg.messaging.queue_mode
        # An attachment-bearing message is never steered: a steer carries TEXT into
        # the running turn, so the files would be dropped on the floor while the
        # user is told their message was folded in. Queue it instead -- the drained
        # turn ingests the descriptors and the picture actually arrives.
        if mode != "queue" and not inbound.attachments:
            provider = self.sessions.get_provider(session_key)
            steer = getattr(provider, "steer", None)
            # Only steer a GENUINELY live turn. ``is_busy`` stays true through
            # post-turn bookkeeping (record_success, persist, notice, audit -- all
            # await points), so without this guard a steer could reach kiro-cli for
            # a prompt that already ended and be silently swallowed, leaving the
            # user an acknowledgement with no answer.
            has_active = getattr(provider, "has_active_turn", None)
            live = has_active is None or bool(has_active())
            steered = bool(
                live
                and getattr(provider, "supports_steer", False)
                and steer is not None
                and await steer(text)
            )
            if steered:
                # Teams has no reaction API for a bot, so unlike Telegram/Discord
                # the acknowledgement has to be a message.
                await self._reply(inbound, f"{STEER_ACK_EMOJI} Folded into the reply in progress.")
                return
        # queue mode, a /queue override, or steer unavailable.
        if not await self._enqueue_with_receipt(session_key, inbound, text):
            await self.handle_message(inbound)

    # ── Adaptive Card clicks ───────────────────────────────────────────────

    def _click_session_keys(self, inbound: "TeamsInbound", identity: str) -> list[str]:
        """Session keys a card posted into this conversation may be registered under.

        A click has to reach the session the TURN ran under. In a resumed conversation
        that is the bound ``dashboard:`` key -- where ``_run_turn`` registered the decider
        and the renderer -- so keying only off the native ``teams:{email}`` session misses
        both, and every Approve/Deny and every option chip reports itself stale before the
        tool denies by default on the prompt timeout.

        Both keys, not just the resolved one, because a card click is a relief activity
        that bypasses the busy check: a ``/sessions`` pick can bind this conversation while
        an earlier turn is still in flight, and that turn's cards stay registered under the
        key it started with. Both keys belong to this conversation and this identity, so
        trying them widens nothing -- the per-prompt nonce still decides.
        """
        native = self._session_key(identity)
        resumed = self._session_resume.resumed_session(inbound.conversation_id) or ""
        return [resumed, native] if resumed and resumed != native else [native]

    async def _handle_card_action(self, inbound: "TeamsInbound") -> None:
        """Resolve an Adaptive Card submit: a tool decision or an option pick.

        Every field is validated by ``parse_submit`` and then used only as a
        LOOKUP into state this process holds -- the payload never carries the
        decision's authority. An unrecognised, stale or already-answered click
        tells the user rather than failing silently, because a button that
        appears to do nothing is indistinguishable from a broken bot.
        """
        payload = parse_submit(inbound.card_value)
        if payload is None:
            logger.debug("Teams: unrecognised card submit ignored")
            return
        identity = self._identity(inbound)
        candidates = self._click_session_keys(inbound, identity)
        if payload["kc"] == KIND_APPROVAL:
            decision = payload["decision"]
            approved = decision in (DECISION_APPROVE, DECISION_TRUST)
            # At most one candidate can hold this (rid, nonce) pair, so trying them in
            # order resolves exactly the prompt that is waiting and no other.
            session_key = next(
                (
                    key
                    for key in candidates
                    if TeamsApprovalDecider.resolve_global(
                        key,
                        payload["rid"],
                        payload["nonce"],
                        approved=approved,
                        trust=decision == DECISION_TRUST,
                    )
                ),
                "",
            )
            resolved = bool(session_key)
            if not resolved:
                audit_outcome = "denied_stale_card"
            elif approved:
                audit_outcome = "approved"
            else:
                audit_outcome = "denied"
            sel().log_api_access(
                caller=identity or "unknown",
                operation="teams.tool_decision",
                outcome=audit_outcome,
                source="teams",
                resources=f"session={session_key or candidates[0]}",
            )
            if not resolved:
                await self._reply(
                    inbound,
                    "⌛ That prompt is no longer waiting — it was already answered "
                    "or it timed out. Send the request again if you still want it.",
                )
                return
            outcome = {
                DECISION_APPROVE: "approved",
                DECISION_TRUST: "approved · auto-approve armed",
                DECISION_DENY: "denied",
            }[decision]
            if decision == DECISION_TRUST:
                # Arm the SAME process-wide grant `/yolo on` arms, through the same
                # shared helper -- so its duration, its expiry and its SEL row are
                # identical whichever way the user asked. This is why the button
                # needs no grant store of its own. Awaited BEFORE the card is
                # settled, so the label cannot claim a grant that failed to arm.
                grant = await run_yolo_command(
                    "on",
                    source="teams_card",
                    caller=identity or "unknown",
                    phrasing=YOLO_PHRASING_MARKDOWN,
                )
                await self._reply(inbound, grant)
            renderer = self._active_renderers.get(session_key)
            if renderer is not None:
                await renderer.settle_prompt(payload["rid"], outcome)
            return
        if payload["kc"] == KIND_SESSION:
            # A session pick. Resolved against the list THIS process offered, so the
            # payload's index can only ever miss -- it never names a session key.
            await self._session_resume.choose(
                cast("TeamsClient", self.client),
                identity,
                inbound.conversation_id,
                inbound.service_url,
                # The card's id, not the submit's: a submit activity has its own
                # `id`, and `replyToId` is what points back at the picker it came
                # from. Passing `activity_id` here made every real press look like a
                # press on a different posting, so it always answered "expired".
                inbound.reply_to_id or inbound.activity_id,
                payload["nonce"],
                int(payload["index"]),
            )
            return
        # An option chip: resolve the label from what this turn actually offered,
        # then run it as an ordinary turn -- the same thing typing it would do.
        session_key, label, renderer = "", "", None
        for key in candidates:
            renderer = self._active_renderers.get(key)
            label = renderer.option_label(payload["nonce"], payload["index"]) if renderer else ""
            if label:
                session_key = key
                break
        if not label or renderer is None:
            await self._reply(
                inbound,
                "⌛ Those choices are from an earlier reply — type your answer instead.",
            )
            return
        # Replace the chips with the pick BEFORE running the turn: every other chip
        # still looks live until this lands, and the transcript otherwise never
        # records which one was chosen. This also clears the nonce, so the renderer
        # below is free to retire.
        await renderer.settle_options(label)
        self._active_renderers.pop(session_key, None)
        # interpret_commands=False, and this is a SECURITY boundary rather than a
        # nicety: the label is a string the MODEL wrote into its ``[OPTIONS:]``
        # trailer, not something the user typed. Display redaction does not strip a
        # leading "/", so with interpretation on, a model that emitted
        # ``[OPTIONS: /dashboard | cancel]`` -- by prompt injection or by accident --
        # renders a chip whose single tap mints a presigned dashboard login token,
        # and ``/yolo on`` is one chip away from conversation-wide auto-approve. A
        # chip is turn content, exactly like a drained queue payload, so it reaches
        # the model as literal text.
        await self.handle_message(
            replace(inbound, text=label, card_value=None), interpret_commands=False
        )

    # ── Mid-turn queue ─────────────────────────────────────────────────────

    def _receipt_surface(self, inbound: "TeamsInbound") -> ReceiptSurface:
        """A receipt surface with this conversation's address already bound.

        Binding the address here is what keeps Teams' ``service_url`` out of the
        shared queue module, which never sees an address at all.
        """
        # cast, not assert: mypy does not carry an assert-narrowed local into the
        # nested class body below. The caller path always has a live client.
        client = cast("TeamsClient", self.client)
        conversation_id = inbound.conversation_id
        service_url = inbound.service_url

        class _Surface:
            label = "teams"

            async def send_receipt(self, body: str) -> Any | None:
                try:
                    return await client.send_message(conversation_id, body, service_url)
                except TeamsSendError:
                    # No receipt bubble is better than failing the enqueue: the
                    # message is still queued and will be answered.
                    logger.debug("Teams: queue receipt send failed", exc_info=True)
                    return None

            async def edit_receipt(self, msg_id: Any, body: str) -> None:
                await client.update_message(conversation_id, str(msg_id), body, service_url)

        return _Surface()

    async def _enqueue_with_receipt(
        self, session_key: str, inbound: "TeamsInbound", text: str
    ) -> bool:
        """Atomically enqueue a mid-turn message and create/grow its receipt.

        Holding ``self._queue.lock`` across BOTH the enqueue and the receipt
        bookkeeping is what makes this race-free against the end-of-turn drain
        (which takes the same lock to dequeue + flip): the drain either sees this
        message queued WITH its receipt or sees neither yet, never a half state
        that would orphan a bubble. Returns False when the turn finished in the
        window, so the caller runs the message as a fresh turn instead.
        """
        async with self._queue.lock:
            # The RAW attachment descriptors ride with the entry, not downloaded
            # bytes: the drained turn re-ingests them, so a queued picture is fetched
            # once, when there is finally a turn to read it.
            if not self.sessions.enqueue(
                session_key,
                str(time.time()),
                text,
                force=False,
                attachments=list(inbound.attachments or []),
            ):
                return False
            # An upload with no caption has no text; a placeholder keeps it from
            # showing as a blank line in the receipt.
            await self._queue.create_or_grow_locked(
                session_key, self._receipt_surface(inbound), text or ATTACHMENT_PLACEHOLDER
            )
            return True

    async def _drain_queue(self, session_key: str, inbound: "TeamsInbound") -> None:
        """Collapse everything queued during the finished turn into ONE turn.

        Order is preserved and the texts are blank-line joined, rather than
        replaying N separate turns. The dequeue and the receipt flip run together
        under ``self._queue.lock``; the combined turn itself runs OUTSIDE it, so
        messages arriving during it open a fresh receipt and drain after.
        """
        while True:
            texts: list[str] = []
            attachments: list[Any] = []
            async with self._queue.lock:
                remainder: list[tuple[str, str, dict]] = []
                defer_rest = False
                while True:
                    item = self.sessions.dequeue(session_key)
                    if item is None:
                        break
                    queued_files = list(item[2].get("attachments") or [])
                    # One collapsed turn must not exceed the neutral ingest's own
                    # per-turn attachment cap, or the surplus files would be
                    # silently refused by the ingest instead of answered next round.
                    over_files = bool(
                        texts
                        and queued_files
                        and len(attachments) + len(queued_files) > _MAX_COLLAPSED_ATTACHMENTS
                    )
                    if not defer_rest and len(texts) < MAX_COLLAPSE and not over_files:
                        texts.append(item[1])
                        attachments.extend(queued_files)
                    else:
                        # Once one message no longer fits, defer it AND everything
                        # behind it, so the queue keeps exact FIFO order.
                        defer_rest = True
                        remainder.append(item)
                # Re-enqueue the surplus IN ORIGINAL ORDER (the queue is empty
                # now, so re-adding preserves FIFO) to drain after the next turn.
                for msg_ts, queued_text, kwargs in remainder:
                    self.sessions.enqueue(session_key, msg_ts, queued_text, force=True, **kwargs)
                if not texts:
                    return
                await self._queue.flip_answering_locked(
                    session_key,
                    self._receipt_surface(inbound),
                    [t or ATTACHMENT_PLACEHOLDER for t in texts],
                    len(remainder),
                )
            combined = "\n\n".join(t for t in texts if t)
            replay = replace(inbound, text=combined, attachments=attachments)
            # Drained payloads are turn content, so command interpretation is off:
            # a queued "/new" must reach the model as text, not execute on drain.
            # drain=False keeps the pump in THIS loop instead of nesting a drain
            # inside the replayed turn.
            await self.handle_message(replay, interpret_commands=False, drain=False)
            # Loop rather than return: messages that arrived DURING the combined
            # turn join this same FIFO pump. The only exit is the empty-queue check
            # above, so nothing is left waiting for unrelated future user input.

    # ── /stop ──────────────────────────────────────────────────────────────

    async def _handle_stop(self, inbound: "TeamsInbound", resumed_key: str | None = None) -> None:
        """Hard cancel: abort the in-flight turn and clear the queue.

        ``resumed_key`` is the session the ROUTING decision resolved, and it has to be
        threaded in rather than recomputed: a resumed conversation's turns run under the
        dashboard key, so recomputing the native key here cancels a session the user is
        not looking at and leaves the one they are watching running.

        The cooperative-cancel contract, the lock ordering across ``clear_queue``
        and the receipt finalize, and both replies live once in
        ``messaging.commands``; only the address-bound receipt surface is ours.
        """
        reply = await stop_running_turn(
            self.sessions,
            resumed_key or self._session_key(self._identity(inbound)),
            queue=self._queue,
            surface=self._receipt_surface(inbound),
        )
        await self._reply(inbound, reply)

    # ── /yolo (this conversation's auto-approve grant) ─────────────────────

    async def _handle_yolo(self, inbound: "TeamsInbound", arg: str) -> None:
        """Report or change the process-wide auto-approve grant.

        The SAME grant the dashboard toggle drives, through the shared
        ``run_yolo_command`` -- exactly like Telegram, Webex, WeCom, iLink and
        iMessage. Teams keeps NO grant of its own, deliberately: a channel-local
        trusted-session store would be a second grant with its own lifetime, its own
        audit trail and its own way to disagree with the dashboard about whether
        auto-approve is on, and "is YOLO on?" must have one answer.

        The card's "Trust session" button arms this same grant, so the button and the
        command cannot diverge either.

        It does not weaken the PreToolUse gate: the sensitive-path keystone, the
        governance ceiling and the deny-list all run ahead of the auto-approve ladder
        in ``TurnDriver``, so a hard DENY still wins. Expiry, renewal and the SEL row
        all belong to the shared helper.
        """
        identity = self._identity(inbound)
        reply = await run_yolo_command(
            arg,
            source="teams",
            caller=identity or "unknown",
            phrasing=YOLO_PHRASING_MARKDOWN,
        )
        await self._reply(inbound, reply)

    # ── /dashboard (presigned login link) ──────────────────────────────────

    async def _handle_dashboard(self, inbound: "TeamsInbound", arg: str) -> None:
        """Mint and send a presigned dashboard login link.

        Teams is personal-scope only, so unlike Telegram there is no group case to
        refuse: every authorized conversation here is already a 1:1 with an
        allow-listed user. The link is still a CREDENTIAL, so issuance is audited
        either way.

        ``generate_token`` is called directly rather than through a shell, and the
        TTL parse comes from the shared helper so ``/dashboard 2h`` and Telegram's
        ``/kirocrew dashboard 2h`` cannot disagree about durations.
        """
        # Imported here, not at module scope: `messaging` must not import
        # `dashboard`, and this keeps the dashboard token module off the Teams
        # import path until a user actually asks for a link.
        from kiro_crew.dashboard.token_auth import (
            MAX_SESSION_TTL_SECS,
            generate_token,
            parse_duration,
        )
        from kiro_crew.dashboard.urls import dashboard_origin, parse_dashboard_url

        identity = self._identity(inbound)
        ttl_secs = min(
            parse_dashboard_ttl(arg, parse_duration=parse_duration), MAX_SESSION_TTL_SECS
        )
        try:
            token = generate_token(identity or "teams", ttl_seconds=ttl_secs)
            origin = dashboard_origin(self.cfg.dashboard.url)
            if not origin:
                # No configured dashboard.url: fall back to the local port, which
                # parse_dashboard_url resolves with the KIROCREW_PORT override.
                _, port = parse_dashboard_url(self.cfg.dashboard.url)
                origin = f"http://localhost:{port}"
            sel().log_api_access(
                caller=identity or "unknown",
                operation="teams.dashboard_token",
                outcome="ok",
                source="teams",
                resources=f"ttl={ttl_secs}",
            )
            await self._reply(
                inbound,
                f"🔗 Dashboard link (valid {format_ttl(ttl_secs)}):\n{origin}/?token={token}",
            )
        except Exception as exc:
            logger.warning("Teams /dashboard: token generation failed", exc_info=True)
            try:
                sel().log_api_access(
                    caller=identity or "unknown",
                    operation="teams.dashboard_token",
                    outcome="error",
                    source="teams",
                    resources=f"ttl={ttl_secs}",
                )
            except Exception:
                # The audit trail must never turn a user-facing failure reply into
                # a crash; the warning above already captured the cause.
                pass
            await self._reply(inbound, f"⚠️ Could not generate a dashboard link: {exc}")

    # ── /link, /unlink (dashboard mirror) ──────────────────────────────────

    def _origin_mirror_link(self, inbound: "TeamsInbound") -> ChannelLink:
        """The mirror location for the conversation this session is read in.

        One definition shared by the automatic bind, ``/link`` and ``/unlink``: an
        unlink matches an occupied location by VALUE, so a second spelling of
        "this conversation" would let the release miss the binding the bind wrote.
        """
        return ChannelLink("teams", channel_id=inbound.conversation_id, thread_id=None)

    def _bind_origin_mirror(self, session_key: str, inbound: "TeamsInbound") -> None:
        """Mirror this conversation's dashboard tab back to Teams, unasked.

        The rule, the re-assert and the opt-out live in the shared
        :func:`~kiro_crew.messaging.link.bind_origin_mirror`; this only supplies
        Teams' spelling of "this conversation".
        """
        bind_origin_mirror(
            self.sessions, key=session_key, location=self._origin_mirror_link(inbound)
        )

    async def _handle_link(self, inbound: "TeamsInbound") -> None:
        """Re-enable mirroring of this conversation's dashboard tab back here.

        Mirroring is automatic, so this withdraws a previous ``/unlink`` rather
        than being the only way to turn it on. The rebind sequence, its
        single-write batching and the reply live once in
        :func:`~kiro_crew.messaging.link.rebind_conversation_location`, the
        counterpart of the ``release_conversation_location`` that ``/unlink``
        already used; only Teams' spelling of "this conversation" is ours.
        """
        reply = rebind_conversation_location(
            self.sessions,
            key=self._session_key(self._identity(inbound)),
            location=self._origin_mirror_link(inbound),
            unlink_command="`/unlink`",
        )
        await self._reply(inbound, reply)

    async def _handle_unlink(self, inbound: "TeamsInbound") -> None:
        """Stop mirroring dashboard replies into this conversation.

        The opt-out is persisted BEFORE the release: mirroring is re-asserted on
        every inbound turn, so a release alone would be undone by the user's next
        message.
        """
        # A RESUMED session is released first, and its failure stops the command: a
        # cleared owner whose flush then failed would run natively in silence until the
        # persisted binding revives on restart, splitting one conversation's history in
        # two. Reporting the failure and changing nothing is the only honest outcome.
        try:
            left_resumed = await self._session_resume.leave_resumed_session(inbound.conversation_id)
        except ResumeReleaseError:
            await self._reply(inbound, _RELEASE_FAILURE)
            return
        key = self._session_key(self._identity(inbound))
        with self.sessions.batched_save():
            self.sessions.set_mirror_opt_out(key, True)
            reply, _swept = release_conversation_location(
                self.sessions,
                key=key,
                location=self._origin_mirror_link(inbound),
                channel="teams",
            )
        if left_resumed is not None:
            reply = f"{reply}\nAlso left the resumed dashboard session."
        await self._reply(inbound, reply)

    # ── /new ───────────────────────────────────────────────────────────────

    async def _handle_new(self, inbound: "TeamsInbound") -> None:
        """Start a fresh conversation, dropping anything still queued.

        Clearing the queue is part of "fresh": a burst held against the previous
        generation would otherwise drain into the new session as if the user had
        just typed it.
        """
        identity = self._identity(inbound)
        # Same ordering and the same refusal as `/unlink`, for the same reason.
        try:
            left_resumed = await self._session_resume.leave_resumed_session(inbound.conversation_id)
        except ResumeReleaseError:
            await self._reply(inbound, _RELEASE_FAILURE)
            return
        session_key = self._session_key(identity)
        async with self._queue.lock:
            self.sessions.clear_queue(session_key)
            await self._queue.finish_cancelled_locked(session_key, self._receipt_surface(inbound))
        self._conv.bump_gen(identity)
        # Retire the OLD generation's renderer here. A renderer kept alive for
        # outstanding chips is keyed by the pre-bump session key, and the next turn
        # assigns under the new one -- so nothing else ever pops this entry, and each
        # retained renderer holds a whole answer buffer for the process lifetime.
        # Those chips are unreachable now regardless: `/new` cleared the session they
        # belonged to.
        self._active_renderers.pop(session_key, None)
        message = "✅ Started a fresh conversation."
        if left_resumed is not None:
            message = "✅ Started a fresh conversation — left the resumed dashboard session."
        await self._reply(inbound, message)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    @staticmethod
    def _identity(inbound: "TeamsInbound") -> str:
        """The sender identity a session is keyed on.

        ``resolved_identity`` is which of the two forms the ALLOW-LIST authorized, decided
        once at the transport's gate. Re-deriving it here from a fixed preference order is
        what let the gate admit a user on their AAD object id and this key their session on
        their UPN -- a session nobody authorized, and one owner-only ``/sessions`` refused.
        The fallback covers an inbound built outside ``receive`` and is the same answer
        whenever only one form is present.
        """
        return inbound.resolved_identity or inbound.user_email or inbound.aad_object_id

    async def _reply(self, inbound: "TeamsInbound", body: str) -> bool:
        """Send a command/notice reply into the inbound conversation; did it land?

        Command acknowledgements are cosmetic relative to the command's effect --
        a ``/stop`` that cancelled the turn but could not post "Stopped." still
        stopped it -- so a failed reply is logged rather than raised, and most
        callers ignore the result. It is returned rather than swallowed because a
        notice that GATES a durable state change is not cosmetic: settling a resume
        refusal the user never received routes their next message into a transcript
        they did not choose.
        """
        assert self.client is not None
        try:
            await self.client.send_message(inbound.conversation_id, body, inbound.service_url)
            return True
        except TeamsSendError:
            logger.warning("Teams: command reply delivery failed", exc_info=True)
            return False

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
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Teams"
            self.conv_log.set_title(session_key, title)

    async def _maybe_notice(self, inbound: "TeamsInbound", session_key: str, provider: Any) -> None:
        """Context-length handling, surfaced as a separate message post-turn.

        Soft threshold nudges the user to /compact or /new; hard threshold
        forces a compaction so the window never overflows.
        """
        assert self.client is not None
        email = self._identity(inbound)
        pct = self.sessions.check_context_usage(session_key, provider)
        if pct >= self.cfg.teams.soft_threshold_pct:
            # Capability gate (#8156): no forced compaction to run and the
            # soft nudge's /compact advice cannot work — the backend compacts
            # on its own as context fills.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                logger.debug("Teams: context notice skipped — %s compacts itself", unsupported)
                return
        if pct >= self.cfg.teams.hard_threshold_pct:
            self._conv.clear_awaiting(email)
            try:
                await provider.compact()
                await provider.wait_for_compaction()
                await self._reply(
                    inbound,
                    "🗜️ Context was near its limit, so it was compacted automatically.",
                )
            except Exception:
                logger.debug("Teams hard-threshold compaction failed", exc_info=True)
        elif pct >= self.cfg.teams.soft_threshold_pct and not self._conv.is_awaiting(email):
            self._conv.set_awaiting(email)
            await self._reply(
                inbound,
                "⚠️ This conversation's context is getting long — reply `/compact` "
                "to compress it, or `/new` to start fresh.",
            )

    async def _handle_compact(
        self, inbound: "TeamsInbound", resumed_key: str | None = None
    ) -> None:
        """In-place ACP ``/compact`` on the session this conversation is DRIVING.

        ``resumed_key`` for the same reason ``/stop`` takes it: compacting the native
        session while the user drives a resumed one compresses a context they are not
        using and leaves the one they are.
        """
        assert self.client is not None
        session_key = resumed_key or self._session_key(self._identity(inbound))
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
            # unbounded wait below. Informational, never an error.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                await self._reply(inbound, compact_unsupported_reply(unsupported))
                return
            await provider.compact()
            await provider.wait_for_compaction()
            await self._reply(inbound, "🗜️ Context compacted.")
        except Exception:
            logger.exception("Teams /compact failed for %s", session_key)
            await self._reply(inbound, "⚠️ Compaction failed — please try again.")
        finally:
            self.sessions.release(session_key)
