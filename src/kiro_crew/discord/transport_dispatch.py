"""Full new-path dispatch: DiscordTransport -> ChannelTurns -> DiscordRenderer.

``DiscordTransport.receive()`` authorizes + normalizes an inbound message and
hands the ``InboundMessage`` to :meth:`DiscordDispatcher.handle_message`:

    governance gate, resume routing, command intercept (!new, !compact, !help, …)
    -> mid-turn busy ladder, idle/daily rotation, option-press provenance
    -> ChannelTurns.answer (messaging.dispatch)   # the shared turn pipeline
       with Discord's adapters: the DiscordRenderer factory, the upload root +
       context chip + attachment intake (``prepare``), the dashboard ledger
       (``record``), the soft-threshold notice and the dashboard surfacing, and
       Discord's remaining divergences declared as ``DISCORD_DRIFT``
    -> queue drain

``on_interaction`` resolves interactive tool approvals (``a:<rid>:<1|0>`` ->
``DiscordApprovalDecider.resolve_global``) and re-injects ``[OPTIONS:]``
choices (``opt:<i>``) as fresh turns.

Dependency direction is ``discord -> messaging`` (allowed), so this module never
imports ``kiro_crew.slack``.

``DiscordDispatcher`` is composed from the owners beside this module: the queued-message
origin and this channel's route spellings (``transport_dispatch_origin``), the
``!model`` picker (``transport_dispatch_model_picker``), the ``!link``, ``!unlink``,
``!compact`` and ``!status`` handlers (``transport_dispatch_commands``), button presses
and slash commands (``transport_dispatch_interactions``), the mid-turn steer-or-queue arm
(``transport_dispatch_midturn``), spawn-approval delivery
(``transport_dispatch_spawn_approval``) and the dashboard transcript adapters
(``transport_dispatch_ledger``). Each owner's methods are bound below as class
attributes of the same name. This module keeps the dispatcher's state, the inbound front
door (``handle_message``), the queue drain and receipt wrappers, ``!stop`` and the
conversation-identity helpers; repository guards read the drain, receipt, stop, pipeline
and admission constructs in this file. It stays the only import path and patch surface,
and :mod:`kiro_crew._facade_mirror` sends a patch of any name it binds to every owner
holding the same object. ``# noqa: F401``
marks an import this module's name surface keeps. Which owner takes new work is
recorded in ``docs/system-specs/modules/messaging.md`` (Discord's dispatcher composition).
"""

from __future__ import annotations

import asyncio
import importlib as _importlib
import logging
import sys as _sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass  # noqa: F401
from types import SimpleNamespace  # noqa: F401
from typing import TYPE_CHECKING, Any, NamedTuple, cast  # noqa: F401

from kiro_crew import _facade_mirror
from kiro_crew.agent_switch_command import ChannelAgentPicks, handle_channel_agent_command
from kiro_crew.config import live
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.config.sections import _clamp_pct
from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT  # noqa: F401
from kiro_crew.discord import transport_dispatch_commands as _commands
from kiro_crew.discord import transport_dispatch_interactions as _interactions
from kiro_crew.discord import transport_dispatch_ledger as _ledger
from kiro_crew.discord import transport_dispatch_midturn as _midturn
from kiro_crew.discord import transport_dispatch_model_picker as _model_picker
from kiro_crew.discord import transport_dispatch_spawn_approval as _spawn_approval
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
from kiro_crew.discord.renderer import (  # noqa: F401
    _STYLE_DANGER,
    _STYLE_SUCCESS,
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
from kiro_crew.discord.transport import DISCORD_CAPABILITIES, _coerce_snowflakes
from kiro_crew.discord.transport_dispatch_commands import _RELEASE_FAILURE, _REPLY_COMMANDS
from kiro_crew.discord.transport_dispatch_model_picker import _ModelPicker
from kiro_crew.discord.transport_dispatch_origin import (
    _CHANNEL,
    _CHAT_TYPE_THREAD,
    _entry_owner,
    _origin_kwargs,
    _queued_origin,
    _QueuedOrigin,
)
from kiro_crew.history import mint_row_mid  # noqa: F401
from kiro_crew.messaging.attachments import IngestLimits
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.commands import (  # noqa: F401
    compact_unsupported_backend,
    compact_unsupported_reply,
    context_recycle_warning,
    recycle_backend,
    recycle_warning_should_send,
    stop_running_turn,
)
from kiro_crew.messaging.conversation import reserve_new_generation
from kiro_crew.messaging.dispatch import (  # noqa: F401
    DISCORD_DRIFT,
    Approvals,
    Asker,
    ChannelTurns,
    MonitorWake,
    TurnOutcome,
    TurnRecord,
    Verdict,
    admit_inbound_callback,
)
from kiro_crew.messaging.display_safety import redact_for_display  # noqa: F401
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE
from kiro_crew.messaging.identity import (  # noqa: F401
    channel_inbound_permitted,
    channel_outbound_permitted,
)
from kiro_crew.messaging.inbound_spool import InboundRoute
from kiro_crew.messaging.link import (  # noqa: F401
    CHAT_TYPE_DIRECT,
    ChannelLink,
    build_dm_session_key,
    parse_session_key,
    rebind_conversation_location,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.messaging.queue_drain import (  # noqa: F401
    drain_until_quiet,
    entry_channel,
    entry_person_origin,
    owner_token,
    person_tag,
    register_drain,
    tag_entry,
)
from kiro_crew.messaging.session_resume import refused_resume_is_restricted
from kiro_crew.messaging.spawn_approval_delivery import unpressed_wait_answer  # noqa: F401
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.messaging.upload_gate import session_is_restricted, uploads_restricted  # noqa: F401
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import MonitorDispatchResult
from kiro_crew.safety_override import describe_grant_lifetime, safety_override  # noqa: F401
from kiro_crew.security import (  # noqa: F401
    redact,
    redact_credentials,
    redact_exfiltration_urls,
    redact_local_paths,
)
from kiro_crew.sel import sel  # noqa: F401
from kiro_crew.session_map import ConversationOwnershipConflict  # noqa: F401
from kiro_crew.stats import Stats  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from kiro_crew.context import ContextBuilder
    from kiro_crew.discord.client import DiscordClient
    from kiro_crew.discord.transport import DiscordTransport
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
from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI  # noqa: F401
from kiro_crew.messaging.queue_receipt import (
    ReceiptQueue,
    ReceiptSurface,
    receipt_address_key,
)
from kiro_crew.start_priority import person_priority

logger = logging.getLogger(__name__)


def _drains_after(outcome: TurnOutcome, *, wake: bool) -> bool:
    """Whether the queue is drained after a turn that ended in *outcome*.

    Not after a message that turned out to carry nothing to send. On a monitor
    wake, only once the wake's claim held a turn: one that finished (delivered or
    not) or one that failed after the claim. A wake the pipeline refused, found
    stale, or ended before its claim leaves the queue to the turn that owns it.
    """
    if outcome.verdict is Verdict.EMPTY:
        return False
    if not wake:
        return True
    if outcome.verdict in (Verdict.ANSWERED, Verdict.UNDELIVERED):
        return True
    return (
        outcome.verdict is Verdict.FAILED
        and outcome.monitor is not MonitorDispatchResult.UNAVAILABLE
    )


# Canonical kiro-cli agent fallback so Discord sessions load kirocrew-core
# (spawn_run etc.) — mirrors the Slack/Telegram paths.
_DEFAULT_KIROCREW_AGENT = "kirocrew"

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


class DiscordDispatcher:
    """Coordinates Discord turns onto the shared channel turn pipeline.

    One instance per gateway lifetime. Holds the per-user conversation state
    (generation counter + soft-threshold flag). ``handle_message`` is wired as
    the transport's dispatch callback; ``on_interaction`` is wired as the
    client's button handler. ``client`` is set by the gateway after
    construction.
    """

    # ── Owner bindings ────────────────────────────────────────────────────
    # Each name below is the owner module's own function, bound as the member it
    # is: the state stays on the instance, and a class or instance patch of a
    # member reaches every caller.

    # transport_dispatch_model_picker.py
    _model_choices = _model_picker._model_choices
    _prune_model_pickers = _model_picker._prune_model_pickers
    _handle_model = _model_picker._handle_model
    _apply_model = _model_picker._apply_model

    # transport_dispatch_commands.py
    _handle_link = _commands._handle_link
    _handle_unlink = _commands._handle_unlink
    _handle_compact = _commands._handle_compact
    _handle_status = _commands._handle_status
    _channel_reply = _commands._channel_reply
    _run_reply_command = _commands._run_reply_command

    # transport_dispatch_interactions.py
    on_interaction = _interactions.on_interaction
    _interaction_actionable = _interactions._interaction_actionable
    _interaction_reply = _interactions._interaction_reply
    _on_command_interaction = _interactions._on_command_interaction

    # transport_dispatch_midturn.py
    _handle_resumed_busy = _midturn._handle_resumed_busy
    _handle_busy = _midturn._handle_busy

    # transport_dispatch_spawn_approval.py
    deliver_spawn_approval = _spawn_approval.deliver_spawn_approval
    _spawn_prompt_channel_permitted = _spawn_approval._spawn_prompt_channel_permitted
    _spawn_prompt_destination_permitted = _spawn_approval._spawn_prompt_destination_permitted
    _spawn_approval_actionable = _spawn_approval._spawn_approval_actionable
    _spawn_chat_target = _spawn_approval._spawn_chat_target

    # transport_dispatch_ledger.py
    _record_turn = _ledger._record_turn
    _session_restricted = _ledger._session_restricted
    _uploads_restricted = _ledger._uploads_restricted
    _persist_turn = _ledger._persist_turn

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
        # The subset of ``_allowed_threads`` that came from config.json, so a
        # reload can tell a thread an operator removed from a thread this process
        # promoted at runtime (see ``register_allowed_thread``).
        self._configured_threads: frozenset[str] = frozenset(self._allowed_threads)
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        self.client: "DiscordClient | None" = None
        # Set by maybe_start_discord after construction (same construction-cycle
        # reason as ``client``); the config applier pushes reloaded authorization
        # fields at it.
        self.transport: "DiscordTransport | None" = None
        self._conv = ConversationState(seed_fn=self._seed_gen)
        # Per-conversation agent picked with !agent, keyed by scope id (a DM or
        # a thread) like _conv, because the agent is part of the session key.
        self._agent_picks: ChannelAgentPicks[str] = ChannelAgentPicks()
        # Held on self: the watcher holds the owner WEAKLY, so a subscription
        # dropped here would be collected and the applier would silently stop
        # firing.
        self._config_sub = live.watch_section(
            self, "discord", "messaging", name="DiscordDispatcher"
        )
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
        # Published so a peer channel sharing this queue can wake this drain. Under
        # ``dm_scope = "unified"`` a Discord DM and a Telegram DM to the same agent
        # resolve to ONE session key and therefore one queue, and a drain can only
        # answer the entries its own channel recorded -- so the channel that sets a
        # foreign entry aside has to hand it back to its owner. See
        # ``messaging/queue_drain.py``.
        register_drain(_CHANNEL, self._drain_queue)

    def _channel_turns(self) -> ChannelTurns:
        """The shared turn pipeline, with Discord's ledger, notice and surfacing as adapters.

        Built per message from the dispatcher's LIVE attributes (``sessions``,
        ``ctx_builder``, ``conv_log``): production never reassigns them, but tests
        do after construction -- wiring two dispatchers onto one shared store, as
        the gateways do through the orchestrator -- and a pipeline built once in
        ``__init__`` would keep the store it was given. Discord's remaining divergences from the pipeline's default are
        declared, not forked: each ``DISCORD_DRIFT`` member is retired by a ruling.
        """
        return ChannelTurns(
            _CHANNEL,
            sessions=self.sessions,
            ctx_builder=self.ctx_builder,
            dispatcher=self,
            agent=self._resolve_agent,
            record=self._record_turn,
            notice=lambda where, key, provider: self._maybe_notice(
                where[0], where[1], key, provider
            ),
            surface=self._surface_own_session,
            restricted=self._session_restricted,
            conv_log=self.conv_log,
            slot_state=lambda: getattr(self._session_resume, "dashboard_state", None),
            approvals=Approvals(discard=DiscordApprovalDecider.discard_session),
            drift=DISCORD_DRIFT,
        )

    def register_allowed_thread(self, thread_id: str) -> None:
        """Authorize interactions in a thread created by the inbound transport."""
        self._allowed_threads.add(thread_id)

    def reconfigure(self, section: Any) -> None:
        """Push reloaded ``discord`` authorization fields at both holders.

        This dispatcher keeps its OWN user roster and thread set: interactions
        bypass ``transport.receive``, so ``_authorized`` and the guild-thread gate
        re-check against them. Both are mutated IN PLACE so any holder of the same
        object follows. Threads are UNIONED with the runtime-promoted ids rather
        than replaced -- a thread this process created is not in ``config.json``
        and dropping it would strand every follow-up press in it -- while a thread
        an operator REMOVES from the config is dropped, so the reload narrows as
        intended. A transport that is not up yet is skipped: it reads the section
        fresh when it connects.
        """
        users = _coerce_snowflakes(getattr(section, "allowed_user_ids", None))
        if users is None:
            logger.warning(
                "discord: allowed_user_ids is not a list in the reloaded config; the dispatcher "
                "keeps its previous allow-list (%d id(s))",
                len(self._allowed),
            )
        else:
            self._allowed.clear()
            self._allowed.update(users)
            # The ``!sessions`` owner is the third copy of the roster and must
            # move with it: an added second identity revokes the surface now.
            self._session_resume.reconfigure(self._allowed)
        threads = _coerce_snowflakes(getattr(section, "allowed_thread_ids", None))
        if threads is None:
            logger.warning(
                "discord: allowed_thread_ids is not a list in the reloaded config; the dispatcher "
                "keeps its previous %d entry(ies)",
                len(self._allowed_threads),
            )
        else:
            promoted = self._allowed_threads - self._configured_threads
            self._allowed_threads.clear()
            self._allowed_threads.update(set(threads) | promoted)
            self._configured_threads = frozenset(threads)
        if self.transport is not None:
            self.transport.reconfigure(section)

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
        """Drive one authorized inbound message through the channel turn pipeline.

        ``interpret_commands`` says whether *text* may execute as a command
        (model-authored text never may). Resume routing is consulted when
        ``interpret_commands`` is true OR the turn carries an ``origin_tag`` —
        the tag implies routing, because validating it requires resolving the
        binding to compare keys. The callers that dispatch with commands off
        and no tag DEPEND on the skip: a queue drain replays messages that were
        accepted for the native session while it was busy (a resumed session's
        busy turn hands the message to the dashboard slot's own queue or steer
        path and never to this queue, so a drained item's affinity is native by
        construction), and an AutoNudge fire targets the native key
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
        native_session_key = self._session_key(user_id, thread_id)

        async def _resolve_refused_route() -> RoutingDecision:
            if not (interpret_commands or bool(origin_tag)):
                return RoutingDecision()
            async with self._routing_turn(channel_id):
                return await self._session_resume.route(channel_id)

        async def _refused_turn_restricted() -> bool:
            return await refused_resume_is_restricted(
                native_session_key,
                resolve=_resolve_refused_route,
                is_restricted=self._session_restricted,
            )

        inbound_route = None
        if monitor_completion is None:
            inbound_route = InboundRoute(
                conversation_id=channel_id,
                text=msg.text,
                user_id=user_id,
                thread_id=thread_id,
                message_id=str(getattr(msg, "message_id", "") or ""),
                attachments_dropped=len(getattr(msg, "attachments", None) or ()),
            )
        if not await admit_inbound_callback(
            self.sessions,
            channel_type="discord",
            route=inbound_route,
            restricted=(True if monitor_completion is not None else _refused_turn_restricted),
        ):
            return MonitorDispatchResult.BUSY if monitor_completion is not None else None

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
            new_session_key = self._session_key(user_id, thread_id)
            saved = await reserve_new_generation(
                self.sessions,
                new_session_key,
                channel_type="Discord",
            )
            message = "✅ New conversation started."
            if left_resumed is not None:
                message = "✅ New conversation started — left the resumed session."
            if not saved:
                message += "\n⚠️ The new conversation could not be saved for restart."
            await self.client.send_message(channel_id, message)
            return monitor_result
        if cmd == "compact":
            self._conv.clear_awaiting(scope_id)
            await self._handle_compact(user_id, channel_id, thread_id, route.resumed_key)
            return monitor_result
        if cmd == "sessions":
            # DM-ONLY. The owner gate answers WHO may resume, not WHERE the
            # result may be shown: in an allow-listed guild thread the picker
            # would post private session TITLES and the bind would replay five
            # transcript messages, making private history readable by every
            # member of that thread. Resume is inherently a private-surface
            # operation, so refuse outside a DM rather than redacting harder.
            if thread_id:
                await self.client.send_message(
                    channel_id,
                    "🔒 `!sessions` works only in a direct message — it lists and "
                    "replays private conversations, so it will not post "
                    "them into a shared thread. DM me instead.",
                )
                return monitor_result
            await self._session_resume.show_picker(
                self.client,
                user_id,
                channel_id,
                query=parse_command_argument(text),
                native_key=self._session_key(user_id, thread_id),
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
        if cmd == "agent":
            if route.resumed_key is not None:
                reply = (
                    "ℹ️ This conversation continues a dashboard session, so its agent "
                    "is changed from the dashboard. Send `!new` to start one here."
                )
            else:
                reply = await handle_channel_agent_command(
                    self._agent_picks,
                    scope_id,
                    parse_command_argument(text),
                    conv=self._conv,
                    configured=self._resolve_agent(),
                    sessions=self.sessions,
                    session_key=lambda: self._session_key(user_id, thread_id),
                    prefix="!",
                )
            await self.client.send_message(channel_id, reply)
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
                # NOT `_handle_busy`: that queues into THIS dispatcher's queue,
                # which ``_drain_queue`` drains only at the tail of a DISCORD-driven
                # turn and replays with resume routing off -- so a message queued
                # there while the dashboard drives would sit until some later
                # Discord turn and then run in the NATIVE session. The dashboard
                # slot has its own steer path and its own queue, drained by the
                # dashboard turn loop; the message goes to those, and the refusal
                # stays for the cases the slot cannot take.
                await self._handle_resumed_busy(session_key, msg, text, override_mode)
                return monitor_result
            await self._handle_busy(session_key, msg, text, override_mode)
            return monitor_result

        if monitor_completion is None:
            self._conv.maybe_rotate(
                scope_id,
                time.time(),
                idle_minutes=int(self._live_cfg().messaging.idle_reset_minutes),
                daily_reset_hour=int(self._live_cfg().messaging.daily_reset_hour),
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
        if monitor_completion is not None and resumed_key is not None:
            # A wake belongs to the conversation's OWN session; a resumed session's
            # surface owns its turns.
            return MonitorDispatchResult.UNAVAILABLE
        decider = (
            DiscordApprovalDecider(session_key=session_key)
            if self.approval_mode == APPROVAL_INTERACTIVE
            else None
        )
        # Bound once for the factory and the intake below: a closure does not keep
        # the narrowing the top of this method asserted.
        client = self.client
        built: list[DiscordRenderer] = []
        attachment_temp_paths: list[str] = []
        attachments_fetched: list[Any] = []

        async def _renderer(muted: bool) -> DiscordRenderer:
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
                client,
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
            if not muted:
                # Published for mid-turn steer chips. Deliberately NOT published when
                # muted: the steer path calls the channel-specific ``note_steer`` and
                # already skips cleanly when there is no entry, so leaving it out both
                # silences the chip in a disconnected conversation and keeps that
                # channel-local API off the pipeline's mute substitute.
                self._active_renderers[session_key] = renderer
            built.append(renderer)
            return renderer

        async def _prepare(provider: Any, prompt: str) -> str:
            renderer = built[0]
            renderer.authorize_upload_root(provider.cwd)
            # The turn footer's context chip reads usage off the session provider,
            # which only exists once the session is acquired. Unbound, the chip
            # cannot render at all and the footer silently ships without the one
            # number that tells a user when to run `!compact`.
            renderer.bind_context_source(provider)
            if not msg.attachments:
                return prompt
            # Acquired before this attachment I/O: a large download yields
            # repeatedly, and leaving the session idle in that window lets a later
            # message run first and persist the conversation in reverse order.
            # Fetched once per message, so a second attempt reuses the files.
            if not attachments_fetched:
                result = await process_discord_attachments(client, msg.attachments)
                attachment_temp_paths.extend(result.temp_paths)
                attachments_fetched.append(result)
            return append_attachment_context(prompt, attachments_fetched[0])

        def _wake_still_current() -> bool:
            # The gateway authorized one exact conversation generation; a ``!new``
            # landing since then replaced it.
            return (
                monitor_session_key is None
                or self._session_key(user_id, thread_id) == monitor_session_key
            )

        try:
            outcome = await self._channel_turns().answer(
                Asker(
                    session_key,
                    chan_id,
                    # Durable inbound spool, for a USER message only: a wake is
                    # generated work whose own loop re-fires after a restart.
                    route=inbound_route,
                    start_priority=person_priority(msg.person_origin),
                    audit_caller=f"discord:{user_id}",
                    reply_to=(channel_id, scope_id),
                    resumed=resumed_key is not None,
                    # Record the conversation's REAL send target, the one ``!link``
                    # and ``!unlink`` also name, so unattended output about the
                    # session can reach the user and a turn later taken from the
                    # dashboard is mirrored back here. A resumed session's own
                    # surface owns its output, and ``!link`` refuses there too, so
                    # the automatic path must not do what the explicit one declines.
                    origin=(
                        None if resumed_key is not None else self._origin_mirror_link(channel_id)
                    ),
                    # ``model`` applies only when this turn COLD-STARTS the session:
                    # the fast path returns a reused session before it consults the
                    # argument, which is exactly what ``!model``'s reply promises.
                    model=self._model_pref.get(scope_id) or None,
                    # A resumed session runs its own persisted agent.
                    agent=None if resumed_key is not None else self._resolve_agent(scope_id),
                ),
                text,
                _renderer,
                decider=decider,
                prepare=_prepare,
                monitor=(
                    MonitorWake(monitor_completion, _wake_still_current)
                    if monitor_completion is not None
                    else None
                ),
            )
        finally:
            # Identity-checked: a later turn on this key may have published its own.
            if built and self._active_renderers.get(session_key) is built[0]:
                self._active_renderers.pop(session_key)
            await asyncio.to_thread(cleanup_attachments, attachment_temp_paths)

        # Drain anything queued during the turn (queue_mode == "queue").
        #
        # Deliberately handed NOTHING about this turn but its session key: the
        # replay envelope comes from each queued entry's own recorded origin, and
        # under ``dm_scope = "unified"`` the person who opened this turn is not
        # necessarily the person who queued during it.
        if drain and _drains_after(outcome, wake=monitor_completion is not None):
            await self._drain_queue(session_key)
        return outcome.monitor

    async def _drain_queue(self, session_key: str) -> None:
        """Collapse every message ONE SENDER queued during the just-finished turn
        into ONE combined turn (order preserved). See the Telegram dispatcher for the
        lock/ordering rationale.

        One combined turn gets ONE envelope, so it may only combine messages that
        SHARE one -- same sender, same channel, same thread. That is
        :attr:`_QueuedOrigin.sender_key`, and it is taken from the FIRST entry this
        iteration collapses, never from the turn that opened the queue: under
        ``dm_scope = "unified"`` one session key, and therefore one queue, is shared
        by every allow-listed person, so a queue holding two of them is reachable on
        the live path. Anything from a different sender or place defers itself and
        everything behind it, so FIFO stays exact and the outer loop drains it next as
        its own turn under its own envelope.

        Which is why this method is given the session key and nothing else: the
        opener's identity is not an input it could accidentally fall back to.

        An entry ANOTHER transport recorded shares this queue under the same scope and
        cannot be answered here at all. It is set aside, and because it has already
        been accepted and receipted, its owner's drain is woken once this pump is done
        -- outside ``self._queue.lock``, since that drain takes its own lock and runs a
        whole turn. See ``messaging/queue_drain.py`` for why the cascade terminates.
        """
        # The sequence -- pump, then wake outside the queue lock but INSIDE this
        # channel's active marker, then pump again for any wake a peer could not
        # deliver back here -- lives in the shared module, because all four drains
        # need exactly it and getting the order wrong has no local symptom.
        await drain_until_quiet(
            channel=_CHANNEL,
            session_key=session_key,
            pump=lambda foreign: self._pump_queue(session_key, foreign),
        )

    async def _pump_queue(self, session_key: str, foreign_channels: set[str]) -> None:
        """The collapse-and-answer loop itself. See :meth:`_drain_queue`.

        Split out so the wake has one exit point to run after: the loop returns from
        several places, and a wake that some of them skipped is the defect it exists
        to close.
        """
        # Iterate rather than recurse: one burst can span multiple
        # attachment-capped turns, and messages arriving during a drained turn
        # join the same FIFO pump instead of waiting for unrelated future input.
        while True:
            texts: list[str] = []
            attachments: list[Any] = []
            remainder: list[tuple[str, str, dict]] = []
            defer_rest = False
            # The origin this iteration answers, taken from the FIRST entry it
            # collapses. None until that entry is read.
            origin: _QueuedOrigin | None = None
            # Whether a person sent any entry this turn collapses.
            person = False
            async with self._queue.lock:
                while True:
                    item = self.sessions.dequeue(session_key)
                    if item is None:
                        break
                    item_attachments = list(item[2].get("attachments") or [])
                    item_origin = _queued_origin(item[2])
                    if item_origin is None:
                        # ANOTHER transport recorded this entry, so it is not this
                        # dispatcher's to answer -- it holds no address this channel
                        # can reach. Set aside for its own channel's drain WITHOUT
                        # ``defer_rest``: order matters within one sender's messages,
                        # which ``sender_key`` already keeps exact, while blocking
                        # this channel's own queue behind a foreign entry would
                        # strand it whenever that transport sends nothing further.
                        # Remember WHOSE it is: the entry was already accepted and
                        # receipted, so its owner is woken once this pump is done.
                        remainder.append(item)
                        foreign_channels.add(entry_channel(item[2]))
                        continue
                    if origin is None:
                        origin = item_origin
                    exceeds_attachment_cap = bool(
                        texts
                        and item_attachments
                        and len(attachments) + len(item_attachments) > _MAX_COLLAPSED_ATTACHMENTS
                    )
                    fits = (
                        not defer_rest
                        and len(texts) < _MAX_COLLAPSE
                        and not exceeds_attachment_cap
                        # sender_key, not the whole origin, for the reason
                        # ``_NOT_A_SENDER`` documents: a per-message field in the
                        # comparison would stop the collapse altogether.
                        and item_origin.sender_key == origin.sender_key
                    )
                    if fits:
                        texts.append(item[1])
                        attachments.extend(item_attachments)
                        person = person or entry_person_origin(item[2])
                    else:
                        # Once one message does not fit, defer it and everything
                        # behind it so queue order remains exact.
                        defer_rest = True
                        remainder.append(item)
                # How many of the set-aside entries belong to the sender this turn
                # answers. NOT ``len(remainder)``: that also counts entries from a
                # DIFFERENT sender and entries another TRANSPORT recorded, each of
                # which drains in its own turn in its own channel. Showing those to
                # this sender would promise them a follow-up for messages they never
                # sent -- and when their own burst fit in one turn, a "+N deferred"
                # where their true count is zero.
                own_deferred = 0
                for _ts, rtext, rkw in remainder:
                    r_origin = _queued_origin(rkw)
                    if (
                        origin is not None
                        and r_origin is not None
                        and r_origin.sender_key == origin.sender_key
                    ):
                        own_deferred += 1
                    self.sessions.enqueue(
                        session_key,
                        str(time.time()),
                        rtext,
                        force=True,
                        # Re-enqueued VERBATIM: its attachments, and its origin -- an
                        # entry deferred because it came from SOMEONE ELSE would
                        # otherwise inherit the next first entry's identity, the bug
                        # one iteration later. Passing the payload through rather than
                        # rebuilding it is also what lets an entry another transport
                        # recorded survive this drain intact.
                        **rkw,
                    )
                if texts and origin is not None:
                    await self._receipt_flip_locked(
                        session_key,
                        origin.channel_id,
                        [text or ATTACHMENT_PLACEHOLDER for text in texts],
                        own_deferred,
                        owner=_entry_owner(origin),
                    )
            if not texts or origin is None:
                return
            if remainder:
                logger.debug(
                    "discord: drain set aside %d message(s) for %s, %d of them this "
                    "sender's own (collapse/attachment caps); the rest belong to "
                    "another sender or another transport. All keep FIFO order, this "
                    "sender's draining in the next iteration of this pump",
                    len(remainder),
                    session_key,
                    own_deferred,
                )
            combined = "\n\n".join(texts)
            await self.handle_message(
                InboundMessage(
                    channel_type="discord",
                    # Every addressing and attribution field comes from the queued
                    # entry's own origin, so the turn runs in the sender's channel
                    # under the sender's identity even when someone else opened the
                    # queue.
                    user_id=origin.user_id,
                    conversation_id=origin.channel_id,
                    text=combined,
                    thread_id=origin.thread_id or None,
                    attachments=attachments,
                    # The queued entries' own flag: a gateway-built wake can have been
                    # queued too (kiro_crew.start_priority).
                    person_origin=person,
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
        origin: _QueuedOrigin,
        person_origin: bool = False,
    ) -> bool:
        """Atomically enqueue a mid-turn message and create/grow its collapsing
        receipt, under ``self._queue.lock``. Returns True if queued; False if the
        turn finished in the window (caller runs the message as a fresh turn).

        *person_origin* is the message's own ``InboundMessage.person_origin``, which
        the drained replay's start priority is read from.

        *origin* is REQUIRED and keyword-only: it is who sent THIS message and where
        its reply goes, and the drain replays the entry under it. A default would be
        a way to enqueue an unattributed message, which under
        ``dm_scope = "unified"`` the drain could only answer under someone else's
        identity."""
        assert self.client is not None
        async with self._queue.lock:
            if not self.sessions.enqueue(
                session_key,
                str(time.time()),
                text,
                force=False,
                attachments=list(attachments or []),
                **_origin_kwargs(origin),
                **person_tag(person_origin),
            ):
                return False
            # An attachment-only message has no text; show a placeholder rather
            # than a blank entry in the receipt.
            await self._queue.create_or_grow_locked(
                session_key,
                self._receipt_surface(channel_id),
                text or ATTACHMENT_PLACEHOLDER,
                _entry_owner(origin),
            )
            return True

    async def _receipt_flip_locked(
        self,
        session_key: str,
        channel_id: str,
        answered: list[str],
        deferred: int = 0,
        *,
        owner: str,
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record. Caller MUST
        hold ``self._queue.lock``.

        ``owner`` is WHOSE messages this turn answers, and the flip needs it because one
        bubble can list several principals': a thread shares a channel address between
        everyone posting in it, so a drain that answered one of them must leave the
        others' lines, and the entry that is their only handle, alone.

        REQUIRED and keyword-only, the same way the registry transition it forwards to
        spells it. This wrapper has exactly one caller and that caller always can name
        the principal, so an omission here is a mistake rather than a degradation -- and
        being required makes it a type error at the call site instead of a silent return
        to retiring the whole bubble.
        """
        assert self.client is not None
        await self._queue.flip_answering_locked(
            session_key, self._receipt_surface(channel_id), answered, deferred, owner=owner
        )

    def _receipt_surface(self, channel_id: str) -> ReceiptSurface:
        """A receipt surface with this channel's address already bound."""
        # cast, not assert: mypy does not carry an assert-narrowed local
        # into the nested class body below, so the closure would still see
        # ``DiscordClient | None``. The caller path always has a live client.
        client = cast("DiscordClient", self.client)

        class _Surface:
            label = "discord"
            # The channel is the whole address: ``edit_message`` takes it plus the
            # message id, and a Discord message id is only that channel's.
            address_key = receipt_address_key("discord", channel_id)

            async def send_receipt(self, body: str) -> Any | None:
                return await client.send_message(channel_id, body)

            async def edit_receipt(self, msg_id: Any, body: str) -> bool:
                return await client.edit_message(channel_id, msg_id, body)

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
        """Hard cancel: abort the in-flight turn and clear THIS caller's queued messages.

        The cooperative-cancel contract, the lock ordering across ``clear_queue``
        + the receipt finalize, and both replies live in
        :func:`~kiro_crew.messaging.commands.stop_running_turn`; this supplies
        Discord's address and stops the session the turn is actually running
        under, which for a resumed conversation is its owner rather than this
        channel's own DM session.

        The owner token is built from the same three fields an inbound records on its
        queue entries, so the caller matches their own entries and no one else's: under
        ``dm_scope = "unified"`` this queue also holds other people's messages, and on
        another transport too.
        """
        assert self.client is not None
        await stop_running_turn(
            self.sessions,
            resumed_key or self._session_key(user_id, thread_id),
            queue=self._queue,
            surface=self._receipt_surface(channel_id),
            owner=_entry_owner(
                _QueuedOrigin(
                    user_id=str(user_id), channel_id=str(channel_id), thread_id=str(thread_id or "")
                )
            ),
            deliver=lambda text: self.client.send_message(channel_id, text),  # type: ignore[union-attr]
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

        May block (the fallback ``load()`` stats config.json and validates it),
        so callers run it off the event loop.

        Read live rather than taken from ``self.cfg``, which is the boot-time
        snapshot: an operator who turns the phase reactions off in the dashboard
        expects the next message to be quiet, not the next restart. A failed read
        keeps the shipped defaults rather than failing the turn, because neither
        toggle is a security control: the loud default is the safe one to fall
        back to for reactions, and the quiet default is the safe one for
        reasoning.
        """
        try:
            discord_cfg = self._live_cfg().discord
            return bool(discord_cfg.reactions_enabled), bool(discord_cfg.show_thinking)
        except Exception:
            logger.warning("discord: could not read the render toggles", exc_info=True)
            return True, False

    # ── Live config ────────────────────────────────────────────────────────

    def _live_cfg(self) -> "KiroCrewConfig":
        """The config in force NOW, for a per-turn read.

        The watcher's snapshot when it is armed, else a fingerprint-cached
        ``load()`` (two stats on a hit), else the boot copy. Falling back to
        ``self.cfg`` rather than raising keeps a turn running when the config
        file is momentarily unreadable: a threshold or a render toggle is not an
        authorization decision, and the boot value is the one the operator last
        had in force.
        """
        return live.current(self.cfg, log_prefix="discord")

    def _soft_threshold(self) -> int:
        """The context-nudge threshold from the live config.

        Re-runs the loader's own clamp, because a reloaded value read straight
        off the section can sit outside the valid range and either nudge on every
        turn or never nudge at all. Discord has no hard threshold, so there is no
        pair to order.
        """
        return _clamp_pct(int(getattr(self._live_cfg().discord, "soft_threshold_pct", 80)))

    def _resolve_agent(self, scope_id: str | None = None) -> str:
        """The agent *scope_id* runs: its ``!agent`` pick, else the configured one."""
        configured = self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT
        return self._agent_picks.resolve(scope_id, configured)

    @staticmethod
    def _scope_id(user_id: str, thread_id: str = "") -> str:
        return f"thread:{thread_id}" if thread_id else f"user:{user_id}"

    def _session_key(self, user_id: str, thread_id: str = "") -> str:
        scope_id = self._scope_id(user_id, thread_id)
        gen = self._conv.current_gen(scope_id)
        return build_dm_session_key(
            "discord",
            self._resolve_agent(scope_id),
            thread_id or user_id,
            gen=gen,
            dm_scope=("per-channel-peer" if thread_id else str(self.cfg.messaging.dm_scope)),
            chat_type=(_CHAT_TYPE_THREAD if thread_id else CHAT_TYPE_DIRECT),
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
                self._resolve_agent(scope_id),
                thread_id,
                dm_scope="per-channel-peer",
                chat_type=_CHAT_TYPE_THREAD,
            )
            return self.sessions.max_generation(bucket)
        user_id = scope_id.removeprefix("user:")
        return seed_generation(
            self.sessions,
            channel="discord",
            agent=self._resolve_agent(scope_id),
            user_id=user_id,
            dm_scope=str(self.cfg.messaging.dm_scope),
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
        soft_pct = self._soft_threshold()
        if recycle_backend(provider):
            # Crew restarts this backend's session at the threshold; warn once
            # before it, offering a fresh start instead of a compaction.
            if recycle_warning_should_send(self.sessions, self._conv, scope_id, session_key, pct):
                assert self.client is not None
                await self.client.send_message(channel_id, context_recycle_warning("`!new`"))
            return
        if pct >= soft_pct and compact_unsupported_backend(provider):
            # Capability gate: the nudge advises !compact, which this
            # backend refuses — it compacts on its own as context fills, so
            # there is nothing for the user to act on.
            return
        if pct >= soft_pct and not self._conv.is_awaiting(scope_id):
            self._conv.set_awaiting(scope_id)
            assert self.client is not None
            await self.client.send_message(
                channel_id,
                "⚠️ Context is getting long. Use `!compact` to compress or "
                "`!new` to start fresh.",
            )


# The transport_dispatch_* owner modules hold the DiscordDispatcher members the class
# binds and the module-level names imported back above. ``_facade_mirror`` keeps the
# patch surface whole: a write of a name this module binds reaches every owner binding
# the same object, and a read of an owner name this module does not bind is answered by
# that owner. ``test/test_transport_dispatch_composition.py`` pins the reach.
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.discord.transport_dispatch_origin",
    "kiro_crew.discord.transport_dispatch_model_picker",
    "kiro_crew.discord.transport_dispatch_commands",
    "kiro_crew.discord.transport_dispatch_interactions",
    "kiro_crew.discord.transport_dispatch_midturn",
    "kiro_crew.discord.transport_dispatch_spawn_approval",
    "kiro_crew.discord.transport_dispatch_ledger",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
del _owner
#: The module-level names an owner defines that this module serves without binding them;
#: the ``TYPE_CHECKING`` manifest below imports the same names for type checkers. The
#: owners' other bindings stay out of this module's surface: the members the class binds
#: are reached on the class.
_SERVED = (
    "_inbound_origin",
    "_MODEL_PICKER_LIMIT",
    "_MODEL_PICKER_MAX",
    "_MODEL_PICKER_TTL_SECS",
    "_NOT_A_SENDER",
    "_ORIGIN_PREFIX",
    "_RESUMED_BUSY_REFUSAL",
    "_UNTAGGED_OPTIONS_REFUSAL",
)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(
    _facade_mirror.install(
        _sys.modules[__name__],
        _OWNERS,
        excluded=(),
        surface=frozenset(globals()) | frozenset(_SERVED),
    ).served
)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))

if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.discord.transport_dispatch_interactions import (  # noqa: F401
        _UNTAGGED_OPTIONS_REFUSAL,
    )
    from kiro_crew.discord.transport_dispatch_midturn import (  # noqa: F401
        _RESUMED_BUSY_REFUSAL,
    )
    from kiro_crew.discord.transport_dispatch_model_picker import (  # noqa: F401
        _MODEL_PICKER_LIMIT,
        _MODEL_PICKER_MAX,
        _MODEL_PICKER_TTL_SECS,
    )
    from kiro_crew.discord.transport_dispatch_origin import (  # noqa: F401
        _NOT_A_SENDER,
        _ORIGIN_PREFIX,
        _inbound_origin,
    )
