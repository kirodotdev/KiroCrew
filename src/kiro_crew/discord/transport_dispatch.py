"""Full new-path dispatch: DiscordTransport -> TurnDriver -> DiscordRenderer.

``DiscordTransport.receive()`` authorizes + normalizes an inbound message and
hands the ``InboundMessage`` to :meth:`DiscordDispatcher.handle_message`,
which mirrors the Telegram transport dispatch:

    command intercept (!new, !compact, !help, …)
    -> construct DiscordRenderer + on_turn_start (typing indicator)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft-threshold notice)  # each guarded
    -> renderer.close() + session release   # in finally

``on_interaction`` resolves interactive tool approvals (``a:<rid>:<1|0>`` ->
``DiscordApprovalDecider.resolve_global``) and re-injects ``[OPTIONS:]``
choices (``opt:<i>``) as fresh turns.

Dependency direction is ``discord -> messaging`` (allowed). The security
``tool_gate`` and spawn auto-approve are wired inline off ``ctx_builder.hooks``
(channel-neutral) so this module never imports ``kiro_crew.slack``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.dashboard.state import row_mid
from kiro_crew.discord.attachments import (
    append_attachment_context,
    process_discord_attachments,
)
from kiro_crew.discord.commands import (
    ConversationState,
    build_help_text,
    is_bare_mid_turn_override,
    parse_command,
    parse_command_argument,
    parse_mid_turn_override,
    unknown_command_usage,
)
from kiro_crew.discord.renderer import (
    DiscordApprovalDecider,
    DiscordRenderer,
    build_model_components,
    session_provenance_tag,
)
from kiro_crew.discord.session_resume import (
    DiscordSessionResume,
    ResumeReleaseError,
    RoutingDecision,
)
from kiro_crew.discord.transport import DISCORD_CAPABILITIES
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.history import mint_row_mid
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.messaging.attachments import IngestLimits
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.commands import stop_running_turn
from kiro_crew.messaging.dispatch import (
    build_auto_approve,
    build_directive_consumer,
    delivery_is_muted,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import (
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    rebind_conversation_location,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.messaging.renderer import Renderer, SilentRenderer
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.messaging.upload_gate import live_dashboard_slot, uploads_restricted
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import MonitorDispatchResult
from kiro_crew.safety_override import describe_grant_lifetime, safety_override
from kiro_crew.security import redact_credentials, redact_exfiltration_urls
from kiro_crew.sel import sel
from kiro_crew.session import SessionBusyError
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.session_map import ConversationOwnershipConflict
from kiro_crew.stats import Stats

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from kiro_crew.context import ContextBuilder
    from kiro_crew.discord.client import DiscordClient, DiscordInteraction
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager

    #: Where a command handler's single reply goes. A ``!`` text command binds
    #: this to a channel message; a registered slash command binds it to the
    #: interaction's own (ephemeral) callback. Handlers reply exactly once, so
    #: neither binding needs a followup route.
    ReplyFn = Callable[[str], Awaitable[None]]

from kiro_crew.messaging.queue_receipt import (
    ATTACHMENT_PLACEHOLDER,
)
from kiro_crew.messaging.queue_receipt import MAX_COLLAPSE as _MAX_COLLAPSE
from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI
from kiro_crew.messaging.queue_receipt import (
    ReceiptQueue,
    ReceiptSurface,
)

logger = logging.getLogger(__name__)


class _MonitorGenerationChanged(Exception):
    """The exact Discord conversation authorized for a wake was replaced."""


# Canonical kiro-cli agent fallback so Discord sessions load kirocrew-core
# (spawn_run etc.) — mirrors the Slack/Telegram paths.
_DEFAULT_KIROCREW_AGENT = "kirocrew"

#: Values that mean "let the backend pick" rather than naming an agent. The
#: dashboard writes ``"default"`` into most session metadata, and ``"auto"`` is
#: its sibling sentinel (see ``dashboard/handlers/agents.py``). Neither is a
#: kiro-cli mode, so neither may reach ACP ``session/set_mode``.
_AGENT_SENTINELS = frozenset({"default", "auto"})

# Keep queue collapse within the shared ingestion layer's per-turn file cap.
_MAX_COLLAPSED_ATTACHMENTS = IngestLimits().max_attachments

#: Commands that still run while this conversation owes the user a detach notice.
#: Everything else targets a session or is a plain turn and must be refused until
#: the user has been told — including a bare message, whose ``cmd`` is ``None``.
_DETACH_EXEMPT_COMMANDS = frozenset({"new", "unlink", "sessions", "help", "status"})

#: Refusal for an option press whose provenance tag does not name the
#: conversation's CURRENT target session (rebound, `!new`, or idle-rotated).
#: One constant, two gate sites (pre-busy and post-rotation), so the wording
#: cannot drift between them.
_STALE_OPTIONS_REFUSAL = (
    "🔘 These buttons belong to a conversation this chat has since moved away "
    "from, so your choice was NOT applied. Type it as a message instead, or "
    "run `!sessions` to reattach the session that asked."
)

#: Refusal for a pre-provenance button (bare ``opt:<i>``): Discord replays old
#: components indefinitely, so an untagged press can never prove which session
#: it belongs to — fail closed rather than route it by current binding.
_UNTAGGED_OPTIONS_REFUSAL = (
    "🔘 These buttons predate a session-safety update, so which conversation "
    "they belong to can no longer be verified and your choice was NOT applied. "
    "Type it as a message instead."
)

#: Refusal for a valid press whose target session is mid-turn. A press must
#: never be queued or steered: the busy queue stores bare text and the drain
#: replays it WITHOUT the provenance tag, so a `!new` or idle rotation between
#: enqueue and drain would execute the model-authored choice in a conversation
#: the tag never named. Refusing is the only shape that keeps the provenance
#: guarantee end to end.
_BUSY_OPTIONS_REFUSAL = (
    "🔘 That conversation is busy with another turn, so your choice was NOT "
    "applied. Type it as a message once the turn finishes."
)

# How long a !model picker stays pressable, and how many pickers are retained.
# Both bound unbounded growth (one entry per press-less !model), they are not UX
# knobs: an expired or evicted picker answers "reopen !model" rather than acting
# on a stale list. Mirrors the Telegram dispatcher.
_MODEL_PICKER_TTL_SECS = 300.0
_MODEL_PICKER_MAX = 50
#: Buttons a picker shows. Discord allows 5 buttons per action row and 5 rows,
#: so 25 is the platform ceiling; 24 leaves the Auto row inside it.
_MODEL_PICKER_LIMIT = 24

#: Commands whose whole effect is one reply, so the text and slash surfaces can
#: share a handler and differ only in where that reply goes. Session-scoped
#: commands are deliberately absent: they need the resume-binding refusal and the
#: mid-turn ladder that ``handle_message`` owns.
_REPLY_COMMANDS = frozenset({"status"})

_RELEASE_FAILURE = (
    "⚠️ Couldn't save the session release, so the command was NOT completed. "
    "Fix the gateway's storage problem, then retry."
)


@dataclass
class _ModelPicker:
    """A posted !model button set, resolving a button index back to a model id."""

    scope_id: str
    channel_id: str
    message_id: str
    created_at: float
    #: ``(model_id, label)`` in button order. ``model_id`` "" is the Auto row.
    choices: tuple[tuple[str, str], ...]


class DiscordDispatcher:
    """Coordinates Discord turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-user conversation state
    (generation counter + soft-threshold flag). ``handle_message`` is wired as
    the transport's dispatch callback; ``on_interaction`` is wired as the
    client's button handler. ``client`` is set by the gateway after
    construction.
    """

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        ctx_builder: "ContextBuilder",
        cfg: "KiroCrewConfig",
        allowed_user_ids: set[str],
        allowed_thread_ids: set[str] | None = None,
        agent: str | None = None,
        conv_log: "ConversationLog | None" = None,
        approval_mode: str = APPROVAL_INTERACTIVE,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self._allowed = set(allowed_user_ids or ())
        self._allowed_threads = set(allowed_thread_ids or ())
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        self.client: "DiscordClient | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)
        # The mid-turn queue receipt + the lock serializing it against the
        # end-of-turn drain, shared with Telegram via messaging/queue_receipt.py.
        self._queue = ReceiptQueue()
        # session_key -> the running turn's renderer (for steer chips).
        self._active_renderers: dict[str, DiscordRenderer] = {}
        # channel_id -> (lock, in-flight deciders); dropped when the last one leaves.
        self._routing_locks: dict[str, tuple[asyncio.Lock, list[int]]] = {}
        # A message in governance predates a refusal but is not a decider yet.
        self._routing_checks: dict[str, int] = {}
        self._session_resume = DiscordSessionResume(
            sessions,
            conv_log,
            self._allowed,
        )
        # Kept as a direct alias for diagnostics/tests; the controller owns it.
        self._session_pickers = self._session_resume.pickers
        # scope_id -> the model id the user picked ("" == Auto). Held in memory
        # only: it is a per-run preference, and persisting it would outlive the
        # advertised set it was chosen from.
        self._model_pref: dict[str, str] = {}
        # "<channel_id>:<message_id>" -> the picker posted on that message.
        self._model_pickers: dict[str, _ModelPicker] = {}

    def register_allowed_thread(self, thread_id: str) -> None:
        """Authorize interactions in a thread created by the inbound transport."""
        self._allowed_threads.add(thread_id)

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(
        self,
        msg: InboundMessage,
        *,
        drain: bool = True,
        interpret_commands: bool = True,
        origin_tag: str = "",
        monitor_completion: MonitorCompletionHook | None = None,
        monitor_session_key: str | None = None,
    ) -> MonitorDispatchResult | None:
        """Drive one authorized inbound message through TurnDriver end-to-end.

        ``interpret_commands`` says whether *text* may execute as a command
        (model-authored text never may). Resume routing is consulted when
        ``interpret_commands`` is true OR the turn carries an ``origin_tag`` —
        the tag implies routing, because validating it requires resolving the
        binding to compare keys. The callers that dispatch with commands off
        and no tag DEPEND on the skip: a queue drain replays messages that were
        accepted for the native session while it was busy (a resumed session's
        busy turn refuses instead of queueing, so a drained item's affinity is
        native by construction), and an AutoNudge fire targets the native key
        its loop resolved and rotation-checked — routing either into a binding
        created later would run them in a session that never queued or armed
        them. An ``[OPTIONS:]`` press dispatches with commands off but a
        non-empty tag: the buttons were rendered on the bound session's own
        reply, so the choice belongs to that session even though its label must
        not execute as a command.

        ``origin_tag`` is the provenance stamp a pressed option button carried
        (see :func:`~kiro_crew.discord.renderer.session_provenance_tag`). When
        non-empty, the turn runs ONLY if the session it resolves to is the one
        that posted the buttons — checked before the busy path (so a stale press
        cannot be queued and replayed tag-less) and AGAIN after idle/daily
        rotation (so it cannot run under a generation the tag never named).
        Discord replays old components indefinitely, so without this check a
        button minted by one session would inject its model-authored choice into
        whatever session the conversation was later rebound to (`!unlink` +
        `!sessions`), or into a post-`!new` conversation that never asked the
        question. An option press ALWAYS supplies a tag: untagged
        (pre-provenance) presses are refused at the interaction boundary in
        :meth:`on_interaction`, never dispatched here.
        """
        assert self.client is not None, "DiscordDispatcher.client must be set"
        monitor_result = (
            MonitorDispatchResult.UNAVAILABLE if monitor_completion is not None else None
        )
        channel_id = msg.conversation_id
        self._routing_checks[channel_id] = self._routing_checks.get(channel_id, 0) + 1
        # Inbound channels-governance gate (off-loop). The startup gate only stops
        # a transport from CONNECTING; a host-profile deny added after it connected
        # would otherwise keep dispatching inbound messages until restart. Recheck
        # per message so a runtime deny takes effect immediately — silently drop
        # (no reply) on deny, matching how an unauthorized user is ignored.
        try:
            permitted = await channel_inbound_permitted("discord")
        finally:
            remaining = self._routing_checks[channel_id] - 1
            if remaining:
                self._routing_checks[channel_id] = remaining
            else:
                self._routing_checks.pop(channel_id)
        if not permitted:
            logger.info("discord inbound dropped: denied by channels governance policy")
            return monitor_result
        user_id = msg.user_id
        thread_id = msg.thread_id or ""
        scope_id = self._scope_id(user_id, thread_id)
        text = msg.text

        # Attachments make this a content-bearing turn, not a control command.
        # Otherwise a caption such as ``!help`` would intercept before ingestion
        # and silently discard the attached file — the exact class of bug this
        # path is meant to eliminate.
        interpret_as_command = interpret_commands and not msg.attachments

        # Per-message mid-turn override (!queue/!steer) — see the Telegram
        # dispatcher for the full precedence rationale.
        override_mode = None
        if interpret_as_command and parse_command(text) is None:
            override_mode, text = parse_mid_turn_override(text)

        # ── Command intercept (no LLM session needed) ──
        cmd = parse_command(text) if interpret_as_command and override_mode is None else None
        # `!compact` and `!stop` act on the resolved session, so after a binding was
        # destroyed they would compact or cancel the NATIVE DM session while the user
        # believes they drive the resumed one; deciding here makes that structural.
        route = RoutingDecision()
        wants_routing = interpret_commands or bool(origin_tag)
        if wants_routing and cmd not in _DETACH_EXEMPT_COMMANDS:
            async with self._routing_turn(channel_id) as queued:
                route = await self._session_resume.route(channel_id)
                if route.refusal is not None:
                    # Settle only once the refusal landed AND nobody who predates it
                    # is still in governance or queued: otherwise that message could
                    # route into a transcript the user never chose.
                    landed = await self.client.send_message(channel_id, route.refusal)
                    if landed and len(queued) == 1 and not self._routing_checks.get(channel_id):
                        await self._session_resume.settle(channel_id, route)
                    return monitor_result
        if cmd == "new":
            try:
                left_resumed = await self._session_resume.leave_resumed_session(channel_id)
            except ResumeReleaseError:
                await self.client.send_message(channel_id, _RELEASE_FAILURE)
                return monitor_result
            self._conv.bump_gen(scope_id)
            message = "✅ New conversation started."
            if left_resumed is not None:
                message = "✅ New conversation started — left the resumed session."
            await self.client.send_message(channel_id, message)
            return monitor_result
        if cmd == "compact":
            self._conv.clear_awaiting(scope_id)
            await self._handle_compact(user_id, channel_id, thread_id, route.resumed_key)
            return monitor_result
        if cmd == "sessions":
            # DM-ONLY. The owner gate answers WHO may resume, not WHERE the
            # result may be shown: in an allow-listed guild thread the picker
            # would post dashboard session TITLES and the bind would replay five
            # transcript messages, making private history readable by every
            # member of that thread. Resume is inherently a private-surface
            # operation, so refuse outside a DM rather than redacting harder.
            if thread_id:
                await self.client.send_message(
                    channel_id,
                    "🔒 `!sessions` works only in a direct message — it lists and "
                    "replays private dashboard conversations, so it will not post "
                    "them into a shared thread. DM me instead.",
                )
                return monitor_result
            await self._session_resume.show_picker(
                self.client,
                user_id,
                channel_id,
                query=parse_command_argument(text),
            )
            return monitor_result
        if cmd == "link":
            await self._handle_link(user_id, channel_id, thread_id, route.resumed_key)
            return monitor_result
        if cmd == "unlink":
            await self._handle_unlink(user_id, channel_id, thread_id)
            return monitor_result
        if cmd == "help":
            await self.client.send_message(channel_id, build_help_text())
            return monitor_result
        if cmd == "stop":
            await self._handle_stop(user_id, channel_id, thread_id, route.resumed_key)
            return monitor_result
        if cmd in _REPLY_COMMANDS:
            await self._run_reply_command(
                cmd,
                self._channel_reply(channel_id),
                user_id=user_id,
                thread_id=thread_id,
                text=text,
            )
            return monitor_result
        if cmd == "model":
            await self._handle_model(
                channel_id,
                scope_id,
                route.resumed_key or self._session_key(user_id, thread_id),
                parse_command_argument(text),
            )
            return monitor_result
        # A lone `!queue` / `!steer` is a directive missing its message body, and
        # an unrecognized `!token` is a mistyped command. Both would otherwise be
        # forwarded verbatim, and the model answers the literal string — which
        # reads as a broken feature rather than a typo. Gated on
        # ``interpret_as_command`` so a caption on an attachment is never read as
        # either: that would answer with usage and silently drop the file. Gated
        # on ``override_mode is None`` because a directive WITH a body has already
        # been stripped off `text`, so what is left is the user's real message.
        if interpret_as_command and override_mode is None:
            if is_bare_mid_turn_override(text):
                await self.client.send_message(
                    channel_id,
                    "Those take a message: `!queue <msg>` or `!steer <msg>`.",
                )
                return monitor_result
            usage = unknown_command_usage(text)
            if usage:
                await self.client.send_message(channel_id, usage)
                return monitor_result

        # ── Mid-turn concurrency: check the CURRENT-generation key BEFORE any
        # idle/daily rotation (see the Telegram dispatcher's rationale). ──
        # ``resumed_key`` comes from the decision above and is NOT re-resolved: a
        # second resolver call let an unlink landing mid-decision route silently.
        resumed_key = route.resumed_key
        derived_session_key = resumed_key or self._session_key(user_id, thread_id)
        if monitor_session_key is not None:
            if monitor_completion is None or derived_session_key != monitor_session_key:
                return MonitorDispatchResult.UNAVAILABLE
            session_key = monitor_session_key
        else:
            session_key = derived_session_key
        if origin_tag and session_provenance_tag(session_key) != origin_tag:
            # The pressed button was minted by a session this conversation no
            # longer targets (rebound via `!unlink`+`!sessions`, or rotated via
            # `!new`). Running it would inject that session's model-authored
            # choice into an unrelated transcript. The gate sits BEFORE the busy
            # check so a stale press can neither run nor be QUEUED — `_handle_busy`
            # enqueues raw text, and the drain replays it without the tag, so a
            # queued stale press would execute unchecked later.
            await self.client.send_message(channel_id, _STALE_OPTIONS_REFUSAL)
            return monitor_result
        if self.sessions.is_busy(session_key):
            if origin_tag:
                # A tagged press must never enter the busy path: `_handle_busy`
                # enqueues BARE TEXT and the drain replays it without the tag,
                # so a `!new` or idle rotation between enqueue and drain would
                # execute the choice in a conversation the tag never named —
                # and steer mode would inject it mid-turn with no check at all.
                # Refuse instead; the user can re-press or type once the turn
                # ends. This also covers the resumed-busy case below, with a
                # press-specific remedy instead of the typed-message one.
                await self.client.send_message(channel_id, _BUSY_OPTIONS_REFUSAL)
                return monitor_result
            if monitor_completion is not None:
                # This is the dispatcher's concurrency boundary. A synthetic
                # monitor wake must retry its durable claim, never steer or
                # queue itself into an unrelated in-flight turn.
                return MonitorDispatchResult.BUSY
            if resumed_key is not None:
                # Do NOT queue or steer into a resumed session's running turn.
                # ``_drain_queue`` is only ever called from the tail of a
                # DISCORD-driven turn; the dashboard turn loop has no knowledge
                # of this queue, so a message enqueued while the dashboard is
                # driving would sit until some later Discord turn and then
                # execute out of order. Refusing is honest and recoverable.
                await self.client.send_message(
                    channel_id,
                    "⏳ That session is busy with a turn started elsewhere. "
                    "Send it again once it finishes, or `!unlink` to go back to "
                    "your own conversation.",
                )
                return monitor_result
            await self._handle_busy(session_key, msg, text, override_mode)
            return monitor_result

        if monitor_completion is None:
            self._conv.maybe_rotate(
                scope_id,
                time.time(),
                idle_minutes=self.cfg.messaging.idle_reset_minutes,
                daily_reset_hour=self.cfg.messaging.daily_reset_hour,
            )
        if monitor_session_key is not None:
            # The gateway authorized one exact conversation generation. Recheck
            # immediately before the non-waiting claim, then claim that key rather
            # than deriving whatever generation a concurrent ``!new`` created.
            if self._session_key(user_id, thread_id) != monitor_session_key:
                return MonitorDispatchResult.UNAVAILABLE
            session_key = monitor_session_key
        else:
            session_key = resumed_key or self._session_key(user_id, thread_id)
        if origin_tag and session_provenance_tag(session_key) != origin_tag:
            # REVALIDATE against the FINAL key: ``maybe_rotate`` above can bump
            # the native generation between the pre-busy gate and here, and the
            # turn must not run under a key the tag never named — the same
            # invariant `!new` enforces, applied to the idle/daily reset. The
            # pre-busy gate is kept too: it is what stops a stale press from
            # being enqueued or probing busy state, which this later check
            # cannot do.
            await self.client.send_message(channel_id, _STALE_OPTIONS_REFUSAL)
            return monitor_result
        chan_id = f"discord:{channel_id}" if thread_id else f"discord:{user_id}"
        agent = self._resolve_agent()
        _acquired = False
        provider = None
        is_new = False
        resumed = False
        if monitor_completion is not None:
            if resumed_key is not None:
                return MonitorDispatchResult.UNAVAILABLE
            try:
                provider, is_new, resumed = await self.sessions.get_or_create(
                    session_key,
                    agent=agent,
                    channel_id=chan_id,
                    wait_if_busy=False,
                )
            except SessionBusyError:
                return MonitorDispatchResult.BUSY
            except SessionClosingError:
                return MonitorDispatchResult.BUSY
            except Exception:
                logger.exception("Discord monitor session claim failed")
                return MonitorDispatchResult.UNAVAILABLE
            _acquired = True
        elif resumed_key is not None:
            # A resumed session must run as ITSELF, not as Discord's agent. On a
            # cold start get_or_create applies the agent we pass, so handing it
            # the Discord default would load the dashboard conversation's
            # transcript and then run it under a different system prompt — and a
            # different allowedTools set, which is a permission-boundary change,
            # not just a tone change. get_metadata touches the filesystem, so it
            # goes off-loop. Fall back to the Discord agent only when the
            # conversation recorded none.
            persisted = await asyncio.to_thread(self._persisted_agent, resumed_key)
            if persisted:
                agent = persisted

        try:
            decider = (
                DiscordApprovalDecider(session_key=session_key)
                if self.approval_mode == APPROVAL_INTERACTIVE
                else None
            )
            # Both render toggles are read PER TURN rather than off the boot-time
            # config, so changing one in the dashboard takes effect on the next
            # message instead of at the next restart. That matches Slack, which reads
            # the same two fields per message, and it is why the settings API reports
            # them as needing no restart.
            # Off-loop: the per-turn read is a real config.json read plus schema
            # validation, so on the gateway's single loop it stalls every other chat
            # and heartbeat task on a slow disk. Reading fresh is the point of the
            # helper, so it cannot be cached away; it can only be moved off the loop.
            render_cfg = await asyncio.to_thread(self._render_config)
            renderer = DiscordRenderer(
                self.client,
                channel_id,
                DISCORD_CAPABILITIES,
                session_key=session_key,
                uploads_allowed=not await self._uploads_restricted(session_key),
                reactions_enabled=render_cfg[0],
                show_thinking=render_cfg[1],
                # The phase emoji goes on the USER'S OWN message, the way Slack's
                # controller keys on the inbound `ts`: it is a progress marker on the
                # thing that started the turn, so it costs no extra bubble. Without
                # this id the ladder cannot arm at all, which is exactly what an
                # unpassed constructor argument looks like from the outside: a feature
                # that appears wired and silently does nothing. A synthetic turn (an
                # option-button re-dispatch, an AutoNudge fire) carries no inbound
                # message, so it has nothing to react to and the ladder stays down.
                react_message_id=getattr(msg, "message_id", ""),
            )
            # Discord runs its OWN copy of the turn loop instead of going through
            # ``messaging.dispatch.drive_turn``, so the disconnect gate there does not
            # reach it — without this the dashboard control changed nothing here but
            # its own label. The turn still runs and the inbound message still lands in
            # the session: the binding is retained by design, and the dashboard is
            # where that user is now working. Only the writes back are dropped.
            muted = delivery_is_muted(self.sessions, session_key, DiscordRenderer.channel_type)
            # Handed to the driver AND closed in the finally, rather than reassigning
            # ``renderer``: the concrete renderer's ``close`` is not inert — it posts an
            # error placeholder when the turn produced no output, which a muted turn by
            # definition did, so closing the real one leaked "⚠️ Error" into the
            # conversation the user had just disconnected.
            out_renderer: Renderer = (
                SilentRenderer(DISCORD_CAPABILITIES, DiscordRenderer.channel_type)
                if muted
                else renderer
            )
            if not muted:
                # Published for mid-turn steer chips. Deliberately NOT published when
                # muted: the steer path calls the channel-specific ``note_steer`` and
                # already skips cleanly when there is no entry, so leaving it out both
                # silences the chip in a disconnected conversation and keeps that
                # channel-local API off the shared substitute.
                self._active_renderers[session_key] = renderer
        except Exception:
            # Monitor delivery owns its lease before renderer setup, unlike an
            # ordinary turn. Fail closed and release it without changing the
            # ordinary dispatcher's historical setup-error behavior.
            if _acquired:
                self.sessions.release(session_key)
                logger.exception("Discord monitor pre-turn setup failed")
                return MonitorDispatchResult.BUSY
            raise
        attachment_temp_paths: list[str] = []

        # Everything acquire-dependent runs INSIDE the try so the finally
        # always finalizes the renderer; release() is gated on _acquired.
        # Mirrors telegram/transport_dispatch.py.
        try:
            # Typing indicator BEFORE the cold start. get_or_create can spend
            # seconds spawning/handshaking an ACP session, and until this runs
            # Discord shows nothing at all, so the user sees dead air and assumes
            # the bot missed the message. This is the ordering the shared
            # skeleton documents ("typing indicator before cold start" in
            # messaging/dispatch.py) and the one telegram/transport_dispatch.py
            # still uses. Safe here: on_turn_start only spawns a background
            # refresh task, is idempotent (the driver calls it again later), and
            # the enclosing finally always finalizes the renderer, so an early
            # return below cannot leak a typing loop.
            # Skipped when muted: a disconnected conversation must not
            # even show a typing indicator.
            if not muted:
                await renderer.on_turn_start()
            # Acquire before attachment I/O. A large download yields repeatedly;
            # leaving the session idle in that window lets a later message run
            # first and persist the conversation in reverse order.
            if not _acquired:
                # ``model`` applies only when this call COLD-STARTS the session: the
                # fast path returns a reused session before it consults the argument.
                # That is exactly what ``!model``'s reply promises ("applies to your
                # next conversation") when one is already live, so the two agree.
                provider, is_new, resumed = await self.sessions.get_or_create(
                    session_key,
                    agent=agent,
                    channel_id=chan_id,
                    model=self._model_pref.get(scope_id) or None,
                )
                _acquired = True
            assert provider is not None
            renderer.authorize_upload_root(provider.cwd)
            # The turn footer's context chip reads usage off the session provider,
            # which only exists once the session is acquired. Unbound, the chip
            # cannot render at all and the footer silently ships without the one
            # number that tells a user when to run `!compact`.
            renderer.bind_context_source(provider)
            if msg.attachments:
                attachment_result = await process_discord_attachments(self.client, msg.attachments)
                attachment_temp_paths = list(attachment_result.temp_paths)
                text = append_attachment_context(text, attachment_result)
            if not text:
                return monitor_result
            # New-session bookkeeping belongs to THIS conversation's own session
            # only. A resumed dashboard session is pre-existing by definition, and
            # `get_or_create` returns is_new whenever its ACP session is merely
            # COLD — which is the normal case, since the picker lists *history*,
            # not live sessions. Treating it as new caused two routine data
            # losses on the very first resumed message:
            #   • set_channel writes through to the legacy slack_channel_id field,
            #     stamping `discord:<id>` onto the dashboard session. That survives
            #     `!unlink` (which clears only `mirror`), so get_mirror_link then
            #     synthesizes a bogus Slack link and every later `!sessions` pick
            #     of that session is refused with "already active on Slack".
            #   • _persist_turn(is_new=True) calls set_title, replacing the
            #     dashboard conversation's title with the first 40 characters of
            #     the Discord message.
            is_new_own_session = is_new and resumed_key is None
            if is_new_own_session:
                await self.sessions.set_channel(session_key, chan_id)
            if resumed_key is None:
                # Record the conversation's REAL send target so unattended
                # output about the session — the auto-compact notice — can reach
                # the user. `chan_id` above is the legacy namespaced bucket and
                # carries the user id for a DM, which is not a postable channel.
                # Skipped for a resumed dashboard session: its own surface owns
                # the notice, and stamping it here would bind a dashboard entry
                # to Discord.
                # An in-memory dict assignment on the session manager, not a
                # persisted field: the target is only needed while the session
                # is live, so no disk I/O and no cross-thread state land on this
                # turn path.
                self.sessions.set_origin_link(
                    session_key, ChannelLink("discord", channel_id=channel_id)
                )
                # Bind this conversation as the session's outbound mirror so a
                # turn the user later takes from the dashboard is delivered back
                # here. Slack gets this from its own per-turn thread binding;
                # Discord had it only behind an explicit `!link`, so the chat sat
                # there looking dead while the conversation continued elsewhere.
                # Inside the `resumed_key is None` branch with set_origin_link,
                # for the same reason: a resumed session's own surface owns its
                # output and `!link` refuses there too, so the automatic path must
                # not do what the explicit one declines. (It would also decline on
                # its own, having found the resume binding for this very channel —
                # the placement is what keeps that from being load-bearing.)
                self._bind_origin_mirror(session_key, channel_id)
            # Publish this turn's session identity so managed MCP tools resolve
            # X-Session-Key; one shared writer lives in messaging.identity.
            await publish_turn_identity(self.sessions, session_key)
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                self.ctx_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=chan_id,
                agent=agent,
                resumed=resumed,
                runtime_source="discord",
            )

            # PreToolUse security gate (channel-neutral, off ctx_builder.hooks).
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

            def _begin_monitor_turn() -> None:
                if (
                    monitor_session_key is not None
                    and self._session_key(user_id, thread_id) != monitor_session_key
                ):
                    raise _MonitorGenerationChanged
                self.sessions.begin_turn(session_key)

            driver = TurnDriver(
                provider,
                out_renderer,
                approval_mode=self.approval_mode,
                decider=decider,
                # Preserve the auto_approve_subagent_spawn hook for spawn_run.
                # The shared builder keys on canonical event identity
                # (tool_name/is_shell), never the model-authored title.
                auto_approve_tool=build_auto_approve(self.ctx_builder),
                # The operator's process-wide grant, read per permission request --
                # the same predicate every other shipped channel passes. Without it
                # Discord is the one surface where arming YOLO from the dashboard is
                # INERT, so an unattended run still stops on every tool prompt.
                # Does not weaken the gate above: `_tool_gate`'s hard deny runs ahead
                # of this rung in TurnDriver, so a policy refusal still wins.
                auto_approve_session=lambda: safety_override().is_active(),
                tool_gate=_tool_gate,
                # Session-directive consumer: monitor_start / autonudge_stop /
                # ... return a marker the driver decodes; apply it against THIS
                # turn's session key (dashboard-only directives stay refused
                # for channel sessions).
                directive_consumer=build_directive_consumer(
                    session_key=session_key, sessions=self.sessions, dispatcher=self
                ),
                audit_session_key=session_key,
                audit_agent=agent or "kirocrew",
                closing_gate=(
                    _begin_monitor_turn
                    if monitor_completion is not None
                    else lambda: self.sessions.begin_turn(session_key)
                ),
                monitor_completion=monitor_completion,
            )
            accumulated = await driver.run(full_message)
            if monitor_completion is not None:
                if not monitor_completion.accepted:
                    return MonitorDispatchResult.UNAVAILABLE
                monitor_result = MonitorDispatchResult.DISPATCHED

            # ── Post-turn bookkeeping (each guarded — see Telegram). ──
            # A turn that produced text but delivered NONE of it is not a
            # success: the provider answered, the user did not hear it. Recording
            # it as one hides the outage behind a healthy success rate and leaves
            # the transcript claiming a reply the channel never carried. The
            # renderer owns the observable because it owns the sends; a muted
            # conversation runs a SilentRenderer, which never attempts a send and
            # therefore never reports a failure here.
            undelivered = bool(accumulated.strip()) and getattr(
                out_renderer, "delivery_failed", False
            )
            if undelivered:
                logger.warning(
                    "discord: the turn for %s produced output but no message reached "
                    "Discord; recording it as a failure",
                    session_key,
                )
                await self.sessions.record_failure(session_key)
            else:
                self.sessions.record_success(session_key)
            try:
                # Loop-side: put the turn in the live dashboard window FIRST so
                # the dashboard's own save serializes it in chronological
                # position instead of appending it to the foreign tail.
                mirror_mids = self._mirror_turn_to_live_slot(session_key, text, accumulated)
                await asyncio.to_thread(
                    self._persist_turn,
                    session_key,
                    text,
                    accumulated,
                    is_new_own_session,
                    agent=agent,
                    mirror_mids=mirror_mids,
                )
            except Exception:
                logger.warning(
                    "Discord: persist_turn failed session=%s",
                    session_key,
                    exc_info=True,
                )
            if is_new_own_session:
                try:
                    await self._surface_own_session()
                except Exception:
                    logger.warning(
                        "Discord: immediate dashboard session surface failed session=%s",
                        session_key,
                        exc_info=True,
                    )
            try:
                await self._maybe_notice(channel_id, scope_id, session_key, provider)
            except Exception:
                logger.warning(
                    "Discord: maybe_notice failed session=%s",
                    session_key,
                    exc_info=True,
                )
            try:
                sel().log_api_access(
                    caller=f"discord:{user_id}",
                    operation="transport_dispatch.handle",
                    outcome="success",
                    source="discord",
                    resources=f"session={session_key}",
                )
            except Exception:
                logger.debug("Discord: success audit failed", exc_info=True)
        except _MonitorGenerationChanged:
            logger.info(
                "Discord monitor dispatch refused after generation changed for %s",
                session_key,
            )
            return MonitorDispatchResult.UNAVAILABLE
        except SessionClosingError:
            logger.info(
                "Discord monitor dispatch refused during shutdown for %s",
                session_key,
            )
            if monitor_completion is not None:
                return MonitorDispatchResult.BUSY
        except Exception:
            logger.exception("Discord transport_dispatch: error handling message")
            if monitor_completion is not None:
                monitor_result = (
                    MonitorDispatchResult.DISPATCHED
                    if monitor_completion.accepted
                    else MonitorDispatchResult.BUSY
                )
            if _acquired:
                await self.sessions.record_failure(session_key)
        finally:
            # Renderer finalization is best-effort and must NEVER prevent the
            # session release below — a rendering failure (e.g. Discord/proxy
            # returning a malformed body) that also failed finalization would
            # otherwise leave the session permanently busy, blocking every
            # subsequent Discord message and the queue drain.
            try:
                await out_renderer.close()
            except Exception:
                logger.warning(
                    "Discord: renderer.close failed session=%s",
                    session_key,
                    exc_info=True,
                )
            self._active_renderers.pop(session_key, None)
            if _acquired:
                self.sessions.release(session_key)
            await asyncio.to_thread(cleanup_attachments, attachment_temp_paths)

        # Drain anything queued during the turn (queue_mode == "queue").
        if drain:
            await self._drain_queue(session_key, user_id, channel_id, thread_id)
        return monitor_result

    async def _handle_busy(
        self,
        session_key: str,
        msg: InboundMessage,
        text: str,
        override_mode: str | None,
    ) -> None:
        """A message arrived mid-turn: steer the running turn or queue it."""
        assert self.client is not None
        channel_id = msg.conversation_id
        mode = override_mode or self.cfg.messaging.queue_mode
        if mode != "queue" and not msg.attachments:
            provider = self.sessions.get_provider(session_key)
            steer = getattr(provider, "steer", None)
            # Only steer when a turn is GENUINELY in flight (see the Telegram
            # dispatcher for the post-turn-bookkeeping race rationale).
            has_active = getattr(provider, "has_active_turn", None)
            live = has_active is None or bool(has_active())
            steered = bool(
                live
                and getattr(provider, "supports_steer", False)
                and steer is not None
                and await steer(text)
            )
            if steered:
                r = self._active_renderers.get(session_key)
                if r is not None:
                    r.note_steer(text)
                # Instant, no-extra-bubble ack: react to the user's steer
                # message. Best-effort.
                steer_mid = getattr(msg, "message_id", "")
                if steer_mid:
                    try:
                        await self.client.add_reaction(channel_id, steer_mid, _STEER_ACK_EMOJI)
                    except Exception:
                        logger.debug("discord: steer ack reaction failed", exc_info=True)
                return
        # queue mode (or !queue override, or steer unavailable). Atomic
        # enqueue + receipt under self._queue.lock — see the Telegram dispatcher.
        if not await self._enqueue_with_receipt(
            session_key,
            channel_id,
            text,
            attachments=msg.attachments,
        ):
            await self.handle_message(msg)

    async def _drain_queue(
        self, session_key: str, user_id: str, channel_id: str, thread_id: str = ""
    ) -> None:
        """Collapse every message queued during the just-finished turn into ONE
        combined turn (order preserved). See the Telegram dispatcher for the
        lock/ordering rationale."""
        # Iterate rather than recurse: one burst can span multiple
        # attachment-capped turns, and messages arriving during a drained turn
        # join the same FIFO pump instead of waiting for unrelated future input.
        while True:
            texts: list[str] = []
            attachments: list[Any] = []
            remainder: list[tuple[str, str, dict]] = []
            defer_rest = False
            async with self._queue.lock:
                while True:
                    item = self.sessions.dequeue(session_key)
                    if item is None:
                        break
                    item_attachments = list(item[2].get("attachments") or [])
                    exceeds_attachment_cap = bool(
                        texts
                        and item_attachments
                        and len(attachments) + len(item_attachments) > _MAX_COLLAPSED_ATTACHMENTS
                    )
                    if not defer_rest and len(texts) < _MAX_COLLAPSE and not exceeds_attachment_cap:
                        texts.append(item[1])
                        attachments.extend(item_attachments)
                    else:
                        # Once one message no longer fits, defer it and everything
                        # behind it so queue order remains exact.
                        defer_rest = True
                        remainder.append(item)
                for _ts, rtext, rkw in remainder:
                    self.sessions.enqueue(
                        session_key,
                        str(time.time()),
                        rtext,
                        force=True,
                        attachments=list(rkw.get("attachments") or []),
                    )
                if texts:
                    await self._receipt_flip_locked(
                        session_key,
                        channel_id,
                        [text or ATTACHMENT_PLACEHOLDER for text in texts],
                        len(remainder),
                    )
            if not texts:
                return
            if remainder:
                logger.debug(
                    "discord: drain deferred %d message(s) for %s "
                    "to preserve collapse/attachment caps and FIFO order",
                    len(remainder),
                    session_key,
                )
            combined = "\n\n".join(texts)
            await self.handle_message(
                InboundMessage(
                    channel_type="discord",
                    user_id=user_id,
                    conversation_id=channel_id,
                    text=combined,
                    thread_id=thread_id or None,
                    attachments=attachments,
                ),
                drain=False,
                interpret_commands=False,
            )

    # ── Mid-turn queue receipt (single, in-place, persistent record) ───────

    async def _enqueue_with_receipt(
        self,
        session_key: str,
        channel_id: str,
        text: str,
        *,
        attachments: list[Any] | None = None,
    ) -> bool:
        """Atomically enqueue a mid-turn message and create/grow its collapsing
        receipt, under ``self._queue.lock``. Returns True if queued; False if the
        turn finished in the window (caller runs the message as a fresh turn)."""
        assert self.client is not None
        async with self._queue.lock:
            if not self.sessions.enqueue(
                session_key,
                str(time.time()),
                text,
                force=False,
                attachments=list(attachments or []),
            ):
                return False
            # An attachment-only message has no text; show a placeholder rather
            # than a blank entry in the receipt.
            await self._queue.create_or_grow_locked(
                session_key, self._receipt_surface(channel_id), text or ATTACHMENT_PLACEHOLDER
            )
            return True

    async def _receipt_flip_locked(
        self,
        session_key: str,
        channel_id: str,
        answered: list[str],
        deferred: int = 0,
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record. Caller MUST
        hold ``self._queue.lock``."""
        assert self.client is not None
        await self._queue.flip_answering_locked(
            session_key, self._receipt_surface(channel_id), answered, deferred
        )

    def _receipt_surface(self, channel_id: str) -> ReceiptSurface:
        """A receipt surface with this channel's address already bound."""
        # cast, not assert: mypy does not carry an assert-narrowed local
        # into the nested class body below, so the closure would still see
        # ``DiscordClient | None``. The caller path always has a live client.
        client = cast("DiscordClient", self.client)

        class _Surface:
            label = "discord"

            async def send_receipt(self, body: str) -> Any | None:
                return await client.send_message(channel_id, body)

            async def edit_receipt(self, msg_id: Any, body: str) -> None:
                await client.edit_message(channel_id, msg_id, body)

        return _Surface()

    @asynccontextmanager
    async def _routing_turn(self, channel_id: str) -> "AsyncIterator[list[int]]":
        """Serialize one channel's route -> refusal send -> settle, because delivery is
        what retires the refusal. Only THIS channel, never the accepted turn, and not
        `_bind_lock`, which `choose()` holds across its own Discord round-trips."""
        lock, deciders = self._routing_locks.setdefault(channel_id, (asyncio.Lock(), []))
        deciders.append(1)
        try:
            async with lock:
                yield deciders
        finally:
            deciders.pop()
            if not deciders:
                self._routing_locks.pop(channel_id, None)

    async def _handle_stop(
        self,
        user_id: str,
        channel_id: str,
        thread_id: str,
        resumed_key: str | None,
    ) -> None:
        """Hard cancel: abort the in-flight turn and clear everything.

        The cooperative-cancel contract, the lock ordering across ``clear_queue``
        + the receipt finalize, and both replies live in
        :func:`~kiro_crew.messaging.commands.stop_running_turn`; this supplies
        Discord's address and stops the session the turn is actually running
        under, which for a resumed conversation is its owner rather than this
        channel's own DM session.
        """
        assert self.client is not None
        reply = await stop_running_turn(
            self.sessions,
            resumed_key or self._session_key(user_id, thread_id),
            queue=self._queue,
            surface=self._receipt_surface(channel_id),
        )
        await self.client.send_message(channel_id, reply)

    # ── Button handler (client's on_interaction) ───────────────────────────

    async def on_interaction(self, itx: "DiscordInteraction") -> None:
        """Route an interaction: a slash command, an approval, or a choice."""
        assert self.client is not None
        # Auth first (deny-by-default short-circuit).
        if not self._authorized(itx.user_id):
            return
        # Guild interactions are accepted only in an allow-listed channel that
        # Discord confirms is a thread. This mirrors transport.receive().
        thread_id = itx.channel_id if itx.guild_id else ""
        in_allowed_thread = bool(thread_id) and (
            thread_id in self._allowed_threads and await self.client.is_thread_channel(thread_id)
        )
        if itx.guild_id and not in_allowed_thread:
            # A COMMAND gets an ephemeral explanation rather than silence. A
            # dropped interaction is not invisible to the user: Discord shows its
            # own red "did not respond" with no reason, which reads as the bot
            # being broken. The reply is ephemeral, so naming the rule discloses
            # nothing to the rest of the channel. Stateless commands are still
            # refused HERE rather than answered, because a shared channel is a
            # wider disclosure boundary than the thread allow-list grants and
            # turns are deliberately never run in one.
            if itx.is_command:
                await self.client.respond_interaction(
                    itx.interaction_id,
                    itx.interaction_token,
                    "🔒 Commands run in a direct message or an approved thread. "
                    "Post here and I will open a thread, or DM me.",
                    ephemeral=True,
                )
            return
        # A slash command is answered by its OWN callback, so it must not be
        # pre-acked: DEFERRED_UPDATE_MESSAGE is a component-only callback type,
        # and spending the one permitted first response on it would leave the
        # command's actual reply with no route. It also runs the governance gate
        # BEFORE responding rather than after, unlike the button path below.
        # The order matters and the trade-off is deliberate: a governance check
        # slower than Discord's ~3s callback window makes the command visibly
        # fail, where acking first would have let a policy-denied command run.
        # Failing visibly is the correct direction for a fail-closed gate.
        if itx.is_command:
            if not await channel_inbound_permitted("discord"):
                logger.info("discord command dropped: denied by channels governance policy")
                # Named, not silent, for the same reason as the guild refusal
                # above. The wording stays generic: the governance profile is the
                # operator's ceiling and its contents are not the user's to read.
                await self.client.respond_interaction(
                    itx.interaction_id,
                    itx.interaction_token,
                    "🔒 The Discord channel is currently disabled by policy.",
                    ephemeral=True,
                )
                return
            await self._on_command_interaction(itx)
            return

        # Ack FIRST (after auth) to dismiss Discord's "interaction failed" state —
        # the governance check below does off-loop profile-store I/O that can, on a
        # slow FS, exceed Discord's ~3s interaction-ack deadline. Acking is a no-op
        # UI dismissal; it does NOT resolve the approval or start a turn.
        await self.client.ack_component_interaction(itx.interaction_id, itx.interaction_token)

        data = itx.custom_id or ""

        # Inbound channels-governance gate (off-loop) — a button press RESOLVES a
        # tool approval (executes the governed tool) or injects an [OPTIONS:]
        # choice (starts a turn), so it must pass the SAME gate as a message BEFORE
        # any resolution. Without it, an admin deny added after connect could still
        # execute a governed tool via a stale approval button.
        # EXCEPTION: an explicit REJECT of a tool approval ("a:...:0") is a DENIAL —
        # exactly what a channels-deny wants — so let it resolve the pending future
        # as refused rather than silently dropping it (which would strand the
        # kiro-cli approval until timeout, ~300s). Approve presses and [OPTIONS:]
        # turns stay blocked.
        _is_reject_press = data.startswith("a:") and data.rpartition(":")[2] == "0"
        if not _is_reject_press and not await channel_inbound_permitted("discord"):
            logger.info("discord interaction dropped: denied by channels governance policy")
            return

        # Session picker: "s:<nonce>:<index>". The controller binds the nonce
        # to the owner, channel, message, TTL, and exact server-side choice list.
        # Guild-side refusal is defence in depth: show_picker already refuses
        # outside a DM, so a guild `s:` press means a stale or forged button —
        # honouring it would replay private transcript into a shared thread.
        if data.startswith("s:"):
            if itx.guild_id:
                return
            await self._session_resume.choose(self.client, itx, data)
            return

        # Tool-approval decision: "a:<request_id>:<nonce>:<1|0>". The nonce is
        # validated by resolve_global — a stale button (reused request ID from
        # before a restart, or an earlier prompt) fails closed.
        if data.startswith("a:"):
            body = data[2:]
            head, _, flag = body.rpartition(":")
            rid, _, nonce = head.rpartition(":")
            approved = flag == "1"
            key = DiscordApprovalDecider.key(
                self._inbound_session_key(itx.user_id, itx.channel_id, thread_id),
                rid,
            )
            resolved = DiscordApprovalDecider.resolve_global(key, approved, nonce=nonce)
            if resolved:
                verdict = "✅ Approved" if approved else "🚫 Denied"
            else:
                # No pending decision — already timed out (deny-by-default) or
                # answered. Don't imply the press took effect.
                verdict = "⌛ This approval already expired."
            await self.client.edit_message(itx.channel_id, itx.message_id, verdict, components=[])
            return

        # Model pick: "m:<index>" into the picker posted on this message. The
        # index resolves against the exact choice list that picker recorded, so a
        # button Discord replays after the advertised set changed cannot apply a
        # model from a stale list.
        if data.startswith("m:"):
            token = f"{itx.channel_id}:{itx.message_id}"
            picker = self._model_pickers.get(token)
            expired = picker is not None and (
                time.time() - picker.created_at > _MODEL_PICKER_TTL_SECS
            )
            try:
                index = int(data[2:])
            except ValueError:
                index = -1
            if picker is None or expired or not (0 <= index < len(picker.choices)):
                # Covers expired, evicted and already-consumed alike — the
                # wording must not claim "expired" for a picker that was simply
                # used, which is what a double-press hits.
                self._model_pickers.pop(token, None)
                await self.client.edit_message(
                    itx.channel_id,
                    itx.message_id,
                    "⌛ This model list is no longer active — send `!model` again.",
                    components=[],
                )
                return
            # Consume the picker BEFORE applying: the switch takes a round-trip,
            # and a second press in that window would otherwise apply twice.
            self._model_pickers.pop(token, None)
            model_id, label = picker.choices[index]
            outcome = await self._apply_model(
                picker.scope_id,
                self._inbound_session_key(itx.user_id, itx.channel_id, thread_id),
                model_id,
            )
            sel().log_api_access(
                caller=itx.user_id or "unknown",
                operation="discord.set_model",
                outcome="allowed",
                source="discord",
                resources=f"model={label}",
            )
            # One edit carries both the result text and the retired buttons, so
            # they never outlive the choice they represent.
            await self.client.edit_message(itx.channel_id, itx.message_id, outcome, components=[])
            return

        # [OPTIONS:] choice: "opt:<i>:<origin-tag>" — label recovered from the
        # button text. A bare "opt:<i>" is a pre-provenance button; Discord
        # replays old components indefinitely, so its originating session can
        # never be verified — refuse it here, fail closed, before any echo
        # suggests the choice was sent.
        if data.startswith("opt:"):
            parts = data.split(":", 2)
            origin_tag = parts[2] if len(parts) == 3 else ""
            choice_text = itx.label
            # Retire the buttons but KEEP the original answer text intact —
            # a components-only PATCH leaves the content unchanged.
            await self.client.edit_message_components(itx.channel_id, itx.message_id, [])
            if not origin_tag:
                await self.client.send_message(itx.channel_id, _UNTAGGED_OPTIONS_REFUSAL)
                return
            if not choice_text:
                await self.client.send_message(
                    itx.channel_id,
                    "⚠️ Couldn't read that choice — please type it instead.",
                )
                return
            # Echo the picked option as a quoted line (a button tap can't
            # render as a real user message), then re-dispatch as a fresh turn.
            await self.client.send_message(itx.channel_id, f"> {choice_text}")
            synthetic = InboundMessage(
                channel_type="discord",
                user_id=itx.user_id,
                conversation_id=itx.channel_id,
                text=choice_text,
                thread_id=thread_id or None,
            )
            # An option label is MODEL-AUTHORED: the agent chose the text of the
            # button, and the press only says which one the user picked. So the
            # payload is turn content, never a command. Interpreting it would let
            # a prompt-injected agent offer `!new` as a choice and have one click
            # discard the conversation, or `!stop` and have it cancel the reply the
            # user was waiting on. Same rule and same reason as the queue drain, which
            # replays with commands off so a queued `!new` reaches the model as
            # literal text instead of executing.
            # UNLIKE the drain, the press still resolves the resumed binding: the
            # buttons sit on the bound session's own reply, so the choice is that
            # session's turn content — its non-empty `origin_tag` is what routes
            # it (see ``handle_message``). Without routing the press ran in the
            # NATIVE DM session — the click answered a question nobody asked
            # there, while the bound session kept waiting (and if the binding died
            # in between, routing now surfaces the Detached refusal instead of a
            # silent native turn). The tag also closes the remaining window: the
            # resolved target must BE the session that posted these buttons.
            await self.handle_message(
                synthetic,
                interpret_commands=False,
                origin_tag=origin_tag,
            )

    # ── Public injection surface ────────────────────────────────────────────
    # Contract for out-of-band callers (AutoNudge fire path, the REST create
    # endpoint, future channel injectors): synthetic turns bypass
    # transport.receive, so authorization and session-key derivation MUST go
    # through these methods — renaming the private helpers behind them breaks
    # loudly here instead of silently at fire time.

    def is_authorized(self, user_id: str) -> bool:
        """Deny-by-default allowlist check for out-of-band (synthetic) turns."""
        return self._authorized(user_id)

    def current_session_key(self, user_id: str) -> str:
        """The user's CURRENT DM session key (dm_scope + ``!new`` generation)."""
        return self._session_key(user_id)

    # ── Helpers ────────────────────────────────────────────────────────────

    def _authorized(self, user_id: str) -> bool:
        # Deny-by-default (interactions bypass transport.receive, so re-check).
        return bool(user_id) and bool(self._allowed) and user_id in self._allowed

    def _render_config(self) -> tuple[bool, bool]:
        """``(reactions_enabled, show_thinking)`` for the turn about to start.

        Blocking (a config.json read plus schema validation), so callers run it
        off the event loop.

        Loaded fresh rather than taken from ``self.cfg``, which is the boot-time
        snapshot: an operator who turns the phase reactions off in the dashboard
        expects the next message to be quiet, not the next restart. A failed load
        keeps the shipped defaults rather than failing the turn, because neither
        toggle is a security control: the loud default is the safe one to fall
        back to for reactions, and the quiet default is the safe one for
        reasoning.
        """
        try:
            discord_cfg = KiroCrewConfig.load().discord
            return bool(discord_cfg.reactions_enabled), bool(discord_cfg.show_thinking)
        except Exception:
            logger.warning("discord: could not read the render toggles", exc_info=True)
            return True, False

    def _resolve_agent(self) -> str:
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _persisted_agent(self, session_key: str) -> str:
        """The agent a session was recorded with, or "" when unknown.

        Blocking (reads the conversation log's metadata) — call via
        ``asyncio.to_thread``. Returns "" on any failure so the caller falls back
        to the channel's own agent rather than the turn failing.

        ``"default"``/``"auto"`` are dashboard sentinels meaning "let the backend
        pick", NOT agent names -- most dashboard sessions record ``"default"``.
        Forwarding one reaches ACP ``session/set_mode``, which rejects it with
        ``Mode 'default' not found`` and fails every resumed turn, so they are
        normalized to "" and the channel's own agent is used instead.
        """
        if self.conv_log is None:
            return ""
        try:
            meta = self.conv_log.get_metadata(session_key)
        except Exception:
            logger.debug(
                "discord: could not read persisted agent for %s", session_key, exc_info=True
            )
            return ""
        recorded = str((meta or {}).get("agent") or "").strip()
        if recorded.lower() in _AGENT_SENTINELS:
            return ""
        return recorded

    @staticmethod
    def _scope_id(user_id: str, thread_id: str = "") -> str:
        return f"thread:{thread_id}" if thread_id else f"user:{user_id}"

    def _session_key(self, user_id: str, thread_id: str = "") -> str:
        scope_id = self._scope_id(user_id, thread_id)
        gen = self._conv.current_gen(scope_id)
        return build_dm_session_key(
            "discord",
            self._resolve_agent(),
            thread_id or user_id,
            gen=gen,
            dm_scope=("per-channel-peer" if thread_id else self.cfg.messaging.dm_scope),
            chat_type=("group" if thread_id else "direct"),
        )

    def _inbound_session_key(
        self,
        user_id: str,
        channel_id: str,
        thread_id: str = "",
    ) -> str:
        resumed = self._session_resume.resumed_session(channel_id)
        return resumed or self._session_key(user_id, thread_id)

    def _seed_gen(self, scope_id: str) -> int:
        if scope_id.startswith("thread:"):
            thread_id = scope_id.removeprefix("thread:")
            bucket = build_dm_session_key(
                "discord",
                self._resolve_agent(),
                thread_id,
                dm_scope="per-channel-peer",
                chat_type="group",
            )
            return self.sessions.max_generation(bucket)
        user_id = scope_id.removeprefix("user:")
        return seed_generation(
            self.sessions,
            channel="discord",
            agent=self._resolve_agent(),
            user_id=user_id,
            dm_scope=self.cfg.messaging.dm_scope,
        )

    def _origin_mirror_link(self, channel_id: str) -> ChannelLink:
        """The mirror location for the conversation a session is being read in.

        One definition shared by the automatic bind, ``!link`` and ``!unlink``: an
        unlink matches an occupied location by VALUE, so a second spelling of
        "this conversation" would let the release miss the binding the bind wrote.

        No ``thread_id``: a Discord thread IS a channel with its own id (the
        inbound path takes ``thread_id`` FROM ``channel_id``), so *channel_id*
        already scopes a thread conversation, and it is also the id the transport
        posts to.
        """
        return ChannelLink("discord", channel_id=channel_id)

    def _bind_origin_mirror(self, session_key: str, channel_id: str) -> None:
        """Mirror this conversation's dashboard tab back to Discord, unasked.

        The rule, the re-assert and the opt-out live in
        :func:`~kiro_crew.messaging.link.bind_origin_mirror`, shared with the
        Telegram dispatcher; this only supplies Discord's spelling of "this
        conversation".

        Synchronous and called ON the loop, like every other session-map
        mutation. Interleaving is ordered by ``session_map._MAP_LOCK`` (held for
        the whole of each guarded mutation, including the ``os.replace``), not by
        the loop; what keeps the call here is that the write is BOUNDED — one
        whole-map rewrite whose cost the loop pays once per conversation, on its
        first turn only. ``test_the_binding_write_stays_on_the_loop_thread``
        ratchets that placement.
        """
        bind_origin_mirror(
            self.sessions,
            key=session_key,
            location=self._origin_mirror_link(channel_id),
        )

    async def _handle_link(
        self,
        user_id: str,
        channel_id: str,
        thread_id: str,
        resumed_key: str | None,
    ) -> None:
        """Re-enable mirroring of this conversation's dashboard tab back here.

        The rebind sequence, its batching, its claim-first ordering and its reply
        live in the shared
        :func:`~kiro_crew.messaging.link.rebind_conversation_location`; this
        supplies Discord's spelling of "this conversation" plus the two refusals
        only a resume-capable channel can hit.
        """
        assert self.client is not None
        # Refuse while a resumed session owns this conversation: linking would
        # rebind the same location and silently strand the resumed session. The
        # owner comes from the turn's routing decision, not a fresh resolve — a
        # second resolve is a second answer, and the gap between them is where a
        # concurrent rebind slips through.
        if resumed_key is not None:
            await self.client.send_message(
                channel_id,
                "⚠️ A resumed session is active here. Send `!unlink` first.",
            )
            return
        try:
            reply = rebind_conversation_location(
                self.sessions,
                key=self._session_key(user_id, thread_id),
                location=self._origin_mirror_link(channel_id),
                unlink_command="`!unlink`",
            )
        except ConversationOwnershipConflict:
            # Reachable past the resumed-session check above because that check
            # fails CLOSED on duplicate inbound bindings: with two of them at this
            # conversation `resumed_session` denies routing and returns None,
            # while the claim is still refused. Same instruction either way, and
            # reporting it beats surfacing a traceback as a generic command
            # failure.
            logger.info("discord link refused: conversation already held")
            await self.client.send_message(
                channel_id,
                "⚠️ Another session is already linked here. Send `!unlink` first.",
            )
            return
        await self.client.send_message(channel_id, reply)

    async def _handle_unlink(self, user_id: str, channel_id: str, thread_id: str = "") -> None:
        assert self.client is not None
        # A resumed session takes precedence: it is what the user is actually
        # talking to, so releasing it is the only way back to their own
        # conversation from Discord.
        try:
            left_resumed = await self._session_resume.leave_resumed_session(channel_id)
        except ResumeReleaseError:
            await self.client.send_message(channel_id, _RELEASE_FAILURE)
            return
        if left_resumed is not None:
            await self.client.send_message(
                channel_id,
                "✅ Left the resumed session. Back to your Discord conversation.",
            )
            return
        key = self._session_key(user_id, thread_id)
        # Persist the refusal BEFORE releasing: mirroring is re-asserted on every
        # inbound turn, so a release alone would be undone by the user's next
        # message. Batched with the release so the pair is one whole-map write.
        with self.sessions.batched_save():
            self.sessions.set_mirror_opt_out(key, True)
            reply, swept = release_conversation_location(
                self.sessions,
                key=key,
                location=self._origin_mirror_link(channel_id),
                channel="discord",
            )
        if swept:
            # A swept binding can belong to a dashboard slot whose link chip is
            # projected at push time — nudge the dashboard like every other
            # binding mutation does.
            self._session_resume._push_slots()
        await self.client.send_message(channel_id, reply)

    def _live_dashboard_slot(self, session_key: str) -> Any | None:
        """The OPEN dashboard slot for *session_key*, or ``None``."""
        return live_dashboard_slot(
            getattr(self._session_resume, "dashboard_state", None), session_key
        )

    async def _uploads_restricted(self, session_key: str) -> bool:
        """True when this session must not ship local file bytes to Discord.

        The ladder and its fail-closed reasoning live in
        :func:`kiro_crew.messaging.upload_gate.uploads_restricted`, shared with the
        Telegram dispatcher; this supplies Discord's dashboard state and audit label.
        An approved guild thread is readable by every member who can view it, which
        is why the restricted ceiling matters at least as much here as elsewhere.

        The persisted-transcript probe is passed IN because ``messaging`` may not
        import ``dashboard``; this package may, so the import lives here. Kept
        function-local for the same reason it always was: the dashboard gateway
        imports the channel transports, so a module-level import would cycle.
        """
        from kiro_crew.dashboard.handlers._shared import _probe_persisted_session

        return await uploads_restricted(
            getattr(self._session_resume, "dashboard_state", None),
            session_key,
            channel_type="discord",
            persisted_probe=_probe_persisted_session,
        )

    def _mirror_turn_to_live_slot(
        self, session_key: str, user_text: str, reply_text: str
    ) -> tuple[str, str] | None:
        """Land a resumed turn in the live dashboard window. Loop-side only.

        A disk-only append is not enough. The dashboard save writes
        ``meta + frozen prefix + its own window + foreign tail``, so a line
        appended to disk BEFORE a later dashboard turn is re-serialized AFTER
        it, and the transcript reads back out of chronological order. Appending
        to the live window puts the turn in the region the save re-serializes,
        so ordering is preserved. Mirrors ``dashboard/cron_inject.py``, which
        appends to the slot and persists idempotently for the same reason.

        Returns the ``(user_mid, assistant_mid)`` the slot minted when the
        in-memory window took the turn, else ``None``. The caller hands those to
        the durable write so both copies of one row share ONE identity -- the
        dual-writer shape ``cron_inject`` uses (``row_mid(slot.append(...))`` ->
        ``append_if_absent(..., mid=...)``). Minting a SECOND id there instead
        would defeat the idempotency check the mirrored write exists for:
        ``append_if_absent`` only skips a body-equal row carrying the SAME mid,
        so an unrelated id can never match the copy the slot save already landed
        and the turn would be persisted twice. An empty string stands for a row
        the slot did not mint an id for (a reply that was not written).
        """
        slot = self._live_dashboard_slot(session_key)
        if slot is None:
            return None
        try:
            user_mid = row_mid(slot.append("user", user_text, "msg msg-u")) or ""
            assistant_mid = ""
            if reply_text:
                assistant_mid = row_mid(slot.append("assistant", reply_text, "msg msg-a")) or ""
        except Exception:
            logger.debug(
                "discord: could not mirror turn into live slot %s", session_key, exc_info=True
            )
            return None
        state = getattr(self._session_resume, "dashboard_state", None)
        push = getattr(state, "push_slots_update", None)
        if callable(push):
            try:
                push()
            except Exception:
                logger.debug("discord: slots push after resumed turn failed", exc_info=True)
        return (user_mid, assistant_mid)

    def _persist_turn(
        self,
        session_key: str,
        user_text: str,
        reply_text: str,
        is_new: bool,
        agent: str | None = None,
        mirror_mids: tuple[str, str] | None = None,
    ) -> None:
        """Record the turn to conversation_log (dashboard visibility + restart).

        *mirror_mids* is what ``_mirror_turn_to_live_slot`` returned: the ids the
        live dashboard window minted for this turn's rows, or ``None`` when there
        was no live slot to mirror into. It carries BOTH facts, so there is no
        separate ``mirrored`` flag -- the presence of the tuple IS the flag, and a
        second parameter encoding the same thing could only ever disagree with it.

        With a live slot the disk write must be idempotent: that slot's own save
        re-serializes its window, so a plain append would persist the same message
        twice. The write goes under the SAME ids the window rows carry -- the
        dual-writer shape ``cron_inject`` uses -- because ``append_if_absent``
        skips only a body-equal row carrying the same mid. A fresh id there would
        never match the slot save's copy and the turn would land on disk twice.

        With no live slot nothing has the row yet, so it is a plain append under a
        newly minted id.
        """
        if self.conv_log is None:
            return
        if mirror_mids is not None:
            user_mid, assistant_mid = mirror_mids
            self.conv_log.append_if_absent(
                session_key, "user", user_text, agent=agent, mid=user_mid
            )
            if reply_text:
                self.conv_log.append_if_absent(
                    session_key, "assistant", reply_text, agent=agent, mid=assistant_mid
                )
        else:
            self.conv_log.append(session_key, "user", user_text, agent=agent, mid=mint_row_mid())
            if reply_text:
                self.conv_log.append(
                    session_key, "assistant", reply_text, agent=agent, mid=mint_row_mid()
                )
        if is_new:
            title = (user_text or "").strip().replace("\n", " ")[:40] or "Discord"
            self.conv_log.set_title(session_key, title)

    async def _surface_own_session(self) -> None:
        """Surface a newly created Discord session in the dashboard immediately."""
        from kiro_crew.dashboard.channel_slots import surface_dispatcher_session

        # Keep compatibility with the session-resume controller's older state
        # attachment while all gateways move through register_channel_transport.
        if not hasattr(self, "dashboard_state"):
            self.dashboard_state = getattr(self._session_resume, "dashboard_state", None)
        await surface_dispatcher_session(self)

    async def _maybe_notice(
        self, channel_id: str, scope_id: str, session_key: str, provider: Any
    ) -> None:
        """Soft-threshold context warning as a SEPARATE message (not persisted).

        The hard-compaction backstop is the backend autocompactor
        (``session.autocompact_pct``).
        """
        pct = self.sessions.check_context_usage(session_key, provider)
        soft_pct = self.cfg.discord.soft_threshold_pct
        if pct >= soft_pct and not self._conv.is_awaiting(scope_id):
            self._conv.set_awaiting(scope_id)
            assert self.client is not None
            await self.client.send_message(
                channel_id,
                "⚠️ Context is getting long. Use `!compact` to compress or "
                "`!new` to start fresh.",
            )

    async def _handle_compact(
        self,
        user_id: str,
        channel_id: str,
        thread_id: str,
        resumed_key: str | None,
    ) -> None:
        """In-place ACP ``/compact`` on the conversation's session."""
        assert self.client is not None
        session_key = resumed_key or self._session_key(user_id, thread_id)
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self.client.send_message(
                    channel_id,
                    "⏳ Still working on your last message — try `!compact` " "once it finishes.",
                )
            else:
                await self.client.send_message(channel_id, "No active session to compact.")
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self.client.send_message(channel_id, "No active session to compact.")
                return

            status_id = await self.client.send_message(channel_id, "🔄 Compacting context…")
            result_text: str | None = None

            def _safe(text: str) -> str:
                """Redact backend-echoed, LLM-influenced compaction text before
                it reaches the external Discord surface: normal turns get this
                via the shared TurnDriver, but this path sends directly."""
                cleaned, _ = redact_credentials(text or "")
                cleaned, _ = redact_exfiltration_urls(cleaned)
                return cleaned

            try:

                # Compaction runs over the prompt transport:
                # provider.compact() drives /compact via session/prompt (the
                # commands/execute path does NOT run compaction — it returns
                # with no status). Bound compact()'s prompt
                # turn here, then let wait_for_compaction() own its OWN deadline
                # for a status emitted async after end_turn — it must NOT be
                # nested inside another timeout, or the graceful "timed out"
                # branch is unreachable and a slow-but-healthy session gets
                # destroyed by the outer TimeoutError.
                await asyncio.wait_for(provider.compact(), timeout=120)
                cr = await provider.wait_for_compaction()
                if cr["type"] == "completed":
                    # ``summary`` is model-facing compacted context, not a
                    # user-facing receipt. Never publish its orchestration text.
                    result_text = "✅ Context compacted."
                elif cr["type"] == "failed":
                    err = _safe(cr.get("summary", ""))
                    result_text = f"❌ Compaction failed: {err}" if err else "❌ Compaction failed."
                else:
                    result_text = "⚠️ Compaction timed out."
            except Exception:
                logger.warning("Discord !compact failed for %s", session_key, exc_info=True)
                result_text = "❌ Compaction failed unexpectedly."
                # Drop the wedged native conversation, NOT the session's channel
                # identity: the map entry carries the mirror binding, so a full
                # ``destroy`` would silently unlink a mirrored conversation.
                # Housekeeping never unlinks (see ``SessionMap.prune`` and
                # ``SessionManager._recycle_held``).
                try:
                    await self.sessions.discard_conversation(session_key)
                except Exception:
                    logger.debug(
                        "Discord: discard after compact failure failed",
                        exc_info=True,
                    )

            final = result_text or "✅ Context compacted."
            if status_id:
                await self.client.edit_message(channel_id, status_id, final)
            else:
                await self.client.send_message(channel_id, final)
        finally:
            self.sessions.release(session_key)

    # ── /status, /model ────────────────────────────────────────────────────
    #
    # Each handler takes a ``reply`` sink instead of a channel id, because the
    # same body serves two delivery shapes: a ``!`` text command answers with a
    # normal channel message, while a registered slash command must answer its
    # own interaction (ephemerally, inside Discord's ~3s callback deadline).
    # Sharing the body is the point: a second copy per surface is how the two
    # drift, and the slash form is the one an operator will actually discover.
    # Each handler replies EXACTLY ONCE — an interaction callback may only be
    # used for the first response, and a second would need a followup route.

    async def _handle_status(self, reply: "ReplyFn") -> None:
        """Report runtime stats, from the same source Slack's ``/kirocrew status`` uses.

        ``Stats()`` is the process-wide counter set, so the two channels cannot
        report different numbers for the same gateway. The auto-approve line is
        appended because it is the one piece of runtime state that changes what a
        tool call will DO, and a user deciding whether to send a request needs it.

        READ-ONLY, and the only mention of the grant this channel makes: Discord
        can report auto-approve but cannot take, renew, or drop it. Granting is
        the operator's, from the dashboard or the machine running the gateway.

        Nothing here names a path, a token, or a config value: a slash command is
        invocable from an allow-listed guild thread that every member can read.
        """
        so = safety_override()
        yolo = f"ON ({describe_grant_lifetime()})" if so.is_active() else "OFF"
        await reply(
            f"📊 {Stats().summary()}\n"
            f"agent `{self._resolve_agent()}` · approval `{self.approval_mode}` · "
            f"YOLO {yolo}"
        )

    def _model_choices(self, session_key: str) -> tuple[tuple[str, str], ...]:
        """``(model_id, label)`` rows to offer for this session.

        The ONLY source is what this session's backend advertised at
        ``session/new`` — the set THIS account may actually use, carrying the
        backend's own ids. That is deliberate on both counts: a static catalogue
        would offer models the account cannot reach (a refusal mid-conversation),
        and its display keys would need per-backend translation before the wire,
        whereas an advertised id is what ``set_model`` accepts verbatim.

        Returns just the Auto row when nothing is advertised (no live session
        yet), which the caller reads as "there is nothing to pick".
        """
        rows: list[tuple[str, str]] = [("", "Auto (let the backend choose)")]
        provider = self.sessions.get_provider(session_key)
        advertised = getattr(provider, "available_models", None)
        if not callable(advertised):
            return tuple(rows)
        try:
            entries = [m for m in advertised() if isinstance(m, dict)]
        except Exception:
            logger.warning("discord !model: available_models failed", exc_info=True)
            return tuple(rows)
        for entry in entries:
            model_id = str(entry.get("modelId") or "").strip()
            # "auto" is already the first row; listing it twice would give the
            # same choice two buttons.
            if not model_id or model_id == "auto":
                continue
            rows.append((model_id, str(entry.get("name") or model_id)))
        return tuple(rows[:_MODEL_PICKER_LIMIT])

    def _prune_model_pickers(self, now: float) -> None:
        """Drop expired pickers, then the oldest ones past the retention cap."""
        for token, picker in list(self._model_pickers.items()):
            if now - picker.created_at > _MODEL_PICKER_TTL_SECS:
                self._model_pickers.pop(token, None)
        while len(self._model_pickers) > _MODEL_PICKER_MAX:
            oldest = min(self._model_pickers, key=lambda t: self._model_pickers[t].created_at)
            self._model_pickers.pop(oldest, None)

    async def _handle_model(
        self, channel_id: str, scope_id: str, session_key: str, arg: str
    ) -> None:
        """Post the model buttons (or say there is nothing to pick yet).

        Deliberately button-only: a free-text model id means guessing at names
        the user has no way to enumerate, and a typo lands as a rejected
        ``set_model`` mid-conversation. Any argument is treated as "show me the
        list" rather than parsed.

        Unlike the other command handlers this one does not take a ``reply``
        sink: the buttons must live on a real channel message whose id the picker
        registry keys on, and an ephemeral interaction response is not editable
        by ``edit_message``. A slash invocation therefore acknowledges the
        interaction separately and the picker itself is posted to the channel.
        """
        assert self.client is not None
        choices = self._model_choices(session_key)
        if len(choices) <= 1:
            await self.client.send_message(
                channel_id,
                "No model list available yet — send a message first, then `!model`.",
            )
            return

        current = self._model_pref.get(scope_id, "")
        current_label = next(
            (label for mid, label in choices if mid == current),
            current or "Auto",
        )
        header = f"Current model: **{current_label}**\nPick one:"
        if arg.strip():
            # An argument is not an id to apply — say so once, then show the list
            # anyway so the message is still a step forward.
            header = f"`!model` takes no argument — pick from the list.\n\n{header}"
        message_id = await self.client.send_message(
            channel_id, header, components=build_model_components(choices, current)
        )
        if message_id is None:
            return
        now = time.time()
        self._prune_model_pickers(now)
        self._model_pickers[f"{channel_id}:{message_id}"] = _ModelPicker(
            scope_id=scope_id,
            channel_id=channel_id,
            message_id=message_id,
            created_at=now,
            choices=choices,
        )

    async def _apply_model(self, scope_id: str, session_key: str, model_id: str) -> str:
        """Record *model_id* for this conversation and push it to the live session.

        *model_id* comes verbatim from the session's advertised list, so it is
        already the id this backend accepts — no canonical translation, which
        would differ per backend and could mangle an id that was correct.

        The preference is stored unconditionally so it reaches the NEXT session
        even when there is nothing live to switch (the common case right after
        ``!new``). When a session does exist the switch is attempted in place —
        ``session/set_model`` carries the conversation across — and the semaphore
        is taken atomically so the switch cannot interleave JSON-RPC with a turn
        on the same stdio channel.

        Returns the user-facing outcome line.
        """
        label = model_id or "Auto"
        self._model_pref[scope_id] = model_id
        live = self.sessions.has_session(session_key)
        # Two different promises, because the preference reaches a session only
        # at creation: ``get_or_create`` returns a reused session from its fast
        # path before it consults ``model=``. With nothing live the next message
        # starts the session, so it genuinely lands then; with a session already
        # up, only a fresh conversation picks it up.
        deferred = f"✅ Model set to {label} — it applies to your next message."
        next_new = (
            f"✅ Model set to {label} — this conversation keeps its current "
            f"model; the switch applies to your next one (`!new`)."
        )
        # Auto has no ACP id meaning "let the backend choose", so it can only be
        # recorded; the next session start resolves it from config. Claiming a
        # live switch here would be a lie.
        if not model_id:
            return next_new if live else deferred
        if not live:
            return deferred
        if not await self.sessions.try_acquire(session_key):
            return (
                f"✅ Model set to {label}, but a reply is still running — this "
                f"conversation keeps its current model; the switch applies to "
                f"your next one (`!new`)."
            )
        try:
            provider = self.sessions.get_provider(session_key)
            set_model = getattr(getattr(provider, "client", None), "set_model", None)
            if set_model is None:
                return next_new
            await set_model(model_id)
        except Exception as exc:
            logger.warning(
                "discord !model: live set_model failed for %s: %s",
                session_key,
                type(exc).__name__,
                exc_info=True,
            )
            # The stored preference still stands, so the next session gets it,
            # but do not claim the running conversation switched when it did not.
            return (
                f"⚠️ Couldn't switch this conversation to {label} "
                f"({type(exc).__name__}) — it applies to your next "
                f"conversation (`!new`)."
            )
        finally:
            self.sessions.release(session_key)
        return f"✅ Now using {label}."

    # ── Shared command routing (text ``!x`` and registered slash ``/x``) ────

    def _channel_reply(self, channel_id: str) -> "ReplyFn":
        """A reply sink that posts a normal channel message."""

        async def _send(text: str) -> None:
            assert self.client is not None
            await self.client.send_message(channel_id, text)

        return _send

    def _interaction_reply(self, itx: "DiscordInteraction") -> "ReplyFn":
        """A reply sink that answers the interaction itself, ephemerally.

        Ephemeral because a slash command is invocable from an allow-listed guild
        thread that every member can read, and these replies carry runtime state
        or a login link. Only the FIRST response may use the callback route, which
        is why every handler behind this replies exactly once.
        """

        async def _respond(text: str) -> None:
            assert self.client is not None
            await self.client.respond_interaction(
                itx.interaction_id, itx.interaction_token, text, ephemeral=True
            )

        return _respond

    async def _run_reply_command(
        self,
        cmd: str,
        reply: "ReplyFn",
        *,
        user_id: str,
        thread_id: str,
        text: str,
    ) -> None:
        """Dispatch one single-reply command through the given sink.

        The two surfaces share this so a command cannot exist on one and not the
        other: the text path and the slash path differ only in the sink they bind.
        """
        if cmd == "status":
            await self._handle_status(reply)

    async def _on_command_interaction(self, itx: "DiscordInteraction") -> None:
        """Run a registered slash command.

        Reconstructs the ``!``-form text from the command name and its options so
        the SAME parsers and handlers serve both surfaces; the alternative is a
        second argument grammar per command, which is how the two drift.

        Commands whose reply is not a single message are handled separately:
        ``model`` posts a real channel message because its buttons must be
        editable (an ephemeral response is not), and the session-scoped commands
        route through ``handle_message`` so they keep the resume-binding refusal
        and mid-turn checks that path owns.
        """
        assert self.client is not None
        name = itx.command_name
        thread_id = itx.channel_id if itx.guild_id else ""
        if name in _REPLY_COMMANDS:
            await self._run_reply_command(
                name,
                self._interaction_reply(itx),
                user_id=itx.user_id,
                thread_id=thread_id,
                # Rebuild the text form so the shared argument parsers apply
                # unchanged. Option order does not matter: every command here
                # takes at most one.
                text=" ".join([f"!{name}", *itx.options.values()]).strip(),
            )
            return
        if name == "help":
            await self.client.respond_interaction(
                itx.interaction_id, itx.interaction_token, build_help_text(), ephemeral=True
            )
            return
        if name == "model" and thread_id:
            # `model` is the one command whose output CANNOT be ephemeral: its
            # buttons have to live on an editable channel message, and an
            # ephemeral response is not editable. In a guild thread that would
            # publish the account's advertised model list to every member, after
            # the slash surface promised a private reply. Refusing is the honest
            # resolution: `!model` in the thread still works for anyone who
            # accepts that it posts, and a DM has no such tension.
            await self.client.respond_interaction(
                itx.interaction_id,
                itx.interaction_token,
                "🔒 `/model` needs a message it can edit, so its reply cannot be "
                "private here. DM me `/model`, or send `!model` if you are happy "
                "for the list to be visible in this thread.",
                ephemeral=True,
            )
            return
        # Everything else is session-scoped. Acknowledge the interaction first so
        # Discord does not show "interaction failed" while the turn or command
        # runs, then replay it through the text path, which owns the resume
        # refusal, the governance recheck and the mid-turn ladder.
        await self.client.respond_interaction(
            itx.interaction_id,
            itx.interaction_token,
            f"Running `/{name}`…",
            ephemeral=True,
        )
        argument = " ".join(itx.options.values()).strip()
        synthetic = InboundMessage(
            channel_type="discord",
            user_id=itx.user_id,
            conversation_id=itx.channel_id,
            text=f"!{name} {argument}".strip(),
            thread_id=thread_id or None,
        )
        await self.handle_message(synthetic)
