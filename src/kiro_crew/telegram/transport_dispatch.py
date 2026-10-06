"""Full new-path dispatch: TelegramTransport -> TurnDriver -> TelegramRenderer.

``TelegramTransport.receive()`` authorizes + normalizes an inbound update and
hands the ``InboundMessage`` to :meth:`TelegramDispatcher.handle_message`,
which mirrors the Slack transport dispatch:

    command intercept (/new, /compact, /model, /yolo, /help)
    -> construct TelegramRenderer + on_turn_start (immediate ack placeholder)
    -> session acquire -> context build
    -> TurnDriver.run(provider, renderer)   # shared redaction + approval ladder
    -> post-turn (record_success, persist, soft-threshold notice)  # each guarded
    -> renderer.close() + session release   # in finally

``on_callback`` resolves interactive tool approvals (``a:<rid>:<1|0>`` ->
``TelegramApprovalDecider.resolve_global``), applies ``/model`` picks
(``m:<index>``) and re-injects provenance-tagged ``[OPTIONS:]`` choices
(``opt:<index>:<session-tag>``) as literal fresh turns.

Dependency direction is ``telegram -> messaging`` (allowed). The security
``tool_gate`` and spawn auto-approve are wired inline off ``ctx_builder.hooks``
(channel-neutral) so this module never imports ``kiro_crew.slack``.

``TelegramDispatcher`` is composed from owners in :mod:`kiro_crew.telegram.dispatch`:
queued-origin identity (``origin``), forum activation and reply targeting
(``addressing``), the mid-turn steer-or-queue arm (``midturn``), the ``/model`` and
``/agent`` pickers (``pickers``), inline-button routing (``callbacks``), spawn-approval
delivery (``spawn_approval``), command handlers (``commands``) and spoken replies
(``voice``). Each owner's methods are bound below as class attributes of the same
name. This module keeps the dispatcher's state, the inbound front door
(``handle_message``) and turn engine (``_run_turn``), the queue drain and receipt
wrappers, ``/stop``, ``/title``, the transcript write and the conversation-identity
helpers, because repository guards read them in this file; it stays the only import
path and patch surface. ``# noqa: F401`` marks an import an owner reads through this
module at call time, or one this module's name surface keeps. Which owner takes new
work is recorded in ``docs/system-specs/modules/messaging.md`` (Telegram channel).
"""

from __future__ import annotations

import asyncio
import html  # noqa: F401
import logging
import os  # noqa: F401
import re  # noqa: F401
import time
from contextlib import asynccontextmanager, suppress  # noqa: F401
from dataclasses import dataclass  # noqa: F401
from types import SimpleNamespace  # noqa: F401
from typing import TYPE_CHECKING, Any, NamedTuple, cast  # noqa: F401

from kiro_crew import runtime_death
from kiro_crew.acp.client import AcpError
from kiro_crew.agent_discovery import AgentInfo, list_agents  # noqa: F401
from kiro_crew.config import live
from kiro_crew.config.loader import ACTIVATION_MENTION, ACTIVATION_OFF  # noqa: F401
from kiro_crew.config.sections import _clamp_pct
from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT  # noqa: F401
from kiro_crew.context import session_store_for_turn
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.history import mint_row_mid
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, hook_gate_kwargs
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging import auto_title, privacy_mode, turn_ceiling
from kiro_crew.messaging.attachments import IngestLimits, append_attachment_context
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.commands import (  # noqa: F401
    YOLO_PHRASING_PLAIN,
    compact_unsupported_backend,
    compact_unsupported_reply,
    context_recycle_warning,
    cron_command_reply,
    format_ttl,
    lists_host_state,
    parse_dashboard_ttl,
    recycle_backend,
    recycle_warning_should_send,
    run_yolo_command,
    spawn_task_reply,
    stop_running_turn,
    task_arg_reply,
)
from kiro_crew.messaging.conversation import reserve_new_generation
from kiro_crew.messaging.dispatch import (
    admit_inbound_callback,
    build_auto_approve,
    build_directive_consumer,
    charge_turn_failure,
    consume_reinjection,
    delivery_is_muted,
    driver_turn_landed,
    open_turn_crew_log,
    predecessor_sid,
    rearm_reinjection,
    requested_model_sid,
    rollback_skill_bodies,
    slot_workspace,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.inbound_spool import InboundRoute, spool_refused_turn
from kiro_crew.messaging.link import (  # noqa: F401
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    DM_SCOPE_UNIFIED,
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    channel_namespace_of,
    parse_session_key,
    rebind_conversation_location,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.messaging.queue_drain import (  # noqa: F401
    drain_until_quiet,
    entry_channel,
    owner_token,
    register_drain,
    tag_entry,
)
from kiro_crew.messaging.renderer import (  # noqa: F401
    SilentRenderer,
    display_safe,
    new_approval_nonce,
    session_provenance_tag,
)
from kiro_crew.messaging.session_resume import (
    ResumeReleaseError,
    RoutingDecision,
    persisted_session_agent,
    refused_resume_is_restricted,
)
from kiro_crew.messaging.session_trust import (  # noqa: F401
    add_trusted_session,
    is_session_trusted,
)
from kiro_crew.messaging.spawn_approval_delivery import unpressed_wait_answer  # noqa: F401
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.messaging.turn_ceiling import TurnCeilingExceeded
from kiro_crew.messaging.upload_gate import (
    session_blocks_reads,
    session_is_restricted,
    uploads_restricted,
)
from kiro_crew.safety_override import safety_override
from kiro_crew.security import redact, redact_local_paths
from kiro_crew.sel import sel
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.session_map import ConversationOwnershipConflict  # noqa: F401
from kiro_crew.stats import Stats
from kiro_crew.telegram.attachments import process_telegram_attachments
from kiro_crew.telegram.commands import (  # noqa: F401
    ConversationState,
    build_help_text,
    is_bare_mid_turn_override,
    parse_command,
    parse_command_argument,
    parse_dashboard_argument,
    parse_mid_turn_override,
)
from kiro_crew.telegram.dispatch import addressing as _addressing
from kiro_crew.telegram.dispatch import callbacks as _callbacks
from kiro_crew.telegram.dispatch import commands as _commands
from kiro_crew.telegram.dispatch import midturn as _midturn
from kiro_crew.telegram.dispatch import pickers as _pickers
from kiro_crew.telegram.dispatch import spawn_approval as _spawn_approval
from kiro_crew.telegram.dispatch import voice as _voice
from kiro_crew.telegram.dispatch.addressing import _MENTION_RES, _mention_re  # noqa: F401
from kiro_crew.telegram.dispatch.callbacks import _UNTAGGED_OPTIONS_REFUSAL  # noqa: F401
from kiro_crew.telegram.dispatch.commands import _RELEASE_FAILURE
from kiro_crew.telegram.dispatch.origin import (  # noqa: F401
    _CHANNEL,
    _NOT_A_SENDER,
    _ORIGIN_PREFIX,
    _entry_owner,
    _inbound_origin,
    _origin_kwargs,
    _queued_origin,
    _QueuedOrigin,
)
from kiro_crew.telegram.dispatch.pickers import (  # noqa: F401
    _APP_AGENT_LINK_SEP,
    _MODEL_PICKER_MAX,
    _MODEL_PICKER_TTL_SECS,
    _PICKER_LIMIT,
    _agent_is_internal,
    _Picker,
)
from kiro_crew.telegram.dispatch.voice import (  # noqa: F401
    _AUDIO_MIMES,
    _VOICE_MIN_CHARS,
    _audio_mime,
    _read_bytes,
)
from kiro_crew.telegram.renderer import TelegramApprovalDecider
from kiro_crew.telegram.renderer import TelegramApprovalDecider as _APPROVAL_REGISTRY
from kiro_crew.telegram.renderer import (  # noqa: F401
    TelegramRenderer,
    md_to_telegram_html_safe,
)
from kiro_crew.telegram.session_resume import TelegramSessionResume
from kiro_crew.telegram.transport import (  # noqa: F401
    TELEGRAM_CAPABILITIES,
    TelegramInboundMessage,
    _coerce_id_set,
    forum_gate_outcome,
)
from kiro_crew.voice_reply import synthesis_settings, synthesize_and_deliver  # noqa: F401

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.cron import CronService
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.telegram.transport import TelegramTransport
    from kiro_crew.taskrunner import TaskRunner
    from kiro_crew.telegram.client import TelegramClient

from kiro_crew.messaging.queue_receipt import MAX_COLLAPSE as _MAX_COLLAPSE
from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI  # noqa: F401
from kiro_crew.messaging.queue_receipt import (
    ReceiptQueue,
    ReceiptSurface,
    receipt_address_key,
)

logger = logging.getLogger(__name__)

# Canonical kiro-cli agent fallback so Telegram sessions load kirocrew-core
# (spawn_run etc.) instead of kiro-cli's bare built-in default when neither an
# explicit override nor agent.default_agent is configured. Mirrors the Slack
# path's _DEFAULT_KIROCREW_AGENT.
_DEFAULT_KIROCREW_AGENT = "kirocrew"

#: A pressed option is valid only while this chat still targets the session that
#: rendered it. One constant serves both the pre-busy and post-rotation checks.
_STALE_OPTIONS_REFUSAL = (
    "🔘 These buttons belong to a conversation this chat has since moved away "
    "from, so your choice was NOT applied. Type it as a message instead."
)


#: A busy-path queue or steer retains only bare text, dropping the provenance
#: required to validate the choice when it later executes.
_BUSY_OPTIONS_REFUSAL = (
    "🔘 That conversation is busy with another turn, so your choice was NOT "
    "applied. Type it as a message once the turn finishes."
)


# Commands that must remain usable even when a remembered binding is stale or
# ambiguous. Everything else that acts on a conversation resolves the resumed
# session first, so /stop, /compact, /model, /title, /spawn and /task cannot
# silently operate on Telegram's native conversation instead.
_DETACH_EXEMPT_COMMANDS = frozenset(
    {
        "new",
        "unlink",
        "sessions",
        "help",
        "status",
        "ping",
        "cron",
        "yolo",
        "dashboard",
        "voice",
        "agent",
    }
)


# Keep queue collapse within the shared ingestion layer's per-turn file cap.
# Without this, two queued 10-photo albums would concatenate to 20 attachments
# in one turn and ingest_attachments would silently process only the first 10,
# losing the second album entirely. Mirrors discord/transport_dispatch.py.
_MAX_COLLAPSED_ATTACHMENTS = IngestLimits().max_attachments


_HELP_TEXT = build_help_text()


# Hard cap for a user-visible failure reason: one short chat message, never a
# traceback. Generous enough for the ACP entitlement message (which lists the
# models the account does include) while still bounding hostile input.
_FAILURE_REASON_MAX_CHARS = 500


def _user_safe_failure_reason(exc: BaseException) -> str | None:
    """A bounded, user-safe reason for a failed turn, or None for the generic text.

    A private-memory refusal or *permanent* :class:`AcpError` yields a
    reason: its message is already user-facing and actionable (e.g. names the
    models the account does include), and the generic "please try again"
    placeholder would be actively wrong for it. Transient and unclassified
    failures keep the retry wording, and any other exception type returns
    None — arbitrary internal errors must never leak into chat (CWE-209).

    The text is untrusted output: credentials/exfil URLs and local filesystem
    paths are redacted, newlines are collapsed, and the length is hard-capped.
    """
    if not isinstance(exc, UnknownMemoryStore) and (
        not isinstance(exc, AcpError) or exc.transient is not False
    ):
        return None
    try:
        text = redact_local_paths(redact(str(exc)))[0]
        text = " ".join(text.split())
    except Exception:
        # Fail closed to the generic placeholder: this helper runs inside the
        # turn's except block, so it must never raise (that would skip
        # record_failure and propagate out of the handler).
        logger.debug("Telegram: failure-reason sanitization failed", exc_info=True)
        return None
    if not text:
        return None
    if len(text) > _FAILURE_REASON_MAX_CHARS:
        text = text[: _FAILURE_REASON_MAX_CHARS - 1].rstrip() + "…"
    return f"⚠️ {text}"


#: ``/title`` ceiling. The dashboard sidebar row truncates well before this; the
#: cap is here so a persisted transcript never carries an unbounded title.
_TITLE_MAX_CHARS = 80


class TelegramDispatcher:
    """Coordinates Telegram turns onto the shared ``TurnDriver``.

    One instance per gateway lifetime. Holds the per-user conversation state
    (generation counter + soft-threshold flag). ``handle_message`` is wired as
    the transport's dispatch callback; ``on_callback`` is wired as the client's
    inline-button handler. ``client`` and ``bot_username`` are set by the
    gateway after construction (the latter from ``getMe``, once the token is
    proven).
    """

    def __init__(
        self,
        *,
        sessions: "SessionManager",
        ctx_builder: "ContextBuilder",
        cfg: "KiroCrewConfig",
        allowed_user_ids: set[int],
        agent: str | None = None,
        conv_log: "ConversationLog | None" = None,
        approval_mode: str = APPROVAL_INTERACTIVE,
        cron_service: "CronService | None" = None,
        subagent_manager: "SubagentManager | None" = None,
        task_runner: "TaskRunner | None" = None,
    ) -> None:
        self.sessions = sessions
        self.ctx_builder = ctx_builder
        self.cfg = cfg
        self._allowed = set(allowed_user_ids or ())
        self.agent = agent
        self.conv_log = conv_log
        self.approval_mode = approval_mode
        # Optional gateway services behind /cron, /spawn and /task. Absent on an
        # instance that runs without them (``--no-crons``, a pod), in which case
        # each command says so instead of failing silently.
        self.cron_service = cron_service
        self.subagent_manager = subagent_manager
        self.task_runner = task_runner
        # Injected by ``DashboardState.register_channel_transport`` through the
        # transport's ``dispatcher`` property, so a first turn can surface its
        # session to an open tab immediately instead of waiting for the reconciler.
        self._dashboard_state: Any = None
        self.client: "TelegramClient | None" = None
        # This bot's own registered username (no leading @), from getMe().
        # Empty until the gateway's startup call resolves -- see
        # kiro_crew.telegram.commands._strip_bot_mention for why an unset
        # value means no @-mention is ever treated as ours.
        self.bot_username: str = ""
        # This bot's own numeric id, from the same getMe(). Needed because
        # "replying to one of the bot's messages" is how a Telegram participant
        # addresses it without typing the @handle, and `is_bot` on the replied-to
        # sender is not enough — it must be THIS bot, not any bot in the Topic.
        # 0 until startup resolves, which makes the reply route inert rather than
        # over-permissive.
        self.bot_id: int = 0
        self._conv = ConversationState(seed_fn=self._seed_gen)
        # Published so a peer channel sharing this queue can wake this drain. Under
        # ``dm_scope = "unified"`` a Telegram DM and a Discord DM to the same agent
        # resolve to ONE session key and therefore one queue, and a drain can only
        # answer the entries its own channel recorded -- so the channel that sets a
        # foreign entry aside has to hand it back to its owner. See
        # ``messaging/queue_drain.py``.
        register_drain(_CHANNEL, self._drain_queue)
        # Set by maybe_start_telegram after construction (same construction-cycle
        # reason as ``client``); the config applier pushes reloaded authorization
        # fields at it.
        self.transport: "TelegramTransport | None" = None
        # Held on self: the watcher holds the owner WEAKLY, so a subscription
        # dropped here would be collected and the applier would silently stop
        # firing.
        self._config_sub = live.watch_section(
            self, "telegram", "messaging", name="TelegramDispatcher"
        )
        # The mid-turn queue receipt: one in-place "queued" bubble per session,
        # plus the lock that serializes check-then-send-then-store against the
        # end-of-turn drain. Both now live in messaging/queue_receipt.py so
        # Telegram and Discord cannot drift on the lock discipline.
        self._queue = ReceiptQueue()
        self._session_resume = TelegramSessionResume(sessions, conv_log, self._allowed)
        # Full route identity -> (lock, queued deciders). A Topic is independent from
        # every sibling Topic in the same supergroup, while one route serializes
        # route -> refusal delivery -> settlement.
        self._routing_locks: dict[str, tuple[asyncio.Lock, list[int]]] = {}
        # A message still evaluating governance predates a refusal but is not yet a
        # routing decider. Settlement waits until none remain for this exact route.
        self._routing_checks: dict[str, int] = {}
        # session_key -> the running turn's renderer, so a concurrent mid-turn
        # steer (handled in a separate _handle_busy task) can hand it the user's
        # typed steer text for the inline "↪️ steered: …" chip. Set on turn
        # start, popped in finally. Records text only — no buffer slicing, so
        # none of the old steer-split fragility.
        self._active_renderers: dict[str, TelegramRenderer] = {}
        # route -> the model id the user picked with /model, applied to every
        # session this conversation starts from now on. Keyed by ROUTE, not
        # session_key, so the choice survives /new and the idle/daily rotation
        # (a model is a preference about the peer, not about one session).
        self._model_pref: dict[tuple[str, str], str] = {}
        # route -> the kiro-cli agent the user picked with /agent. Keyed by ROUTE
        # like the model preference, so it survives the idle/daily rotation.
        self._agent_pref: dict[tuple[str, str], str] = {}
        # route -> whether this conversation speaks its answers (/voice on|off).
        # Keyed by ROUTE for the same reason as the two above: "read replies aloud
        # to me" is a preference about the peer, not about one session, so it must
        # survive /new. Absent means "use telegram.voice_replies", so an operator's
        # configured default is what a brand-new conversation gets. In memory only:
        # the durable answer is the config field, and an ad-hoc toggle that
        # outlived a restart would be a second, invisible source of truth.
        self._voice_pref: dict[tuple[str, str], bool] = {}
        # Live auto-title tasks. Held because asyncio keeps only a WEAK reference
        # to a bare create_task, so a title generation can be collected mid-flight
        # and the conversation silently keeps its truncated name. Discarded on
        # completion, so the set cannot grow with the conversation count.
        self._title_tasks: set[asyncio.Task] = set()
        # Live pickers awaiting a button press, keyed "chat:message". Telegram
        # caps callback_data at 64 bytes and model/agent ids routinely exceed
        # that, so a button carries an INDEX into one of these tables.
        self._model_pickers: dict[str, _Picker] = {}
        self._agent_pickers: dict[str, _Picker] = {}

    @property
    def dashboard_state(self) -> Any:
        return self._dashboard_state

    @dashboard_state.setter
    def dashboard_state(self, state: Any) -> None:
        self._dashboard_state = state
        self._session_resume.dashboard_state = state

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
        return live.current(self.cfg, log_prefix="telegram")

    def _soft_threshold(self) -> int:
        """The context-nudge threshold from the live config.

        Re-runs the loader's own clamp, because a reloaded value read straight
        off the section can sit outside the valid range and either nudge on every
        turn or never nudge at all. Telegram has no hard threshold, so there is
        no pair to order.
        """
        return _clamp_pct(int(getattr(self._live_cfg().telegram, "soft_threshold_pct", 80)))

    def reconfigure(self, section: Any) -> None:
        """Push a reloaded ``telegram.allowed_user_ids`` at all three holders.

        This dispatcher keeps its OWN roster: callbacks bypass
        ``transport.receive``, so ``_authorized`` re-checks against ``_allowed``,
        and the ``/sessions`` owner rule counts it. The set is mutated IN PLACE
        because ``TelegramSessionResume`` was handed this same object, and the
        resume owner is re-derived from it -- a removed operator must lose the
        callback surface and the session list on the same reload, not at the next
        restart. A transport that is not up yet is skipped: it reads the section
        fresh when it connects.
        """
        ids = _coerce_id_set(getattr(section, "allowed_user_ids", None), int)
        if ids is None:
            logger.warning(
                "telegram: allowed_user_ids is unusable in the reloaded config; the dispatcher "
                "keeps its previous roster (%d id(s))",
                len(self._allowed),
            )
        else:
            self._allowed.clear()
            self._allowed.update(ids)
            self._session_resume.reconfigure(self._allowed)
        if self.transport is not None:
            self.transport.reconfigure(section)

    # ── Turn dispatch (transport's dispatch callback) ──────────────────────

    async def handle_message(
        self,
        msg: InboundMessage,
        *,
        drain: bool = True,
        interpret_commands: bool = True,
        privacy_request: str = "",
        origin_tag: str = "",
    ) -> None:
        """Drive one authorized inbound message through TurnDriver end-to-end.

        *privacy_request* carries a modifier that was parsed off an EARLIER copy of
        this message and must still apply to the turn that finally runs it. The
        drain path needs it: it re-enters with ``interpret_commands=False``, on text
        the modifier was already stripped from, so without it a
        ``/temporary <question>`` that had to queue would run unprotected.

        *origin_tag* is the posting session encoded into an ``[OPTIONS:]``
        button. A non-empty tag makes that button valid only while the current
        and final post-rotation session keys still match it. The choice is never
        queued or steered, because those paths retain text but not provenance.
        """
        assert self.client is not None, "TelegramDispatcher.client must be set"
        user_id = int(msg.user_id)
        chat_id = int(msg.conversation_id)
        thread = getattr(msg, "thread_id", None)
        route = self._route_key(
            chat_type=getattr(msg, "chat_type", "private"),
            user_id=user_id,
            chat_id=chat_id,
            thread=thread,
        )
        reply_thread = self._route_thread(route)
        routing_id = self._session_resume.expectation_id(chat_id, reply_thread)
        self._routing_checks[routing_id] = self._routing_checks.get(routing_id, 0) + 1
        try:
            permitted = await channel_inbound_permitted("telegram")
        finally:
            remaining = self._routing_checks[routing_id] - 1
            if remaining:
                self._routing_checks[routing_id] = remaining
            else:
                self._routing_checks.pop(routing_id)
        if not permitted:
            logger.info("telegram inbound dropped: denied by channels governance policy")
            return
        native_session_key = self._session_key(route)

        async def _resolve_refused_route() -> RoutingDecision:
            if not (interpret_commands or bool(origin_tag)):
                return RoutingDecision()
            async with self._routing_turn(routing_id):
                return await self._session_resume.route(
                    user_id,
                    chat_id,
                    getattr(msg, "chat_type", "private"),
                    reply_thread,
                )

        async def _refused_turn_restricted() -> bool:
            return await refused_resume_is_restricted(
                native_session_key,
                resolve=_resolve_refused_route,
                is_restricted=self._session_restricted,
            )

        inbound_route = InboundRoute(
            conversation_id=str(chat_id),
            text=msg.text,
            user_id=str(user_id),
            thread_id=str(reply_thread) if reply_thread else "",
            message_id=str(getattr(msg, "message_id", "") or ""),
            attachments_dropped=len(getattr(msg, "attachments", None) or ()),
        )
        if not await admit_inbound_callback(
            self.sessions,
            channel_type="telegram",
            route=inbound_route,
            restricted=_refused_turn_restricted,
        ):
            return
        # Counted here, matching where Slack counts it: an inbound message the
        # governance gate refused never happened as far as the operator's own
        # traffic figures go, but everything past this point did.
        Stats().inc_message_received()
        _activation = self._activation_outcome(msg)
        if _activation is not None:
            sel().log_api_access(
                caller=str(msg.user_id) or "unknown",
                operation="telegram.inbound",
                outcome=_activation,
                source="telegram",
                resources=f"chat={msg.conversation_id}",
            )
            return
        text = msg.text

        # Per-message mid-turn override: "/queue …" / "/steer …" let the user
        # choose how THIS message is handled if it lands while a turn is running
        # (overriding the global queue_mode). Ordinary commands are parsed
        # against the ORIGINAL text — and when an override prefix IS present,
        # its payload is turn CONTENT, never a command: "/queue /new" queues the
        # literal "/new" text for after the turn instead of executing it now.
        # interpret_commands=False (the queue-drain path) skips BOTH: a drained
        # payload is replayed as pure content, so a queued "/new" reaches the
        # model as text instead of executing on drain.
        override_mode = None
        # Attachments make this a content-bearing turn, not a control command:
        # a caption of "/new" would otherwise intercept and return BEFORE
        # attachment ingestion, silently discarding the photo the user attached
        # to it. Mirrors discord/transport_dispatch.py's interpret_as_command.
        interpret_as_command = interpret_commands and not msg.attachments
        if interpret_as_command and parse_command(text, self.bot_username) is None:
            override_mode, text = parse_mid_turn_override(text, self.bot_username)

        # ── Command intercept (no LLM session needed; skipped for override
        # payloads and drained queue content — see above) ──
        cmd = (
            parse_command(text, self.bot_username)
            if interpret_as_command and override_mode is None
            else None
        )
        decision = RoutingDecision()
        # A HOST-scoped listing is not conversation work: `/spawn list` and
        # `/task status` report on the whole box, so routing them through a resumed
        # binding lets a stale or refused one withhold output that has nothing to do
        # with that session. Asked of the argument, not the command name, because
        # the same verb is conversation-scoped with a different argument.
        lists_host = cmd is not None and lists_host_state(cmd, parse_command_argument(text))
        wants_routing = (
            (interpret_commands or bool(origin_tag))
            and (cmd not in _DETACH_EXEMPT_COMMANDS)
            and not lists_host
        )
        if wants_routing:
            async with self._routing_turn(routing_id) as queued:
                decision = await self._session_resume.route(
                    user_id,
                    chat_id,
                    getattr(msg, "chat_type", "private"),
                    reply_thread,
                )
                if decision.refusal is not None:
                    landed = await self._reply(chat_id, decision.refusal, thread=reply_thread)
                    if (
                        landed is not None
                        and len(queued) == 1
                        and not self._routing_checks.get(routing_id)
                    ):
                        await self._session_resume.settle(chat_id, reply_thread, decision)
                    return
        resumed_key = decision.resumed_key

        if cmd == "new":
            try:
                left_resumed = await self._session_resume.leave_resumed_session(
                    chat_id, reply_thread
                )
            except ResumeReleaseError:
                await self._reply(chat_id, _RELEASE_FAILURE, thread=reply_thread)
                return
            self._conv.bump_gen(route)
            new_session_key = self._session_key(route)
            saved = await reserve_new_generation(
                self.sessions,
                new_session_key,
                channel_type="Telegram",
            )
            message = "✅ New conversation started."
            if left_resumed is not None:
                message = "✅ New conversation started — left the resumed session."
            if not saved:
                message += "\n⚠️ The new conversation could not be saved for restart."
            await self._reply(chat_id, message, thread=reply_thread)
            return
        if cmd == "compact":
            self._conv.clear_awaiting(route)
            await self._handle_compact(route, chat_id, session_key=resumed_key)
            return
        if cmd == "link":
            await self._handle_link(route, chat_id, resumed_key=resumed_key)
            return
        if cmd == "unlink":
            await self._handle_unlink(route, chat_id)
            return
        if cmd == "help":
            await self._reply(chat_id, _HELP_TEXT, thread=reply_thread)
            return
        if cmd == "stop":
            await self._handle_stop(
                route, chat_id, origin=_inbound_origin(msg), session_key=resumed_key
            )
            return
        if cmd == "model":
            await self._handle_model(
                route,
                chat_id,
                parse_command_argument(text),
                session_key=resumed_key,
            )
            return
        if cmd == "agent":
            await self._handle_agent(route, chat_id, parse_command_argument(text))
            return
        # A privacy modifier is DEFERRED, not applied here. The session key this
        # early in the ladder is the pre-rotation one, and the idle/daily rotation
        # below can mint a different key for the very turn the user is asking to
        # protect — marking the old key would leave the turn unrestricted while
        # reporting success. So record the request and apply it once the final key
        # is known. A bare modifier still short-circuits, since there is nothing to
        # answer, and applies against the un-rotated key it is scoped to.
        #
        # Seeded from the argument, not reset to empty: a drained turn arrives with
        # the modifier already parsed off an earlier copy of itself, and the deferred
        # apply below is the one place that can still honour it.

        if cmd in (privacy_mode.MODE_TEMPORARY, privacy_mode.MODE_INCOGNITO):
            if resumed_key is not None:
                # A dashboard slot owns its memory_mode. Marking only Telegram's
                # process-local tracker would announce privacy while the persistent
                # live slot kept recording the next turn. Runtime mode switching is
                # not a dashboard capability, so fail visibly instead of inventing
                # a second authority. This covers both a bare modifier and
                # `/temporary <message>`: the message is NOT processed.
                await self._reply(
                    chat_id,
                    "🔒 Privacy mode can't be changed while a dashboard session is "
                    "resumed. Your message was NOT processed. Use /unlink or /new first.",
                    thread=reply_thread,
                )
                return
            rest = parse_command_argument(text)
            if not rest:
                try:
                    applied = await privacy_mode.apply_mode(
                        cmd,
                        # Rotation FIRST: a bare modifier returns before the turn path
                        # would have rotated, so keying on the un-rotated generation
                        # protects a session the next message abandons.
                        resumed_key or self._rotated_session_key(route),
                        source="telegram",
                        caller=str(user_id),
                        sessions=self.sessions,
                        notify=lambda note: self._notify(chat_id, note, thread=reply_thread),
                    )
                except privacy_mode.PrivacyModeRefused:
                    # Audited and announced by apply_mode: the conversation was not
                    # made private and nothing runs.
                    return
                if not applied:
                    # Idempotent, so apply_mode said nothing. Say something anyway:
                    # silence reads as the command having failed.
                    await self._reply(chat_id, privacy_mode.notice(cmd), thread=reply_thread)
                return
            # "/temporary summarise this" both marks the conversation and answers,
            # matching Slack's "!temporary summarise this".
            privacy_request = cmd
            text = rest
            cmd = None
        if cmd == "voice":
            await self._handle_voice(route, chat_id, parse_command_argument(text), reply_thread)
            return
        if cmd == "status":
            await self._reply(chat_id, Stats().summary(), thread=reply_thread)
            return
        if cmd == "ping":
            # Answered here, never by the model: the point is to prove the gateway
            # is alive without depending on a provider that may be the thing wedged.
            await self._reply(chat_id, "pong", thread=reply_thread)
            return
        if cmd == "sessions":
            if not await self._require_direct_chat(
                cmd, route, chat_id, user_id, thread=reply_thread, subject="conversation list"
            ):
                return
            await self._session_resume.show_picker(
                self.client,
                user_id,
                chat_id,
                getattr(msg, "chat_type", "private"),
                reply_thread,
                query=parse_command_argument(text),
                native_key=self._session_key(route),
            )
            return
        if cmd == "title":
            await self._handle_title(
                route, chat_id, parse_command_argument(text), session_key=resumed_key
            )
            return
        if cmd == "cron":
            if not await self._require_direct_chat(
                cmd, route, chat_id, user_id, thread=reply_thread, subject="scheduled job list"
            ):
                return
            await self._handle_cron(
                chat_id, parse_command_argument(text), caller=str(user_id), thread=reply_thread
            )
            return
        if cmd in ("spawn", "task"):
            arg = parse_command_argument(text)
            # The SAME command is conversation-scoped with one argument and
            # host-scoped with another: `/spawn <task>` starts work for this
            # conversation, while `/spawn list` renders every subagent on the box
            # with its task text. `lists_host_state` is asked rather than the
            # command name, because reading one subcommand and generalizing to the
            # command is what let the listing through.
            if lists_host_state(cmd, arg) and not await self._require_direct_chat(
                cmd, route, chat_id, user_id, thread=reply_thread, subject=f"{cmd} listing"
            ):
                return
            if cmd == "spawn":
                await self._handle_spawn(
                    route,
                    chat_id,
                    arg,
                    thread=reply_thread,
                    session_key=resumed_key,
                )
            else:
                await self._handle_task(
                    chat_id,
                    arg,
                    route=route,
                    thread=reply_thread,
                    session_key=resumed_key,
                )
            return
        if cmd == "yolo":
            await self._handle_yolo(
                chat_id, parse_command_argument(text), user_id, thread=reply_thread
            )
            return
        if cmd == "dashboard":
            await self._handle_dashboard(route, chat_id, text, user_id)
            return
        # A lone "/queue" / "/steer" is a directive missing its message body.
        # Answering with the usage beats forwarding the token to the model, which
        # would answer the literal string and read as a broken feature. Gated on
        # interpret_as_command so a caption on an attachment is never read as a
        # bare directive -- that would answer with usage and drop the file.
        if (
            interpret_as_command
            and override_mode is None
            and is_bare_mid_turn_override(text, self.bot_username)
        ):
            await self._reply(
                chat_id,
                "Those take a message: /queue <msg> or /steer <msg>.",
                thread=reply_thread,
            )
            return

        session_key = resumed_key or self._session_key(route)
        if origin_tag and session_provenance_tag(session_key) != origin_tag:
            await self._reply(chat_id, _STALE_OPTIONS_REFUSAL, thread=reply_thread)
            return
        if self.sessions.is_busy(session_key):
            if origin_tag:
                await self._reply(chat_id, _BUSY_OPTIONS_REFUSAL, thread=reply_thread)
                return
            if resumed_key is not None:
                # NOT `_handle_busy`: that queues into THIS dispatcher's queue, drained
                # only at the tail of a TELEGRAM-driven turn and replayed with resume
                # routing off, so the message would run later in the NATIVE session.
                # The dashboard slot has its own steer path and queue; the refusal
                # stays for the cases the slot cannot take.
                await self._handle_resumed_busy(
                    session_key,
                    msg,
                    text,
                    override_mode,
                    thread=reply_thread,
                    route=route,
                    interpret_commands=interpret_commands,
                    drain=drain,
                    principal=str(user_id),
                )
                return
            await self._handle_busy(
                session_key,
                msg,
                text,
                override_mode,
                thread=reply_thread,
                privacy_request=privacy_request,
                caller=str(user_id),
            )
            return

        if resumed_key is None:
            session_key = self._rotated_session_key(route)
        if origin_tag and session_provenance_tag(session_key) != origin_tag:
            await self._reply(chat_id, _STALE_OPTIONS_REFUSAL, thread=reply_thread)
            return
        privacy_mode.hydrate(self.sessions, session_key)
        if privacy_request:
            try:
                await privacy_mode.apply_mode(
                    privacy_request,
                    session_key,
                    source="telegram",
                    caller=str(user_id),
                    sessions=self.sessions,
                    notify=lambda note: self._notify(chat_id, note, thread=reply_thread),
                )
            except privacy_mode.PrivacyModeRefused:
                # Audited and announced by apply_mode. The message is NOT run:
                # running it with the mode dropped is the leak the modifier
                # exists to prevent.
                return
        await self._run_turn(
            msg,
            text,
            session_key=session_key,
            resumed_key=resumed_key,
            route=route,
            user_id=user_id,
            chat_id=chat_id,
            thread=thread,
            reply_thread=reply_thread,
            interpret_commands=interpret_commands,
            drain=drain,
        )

    async def _run_turn(
        self,
        msg: InboundMessage,
        text: str,
        *,
        session_key: str,
        resumed_key: str | None,
        route: tuple[str, str],
        user_id: int,
        chat_id: int,
        thread: str | None,
        reply_thread: int | None,
        interpret_commands: bool,
        drain: bool,
    ) -> None:
        """Run one model turn for a message ``handle_message`` admitted, then drain.

        By here the message has passed governance, admission and activation, is
        not a command and did not land mid-turn, and *session_key* is the key the
        turn runs under, with any privacy request already applied to it. This is
        the turn itself: acquiring the session, building the prompt, driving
        ``TurnDriver``, the post-turn bookkeeping, classifying a failure,
        finalizing the renderer and releasing the session, and then draining the
        queue that built up behind the turn. Repository guards read its constructs
        in this file: the tool gate, the turn ceiling and its mute-aware refusal,
        the crew-log opener, the persist-then-pin order and the failure charge.

        *thread* is the message's own Topic id, which the voice leg answers in;
        *reply_thread* is the Topic the route resolved for every other reply.
        """
        assert self.client is not None, "TelegramDispatcher.client must be set"
        channel_id = f"telegram:{user_id}"
        agent = self._resolve_agent(route)
        if resumed_key is not None:
            persisted = await asyncio.to_thread(persisted_session_agent, self.conv_log, resumed_key)
            if persisted:
                agent = persisted

        decider = (
            TelegramApprovalDecider(session_key=session_key)
            if self.approval_mode == APPROVAL_INTERACTIVE
            else None
        )
        renderer = TelegramRenderer(
            self.client,
            chat_id,
            TELEGRAM_CAPABILITIES,
            session_key=session_key,
            message_thread_id=reply_thread,
            show_thinking=bool(self._live_cfg().telegram.show_thinking),
            uploads_allowed=not await self._uploads_restricted(session_key),
            reply_to_message_id=self._reply_target(msg, interpret_commands=interpret_commands),
        )
        # Same gate as Discord, for the same reason: Telegram also runs its own
        # copy of the turn loop rather than going through ``drive_turn``, so a
        # disconnected conversation would otherwise keep answering.
        muted = delivery_is_muted(self.sessions, session_key, TelegramRenderer.channel_type)
        # Handed to the driver AND closed in the finally. Not a reassignment of
        # ``renderer`` because the concrete ``close`` is not inert (it finalizes the
        # "🤔" placeholder and can surface an error), and a muted turn must leave
        # nothing behind in the conversation. Typed as a union rather than the base
        # ``Renderer`` because this channel WIDENS close to take ``failure_reason``.
        out_renderer: TelegramRenderer | SilentRenderer = (
            SilentRenderer(TELEGRAM_CAPABILITIES, TelegramRenderer.channel_type)
            if muted
            else renderer
        )
        # Expose this turn's renderer so a concurrent mid-turn steer (a separate
        # _handle_busy task) can hand it the user's typed steer text for the
        # inline "↪️ steered: …" chip. Popped in finally.
        # Not published when muted: the steer path calls the channel-specific
        # ``note_steer`` and already skips cleanly on absence, so this both
        # silences the chip in a disconnected conversation and keeps that
        # channel-local API off the shared substitute.
        if not muted:
            self._active_renderers[session_key] = renderer

        # Everything acquire-dependent runs INSIDE the try so the finally always
        # finalizes the placeholder (renderer.close -> no perma-"🤔 …"), even if
        # get_or_create itself raises on a cold-start failure. release() is gated
        # on _acquired so we never release a semaphore we didn't hold. Mirrors
        # slack/transport_dispatch.py.
        _acquired = False
        # The provider THIS turn acquired, for the failure handler's attribution
        # question. Bound before the try so every handler can read it -- an
        # attribution flag read on a path its assignment cannot reach is an
        # UnboundLocalError inside an except arm, not a guard. Stays None when
        # get_or_create never returned, and an unattributable death charges as
        # before.
        _turn_provider: object | None = None
        failure_reason: str | None = None
        attachment_temp_paths: list[str] = []
        # Post-compaction re-injection bookkeeping for the finally: whether this
        # turn consumed the one-shot flag, and whether it landed (recorded success).
        _needs_reinjection = False
        _turn_landed = False
        # The turn's driver, for the finally: it records whether the backend
        # compacted the session, on every exit path.
        driver: TurnDriver | None = None
        try:
            # Ack placeholder first (before the potentially slow cold-start);
            # on_turn_start is idempotent so the driver's later call no-ops.
            # Skipped when muted, as in the Discord twin.
            if not muted:
                await renderer.on_turn_start()
            _memory_store = await session_store_for_turn(self.ctx_builder, session_key)
            # Imported here, not at module level: this facade's bound names are
            # pinned to the split's base (the composition contract test).
            from kiro_crew.start_priority import person_priority

            provider, is_new, resumed = await self.sessions.get_or_create(
                session_key,
                start_priority=person_priority(msg.person_origin),
                agent=agent,
                channel_id=channel_id,
                # "" is the Auto row's stored value; collapse it to None so Auto
                # means "as if never picked". get_or_create gates its own model
                # resolution on `model is None`, so passing "" would skip that
                # and land on the provider factory's narrower fallback instead.
                #
                # Scoped to a NATIVE turn. ``_model_pref`` is this Telegram route's
                # own choice, and a resumed dashboard session already has a model of
                # its own; handing the route's preference to a cold start would run
                # that conversation under a model its owner never picked, silently
                # and for every later turn. A resumed session therefore passes None
                # and lets its own persisted model resolve.
                model=(None if resumed_key is not None else self._model_pref.get(route) or None),
            )
            _acquired = True
            # Hold the provider this turn obtained, for the failure handler's
            # attribution question. Captured HERE rather than looked up when a
            # failure is handled: the recovery paths replace a dead session, so a
            # lookup at failure time answers for the replacement and the question
            # silently becomes "was the NEW runtime shared".
            _turn_provider = provider
            if resumed_key is None or channel_namespace_of(session_key):
                # The session's crew log, opened the moment the allocation lands
                # and before ANY further await: the work ledger appends every write
                # to the acting session's log and rolls back one it cannot record,
                # so a DM admitted as a conductor needs its log to exist before its
                # first ledger call -- and a turn that bails between the allocation
                # and a later opener (a failed attachment fetch, a renderer error)
                # would leave a live session whose log is first created on the NEXT
                # turn, by then a warm reuse, so the previous edge would never be
                # written. The predecessor is the allocation boundary's own capture,
                # taken inside its critical section and consumed here after the
                # claim -- no read of the mapping around the call, which a
                # concurrent turn's allocate-and-recycle could stale while this turn
                # waited inside the allocation. Every CHANNEL session this dispatcher
                # runs is opened here, including a same-DM native history picked
                # through ``/sessions`` (``resumed_key`` naming a channel key): no
                # dashboard runner ever handles its turns, so this is its only
                # opener. Only a resumed DASHBOARD session is left alone -- its
                # opener is the dashboard's, which alone holds its lineage. The
                # workspace is read off the dashboard slot this conversation is
                # surfaced under, the source a tab on it states the same fact from,
                # so the two writers of this log agree. Never raises, never suspends.
                open_turn_crew_log(
                    provider,
                    session_key=session_key,
                    agent=agent,
                    resumed=resumed,
                    ctx_builder=self.ctx_builder,
                    previous_sid=predecessor_sid(self.sessions, session_key),
                    model_requested=requested_model_sid(self.sessions, session_key),
                    workspace=slot_workspace(self.dashboard_state, session_key),
                )
            # Extraction's approved root is the provider's OWN resolved cwd, so a
            # path lexically outside it is refused before any metadata probe.
            # Read defensively: an absent cwd must mean "no uploads", never a dead
            # turn — this capability may not add a failure mode to the path that
            # answers the user's message.
            renderer.authorize_upload_root(getattr(provider, "cwd", ""))
            # Feeds the turn footer's context gauge. Read off the provider rather
            # than passed at construction, because the provider does not exist
            # until the session is acquired.
            renderer.attach_context_client(getattr(provider, "client", None))
            is_new_own_session = is_new and resumed_key is None
            if is_new_own_session:
                await self.sessions.set_channel(session_key, channel_id)
            if resumed_key is None:
                # A resumed dashboard session already owns its surface and binding.
                # Reassert only Telegram's native conversation mirror -- and record
                # that conversation as the session's ORIGIN, the in-memory fact the
                # dashboard reads for unattended output about the session and for
                # session control's owner-DM check (a DM whose mirror IS its own
                # conversation is one audience; a mirror aimed anywhere else is not,
                # and only the recorded origin can tell the two apart). Discord's
                # dispatcher writes both on the same turn for the same reasons.
                # The same key-based unified guard ``bind_origin_mirror`` applies:
                # a ``unified:{agent}`` bucket collapses every user's DMs into one
                # session, so it has no single origin to record.
                setter = getattr(self.sessions, "set_origin_link", None)
                if setter is not None and channel_namespace_of(session_key) != DM_SCOPE_UNIFIED:
                    setter(session_key, self._origin_mirror_link(route, chat_id))
                self._bind_origin_mirror(session_key, route, chat_id)
            # ── Attachment ingestion (mirrors Discord) ──
            if msg.attachments:
                attachment_result = await process_telegram_attachments(self.client, msg.attachments)
                attachment_temp_paths = list(attachment_result.temp_paths)
                text = append_attachment_context(text, attachment_result)
            if not text:
                return
            # Publish this turn's session identity so managed MCP tools resolve
            # X-Session-Key; one shared writer lives in messaging.identity.
            await publish_turn_identity(self.sessions, session_key)
            # This conversation's own silo, from the session's RECORDED binding and
            # never from ``agent``: that value is a kiro agent name, a namespace
            # disjoint from ``cfg.agents``, so a store derived from it resolves to
            # ``default`` for exactly the crew that configured otherwise. Private
            # memory was prepared before provider acquisition and fails closed.
            # A compaction drops session-start context. Read-and-clear the
            # one-shot flag so this turn re-injects that context exactly once;
            # the finally re-arms it if this turn never lands.
            _needs_reinjection = consume_reinjection(self.sessions, session_key)
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                self.ctx_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=channel_id,
                agent=agent,
                memory_store=_memory_store,
                resumed=resumed,
                needs_reinjection=_needs_reinjection,
                runtime_source="telegram",
                context_provider=provider,
                # Temporary mode reads NO memory, which is the half the transcript
                # gate cannot cover: refusing to WRITE still leaves yesterday's
                # memories and lessons in today's prompt. Incognito deliberately
                # still reads — that is the documented difference between the two.
                # Resolved dashboard-aware, because a RESUMED session carries its
                # mode on the slot rather than in this channel's tracker.
                blocks_reads=await self._blocks_memory_reads(session_key),
                # Who is speaking. Slack has always passed this; without it the
                # model cannot address the user or tell two participants of a
                # forum Topic apart. Already narrowed to Telegram's own username
                # grammar by ``prompt_safe_handle``, and empty for an account with
                # no @handle, which ``build_message`` treats as "omit the line".
                user_display_name=getattr(msg, "username", "") or None,
            )

            # PreToolUse security gate (channel-neutral, off ctx_builder.hooks):
            # sensitive-path keystone + governance ceiling + deny-list. Returns
            # "deny" (un-overridable), "auto_approve", or "" (passthrough).
            def _tool_gate(event: Any) -> str:
                result = self.ctx_builder.hooks.on_tool_call(
                    getattr(event, "title", "") or "",
                    session_key=session_key,
                    agent=agent,
                    **hook_gate_kwargs(event),
                )
                if result.action == TOOL_DENY:
                    return "deny"
                if result.action == TOOL_AUTO_APPROVE:
                    return "auto_approve"
                return ""

            driver = TurnDriver(
                provider,
                out_renderer,
                approval_mode=self.approval_mode,
                decider=decider,
                # Preserve the auto_approve_subagent_spawn hook for spawn_run.
                # The shared builder keys on canonical event identity
                # (tool_name/is_shell), never the model-authored title.
                auto_approve_tool=build_auto_approve(self.ctx_builder),
                # /yolo: read the grant per request, not once at boot, so turning
                # it on (or letting it expire) takes effect on the very next tool
                # instead of after a gateway restart. TurnDriver runs the
                # PreToolUse gate BEFORE this, so a hard deny still wins.
                # Global YOLO OR this conversation's own Trust grant. Read per
                # request, not once at boot, so a Trust press or a YOLO expiry
                # takes effect on the very next tool. TurnDriver runs the
                # PreToolUse gate BEFORE this, so a hard deny still wins.
                auto_approve_session=lambda: (
                    safety_override().is_active() or is_session_trusted(session_key)
                ),
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
                closing_gate=turn_ceiling.gate(
                    session_key, lambda: self.sessions.begin_turn(session_key)
                ),
            )
            accumulated = await driver.run(full_message)

            # ── Post-turn bookkeeping (each guarded so a failure here can't
            # fall through to the except and re-record the successful turn). ──
            self.sessions.record_success(session_key)
            # Beside the counter it stands in for: a landed turn clears the
            # shared-death streak exactly as it clears the consecutive-failure
            # count, so the streak stays a consecutive run rather than a lifetime
            # total whose bound is permanently tripped.
            runtime_death.clear_shared_deaths(session_key)
            # The prompt (with any re-injected context) reached the model and
            # the turn completed, so the finally must NOT restore the flag --
            # unless the user cancelled it, which discards that prompt.
            _turn_landed = driver_turn_landed(driver)
            Stats().inc_message_success()
            if accumulated and not muted and self._voice_enabled(route):
                # Its own bookkeeping step, and last-effort by design: the text
                # answer has already landed, so a TTS failure must not reach the
                # except below and re-record a successful turn as a failure.
                # ``muted`` conversations get nothing outbound at all, voice
                # included — a disconnected conversation that started talking would
                # be the loudest possible version of the bug the mute gate exists
                # to prevent.
                await self._speak_reply(
                    route, chat_id, accumulated, int(thread) if thread else None
                )
            # Decided BEFORE the persist below, and used by BOTH it and the
            # auto-title dispatch, so the two cannot disagree. That write mints
            # this conversation's deterministic fallback name, and auto-title's
            # guard refuses a record that already carries a name -- so a fallback
            # written here would be the conversation's name for good, with the
            # claim retained and no later exchange retrying. The suppression is
            # passed explicitly rather than inferred from claim timing, because the
            # claim is taken AFTER this write: the pin below reads a record this
            # turn has already persisted.
            #
            # ``accumulated`` is required: a turn that produced no text has
            # nothing to name, and titling it would spend a background turn to be
            # told SKIP. A restricted session persists nothing to title, and a
            # resumed session is not this dispatcher's to name.
            _will_auto_title = bool(
                resumed_key is None
                and accumulated
                and not privacy_mode.is_restricted(session_key)
                # Cheap synchronous peek: ``try_claim`` below tests this very
                # membership, so without it the pin's thread hop would be paid
                # and then thrown away on every later message of every
                # already-named conversation.
                and not auto_title.is_titled(session_key)
            )
            try:
                # Circular import: the dashboard package imports the channel
                # transports on its boot path, so this edge only exists at call
                # time. Same reason as the ``surface_dispatcher_session`` import
                # below.
                from kiro_crew.dashboard.channel_slots import project_channel_turn_live

                # Decided HERE, on the loop, because a resumed dashboard session's
                # restriction lives on its live slot (or, with the tab closed, in
                # the persisted transcript) — neither is reachable from the worker
                # thread the write runs in. BOTH paths below must be gated: the
                # projection appends to a dashboard slot and marks it dirty, so a
                # later slot flush would persist those rows even if this direct
                # writer were skipped.
                dashboard_restricted = await self._session_restricted(session_key)
                if not dashboard_restricted:
                    mirror_mids = project_channel_turn_live(
                        self.dashboard_state,
                        session_key,
                        text,
                        accumulated,
                        broadcast_user=True,
                    )
                    await asyncio.to_thread(
                        self._persist_turn,
                        session_key,
                        text,
                        accumulated,
                        is_new_own_session,
                        agent=agent,
                        mirror_mids=mirror_mids,
                        auto_title_pending=_will_auto_title,
                    )
            except Exception:
                logger.warning(
                    "Telegram: persist_turn failed session=%s", session_key, exc_info=True
                )
            # Auto-title, fire-and-forget. Without it a conversation's name is
            # frozen at the first forty characters of the first message forever —
            # the deterministic fallback ``_persist_turn`` writes — and the only
            # correction is a manual /title. Claim-and-spawn, never awaited: the
            # answer has already been delivered, so the user waits on nothing.
            #
            # Placed AFTER the persist above, because the record the pin reads is
            # the one ``_persist_turn`` mints: pinning ahead of it would read
            # ABSENT on a conversation's first exchange, which the guard refuses,
            # so a new thread would lose its generated name and pay a second
            # background turn for it on the next exchange. Both Slack dispatchers
            # are ordered the same way -- they persist their turn before they pin
            # -- so an ABSENT pin means the record is genuinely gone rather than
            # not yet written. It stays OUTSIDE the ``dashboard_restricted``
            # branch above so titling keeps the conditions it has here and gains
            # none from that gate.
            #
            # Isolated like every other bookkeeping step here, so failing even to
            # SPAWN the task never re-records this successful turn as a failure.
            try:
                if _will_auto_title:
                    # Pin BEFORE claiming, and both before scheduling. The pin read
                    # suspends on a thread, so claiming first would hold the claim
                    # across that await with nothing scheduled yet to release it,
                    # and a cancellation there would strand it -- the claim is
                    # process-wide, so this key could not be named again until the
                    # gateway restarts. The pin still precedes ``create_task``,
                    # which is what closes the scheduling-tick window: read inside
                    # the task, one tick is enough for a delete plus a re-message
                    # on this thread to pin the replacement.
                    _title_pin = await auto_title.pin_record(self.conv_log, session_key)
                    if auto_title.try_claim(session_key):
                        _title_task = asyncio.create_task(
                            auto_title.maybe_auto_title(
                                self.sessions,
                                self.conv_log,
                                session_key,
                                text,
                                accumulated,
                                pin=_title_pin,
                                source="telegram",
                            )
                        )
                        self._title_tasks.add(_title_task)
                        _title_task.add_done_callback(self._title_tasks.discard)
            except Exception:
                logger.warning(
                    "Telegram: auto-title dispatch failed session=%s",
                    session_key,
                    exc_info=True,
                )
            if is_new_own_session:
                try:
                    # Circular import: dashboard boot imports channel packages.
                    from kiro_crew.dashboard.channel_slots import (
                        surface_dispatcher_session,
                    )

                    await surface_dispatcher_session(self)
                except Exception:
                    logger.warning(
                        "Telegram: immediate dashboard session surface failed session=%s",
                        session_key,
                        exc_info=True,
                    )
            try:
                await self._maybe_notice(chat_id, route, session_key, provider)
            except Exception:
                logger.warning(
                    "Telegram: maybe_notice failed session=%s", session_key, exc_info=True
                )
            try:
                sel().log_api_access(
                    caller=f"telegram:{user_id}",
                    operation="transport_dispatch.handle",
                    outcome="success",
                    source="telegram",
                    resources=f"session={session_key}",
                )
            except Exception:
                logger.debug("Telegram: success audit failed", exc_info=True)
        except TurnCeilingExceeded as exc:
            # At the conversation's turn ceiling, so no turn opened. Unlike the
            # shutdown branch below this is NOT spooled -- the spool replays a
            # message our restart dropped, and this one was refused on purpose --
            # and it is not charged to the circuit breaker. The notice is
            # rendered so the placeholder finalizes as the pause message rather
            # than a perma-"thinking".
            #
            # Through ``out_renderer``, the same one the driver streams to, NOT
            # the concrete one: a muted conversation substitutes ``SilentRenderer``
            # because the dashboard disconnected it, and posting there would put a
            # message into a chat that is supposed to hear nothing -- once per
            # inbound message, since the latch does not clear on its own.
            logger.warning(
                "Telegram turn ceiling reached for %s -- conversation paused", session_key
            )
            await turn_ceiling.render_refusal(out_renderer, exc)
        except SessionClosingError:
            # Shutdown began between the claim and the dispatch, so no turn ever
            # opened. Caught ahead of the generic handler so a restart is not
            # charged to the circuit breaker via `record_failure` nor counted as
            # a failed message — neither is true of a session that never
            # misbehaved. `failure_reason` is left as it was, so the `finally`
            # finalizes the placeholder with this channel's usual notice instead
            # of leaving a perma-"🤔 …".
            logger.info(
                "Telegram: aborting dispatch for %s — gateway is shutting down",
                session_key,
            )
            # Durable inbound spool. Written HERE and nowhere else:
            # this is the one point where the payload is still in memory AND the
            # turn is provably unopened, so a replay on the next start cannot
            # double-answer a turn that actually ran. Telegram cannot recover this
            # from its own offset either — ``_persistable_offset`` bounds duplicate
            # replay, not loss, because the next long poll server-confirms the
            # batch it just dispatched.
            #
            # NOT for a restricted session. ``/incognito`` and ``/temporary`` are a
            # promise that this conversation persists nothing, and the spool is a
            # durable file holding the message verbatim. The same predicate that
            # gates the durable-history write gates this one; a refused restricted
            # message degrades to the pre-feature loss, which is what the user asked
            # for by choosing the mode.
            if not await self._session_restricted(session_key):
                await spool_refused_turn(
                    channel_type="telegram",
                    route=InboundRoute(
                        conversation_id=str(chat_id),
                        # ``msg.text``, NOT the local ``text``: by here the latter
                        # has attachment context appended, whose inlined temp paths
                        # are gone after a restart, and may have had a mid-turn
                        # override prefix stripped. The spool wants what the user
                        # typed.
                        text=msg.text,
                        user_id=str(user_id),
                        thread_id=str(reply_thread) if reply_thread else "",
                        message_id=str(getattr(msg, "message_id", "") or ""),
                        attachments_dropped=len(getattr(msg, "attachments", None) or ()),
                    ),
                )
        except Exception as exc:
            logger.exception("Telegram transport_dispatch: error handling message")
            # Permanent, user-actionable failures (e.g. model entitlement)
            # surface their own bounded reason instead of the misleading
            # generic retry text; everything else stays generic (None).
            failure_reason = _user_safe_failure_reason(exc)
            if _acquired:
                # A dying runtime reaches this generic handler as one more
                # exception, so without the attribution question every tenant of
                # one process charges its own breaker for a single process event.
                # ``_turn_provider`` is the one THIS turn acquired, never a lookup
                # made while handling the failure.
                await charge_turn_failure(
                    self.sessions,
                    session_key,
                    exc=exc,
                    provider=_turn_provider,
                    channel_type="telegram",
                )
                Stats().inc_message_failed()
        finally:
            # An approval window the driver never awaited -- the prompt went out
            # and the turn then ended before the decider -- has no wait of its own
            # to close it, so it would outlive this turn with its nonce still
            # armed and authorizing a press.
            #
            # ``_APPROVAL_REGISTRY`` is ``TelegramApprovalDecider`` under a second
            # name. Reservations are class state, so the sweep has to reach the
            # class holding them, and the construction name above is a seam
            # callers and tests substitute to observe the decider a turn builds.
            # Sweeping through that name aims at the substitute: it raises on a
            # plain function, and on a stand-in class it clears an empty registry
            # and leaves the real window armed past the end of its turn.
            _APPROVAL_REGISTRY.discard_session(session_key)
            # A turn that consumed the post-compaction flag but never landed
            # discarded the prompt carrying the re-injected context, and a backend
            # that compacted the session during the turn dropped it; either way
            # the flag is set so the next turn re-injects it.
            rearm_reinjection(
                self.sessions,
                session_key,
                consumed=_needs_reinjection,
                landed=_turn_landed,
                compacted=getattr(driver, "compaction_completed", False) is True,
            )
            rollback_skill_bodies(self.ctx_builder, session_key, landed=_turn_landed)
            # Always finalize the placeholder (no perma-"🤔 …"), even if
            # get_or_create raised before the semaphore was held. Only release
            # the semaphore if we actually acquired it.
            #
            # ``close()`` is best-effort and must NEVER prevent the three steps
            # after it. A renderer that fails to finalize -- a malformed
            # Telegram response, a socket dropped mid-edit -- would otherwise
            # skip ALL of them: the session semaphore is never given back (and
            # because it is keyed by SESSION, every later message in that
            # conversation blocks forever and the queue never drains), the
            # ``_active_renderers`` entry leaks, and the attachment temp files
            # stay on disk. Discord and the shared pipeline both already guard
            # this; Telegram was the remaining copy that did not.
            try:
                await out_renderer.close(failure_reason=failure_reason)
            except Exception:
                logger.warning(
                    "Telegram: renderer.close failed session=%s",
                    session_key,
                    exc_info=True,
                )
            self._active_renderers.pop(session_key, None)
            if _acquired:
                self.sessions.release(session_key)
            await asyncio.to_thread(cleanup_attachments, attachment_temp_paths)

        # Now that the turn is released, run anything that queued during it
        # (queue_mode == "queue"). ``drain`` is False for drained turns so the
        # loop stays iterative at one level (no recursion); ``limit`` bounds it.
        #
        # Deliberately handed NOTHING about this turn but its session key: the
        # replay envelope comes from each queued entry's own recorded origin, and
        # under ``dm_scope = "unified"`` the person who opened this turn is not
        # necessarily the person who queued during it.
        if drain:
            await self._drain_queue(session_key)

    @asynccontextmanager
    async def _routing_turn(self, route_id: str) -> "AsyncIterator[list[int]]":
        """Serialize decision settlement for one exact DM or Topic, never provider work."""
        lock, deciders = self._routing_locks.setdefault(route_id, (asyncio.Lock(), []))
        deciders.append(1)
        try:
            async with lock:
                yield deciders
        finally:
            deciders.pop()
            if not deciders:
                self._routing_locks.pop(route_id, None)

    # dispatch/midturn.py
    _handle_busy = _midturn._handle_busy
    _handle_resumed_busy = _midturn._handle_resumed_busy

    async def _drain_queue(self, session_key: str) -> None:
        """Collapse every message ONE SENDER queued during the just-finished turn
        into ONE combined turn (order preserved, blank-line joined) and answer them
        together, rather than replaying each as a separate turn.

        The dequeue + receipt flip run together under ``self._queue.lock`` so a
        concurrent mid-turn ``_enqueue_with_receipt`` (which takes the same lock)
        cannot interleave and leave an orphaned receipt. The combined turn itself
        runs OUTSIDE the lock -- messages that arrive during it open a fresh
        receipt and drain after the next turn. Only the queued text is replayed
        (matching what ``enqueue`` persists for DM channels: text only).

        One combined turn gets ONE envelope, so it may only combine messages that
        SHARE one -- same sender, same chat, same Topic. That is
        :attr:`_QueuedOrigin.sender_key`, and it is taken from the FIRST entry this
        iteration collapses, never from the turn that opened the queue: under
        ``dm_scope = "unified"`` one session key, and therefore one queue, is shared
        by every allow-listed person, so a queue holding two of them is reachable on
        the live path. Anything from a different sender or place defers itself and
        everything behind it, so FIFO stays exact and the outer loop drains it next
        as its own turn under its own envelope.

        Which is why this method is given the session key and nothing else: the
        opener's identity is not an input it could accidentally fall back to.

        An entry ANOTHER transport recorded shares this queue under the same scope and
        cannot be answered here at all. It is set aside, and because it has already
        been accepted and receipted, its owner's drain is woken once this pump is done
        -- outside ``self._queue.lock``, since that drain takes its own lock and runs a
        whole turn. See ``messaging/queue_drain.py`` for why the cascade terminates.
        """
        # Iterate rather than recurse: one burst can span multiple
        # attachment-capped turns, and a message deferred by the cap must drain
        # in THIS pump rather than waiting for unrelated future user input.
        # Mirrors the Discord drain.
        #
        # Channels whose entries this pump set aside, so they can be woken after it.
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
        # Imported here, not at module level: this facade's bound names are pinned to
        # the split's base (the composition contract test).
        from kiro_crew.messaging.queue_drain import entry_person_origin

        while True:
            texts: list[str] = []
            all_attachments: list[Any] = []
            remainder: list[tuple[str, str, dict]] = []
            privacy_requests: list[str] = []
            defer_rest = False
            # The origin this iteration answers, taken from the FIRST entry it
            # collapses. None until that entry is read.
            origin: _QueuedOrigin | None = None
            # Whether a person sent any entry this turn collapses.
            person = False
            async with self._queue.lock:
                # Drain the ENTIRE queue under the lock, then split: the first
                # _MAX_COLLAPSE messages FROM ONE SENDER collapse into this turn;
                # the rest are re-enqueued IN ORIGINAL ORDER (the queue is now
                # empty, so re-adding preserves FIFO) to drain after the next turn.
                # This bounds the combined prompt without dropping or reordering
                # surplus.
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
                    # Never collapse past the shared ingestion cap: the extra files
                    # would be dropped inside ingest_attachments with the user given
                    # no indication, so defer instead. Mirrors the Discord drain.
                    exceeds_attachment_cap = bool(
                        texts
                        and item_attachments
                        and len(all_attachments) + len(item_attachments)
                        > _MAX_COLLAPSED_ATTACHMENTS
                    )
                    fits = (
                        not defer_rest
                        and len(texts) < _MAX_COLLAPSE
                        and not exceeds_attachment_cap
                        # sender_key, NOT the whole origin: the origin also carries
                        # the sender's @handle, which they can change between two
                        # messages, so comparing all of it would make one person's
                        # own burst compare unequal and drain as N turns.
                        and item_origin.sender_key == origin.sender_key
                    )
                    if fits:
                        texts.append(item[1])
                        all_attachments.extend(item_attachments)
                        person = person or entry_person_origin(item[2])
                        requested = item[2].get("privacy_request") or ""
                        if isinstance(requested, str) and requested:
                            privacy_requests.append(requested)
                    else:
                        # Once one message does not fit, defer it AND everything
                        # behind it, so queue order stays exact.
                        defer_rest = True
                        remainder.append(item)
                # How many of the set-aside entries belong to the sender this turn
                # answers. NOT ``len(remainder)``: that also counts entries from a
                # DIFFERENT sender and entries another TRANSPORT recorded, each of
                # which drains in its own turn in its own chat. Showing those to this
                # sender would promise them a follow-up for messages they never sent
                # -- and when their own burst fit in one turn, a "+N deferred" where
                # their true count is zero.
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
                        # Re-enqueued VERBATIM: its attachments, its privacy modifier
                        # (a deferred message drains in a LATER iteration of this pump,
                        # and dropping the request here would unprotect exactly the
                        # messages the collapse cap pushed back), and its origin -- an
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
                        int(origin.chat_id),
                        texts,
                        own_deferred,
                        owner=_entry_owner(origin),
                    )
            if not texts or origin is None:
                return
            if remainder:
                logger.debug(
                    "telegram: drain set aside %d message(s) for %s, %d of them this "
                    "sender's own (collapse cap %d / attachment cap %d); the rest "
                    "belong to another sender or another transport. All drain in "
                    "order, this sender's in the next iteration of this pump",
                    len(remainder),
                    session_key,
                    own_deferred,
                    _MAX_COLLAPSE,
                    _MAX_COLLAPSED_ATTACHMENTS,
                )
            combined = "\n\n".join(texts)
            await self.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    # Every addressing and attribution field comes from the queued
                    # entry's own origin, so the turn runs in the sender's chat under
                    # the sender's identity even when someone else opened the queue.
                    user_id=origin.user_id,
                    conversation_id=origin.chat_id,
                    text=combined,
                    # Carry the QUEUED message's ORIGINAL route so the drained turn
                    # resolves to the SAME forum session key -- a plain DM-shaped
                    # InboundMessage would drain a queued forum message under the DM
                    # key instead.
                    thread_id=origin.thread_id or None,
                    chat_type=origin.chat_type,
                    username=origin.username,
                    attachments=all_attachments,
                    # The queued entries' own flag: a gateway-built wake can have been
                    # queued too (kiro_crew.start_priority).
                    person_origin=person,
                ),
                drain=False,
                # Drained payloads are pure turn content: a queued "/new" must reach
                # the model as literal text, not execute as a command on drain.
                interpret_commands=False,
                # The one exception, and it is not a command being executed: a privacy
                # modifier was already parsed and stripped before this text was
                # queued, so it travels as state rather than as text. The STRICTEST of
                # the collapsed messages wins, because they answer as one turn under
                # one key -- honouring only the first would let a later `/incognito`
                # in the same burst be silently downgraded to whatever led it. Now
                # scoped to ONE sender's messages, so one person's modifier can no
                # longer restrict a turn answering someone else.
                privacy_request=privacy_mode.strictest(privacy_requests),
            )

    # ── Mid-turn queue receipt (single, in-place, persistent record) ───────

    def _receipt_surface(self, chat_id: int, thread: int | None) -> ReceiptSurface:
        """A receipt surface with this conversation's address already bound.

        Binding ``chat_id`` AND the forum ``thread`` here is what keeps forum
        routing out of the shared queue module: it never sees an address at all.
        """
        # cast, not assert: mypy does not carry an assert-narrowed local
        # into the nested class body below, so the closure would still see
        # ``TelegramClient | None``. The caller path always has a live client.
        client = cast("TelegramClient", self.client)
        reply = self._reply

        class _Surface:
            label = "telegram"
            # ``chat_id`` alone, deliberately: a forum route's ``comp`` is
            # ``"{chat_id}:{thread}"``, so ``_session_key`` already gives each Topic its
            # own entry and no two Topics ever share a bubble for this key to keep
            # apart. ``thread`` routes a send, which the bubble's own retained surface
            # already carries.
            address_key = receipt_address_key("telegram", chat_id)

            async def send_receipt(self, body: str) -> Any | None:
                return await reply(chat_id, body, thread=thread)

            async def edit_receipt(self, msg_id: Any, body: str) -> bool:
                return await client.edit_message(chat_id, msg_id, body)

        return _Surface()

    async def _enqueue_with_receipt(
        self,
        session_key: str,
        chat_id: int,
        text: str,
        *,
        thread: int | None = None,
        attachments: list[Any] | None = None,
        privacy_request: str = "",
        origin: _QueuedOrigin,
        person_origin: bool = False,
    ) -> bool:
        """Atomically enqueue a mid-turn message and create/grow its collapsing
        "⏳ Queued (N): …" receipt, under ``self._queue.lock``.

        Holding the lock across BOTH the enqueue and the receipt bookkeeping is
        what makes this race-free against the end-of-turn drain (which takes the
        same lock to dequeue + flip): the drain either sees this message queued
        WITH its receipt or sees neither yet -- never a half state that would
        orphan a bubble. Returns True if queued; False if the turn finished in
        the window (``enqueue`` is a no-op once the semaphore is free), so the
        caller runs the message as a fresh turn instead.

        *origin* is REQUIRED and keyword-only: it is who sent THIS message and where
        its reply goes, and the drain replays the entry under it. A default would be
        a way to enqueue an unattributed message, which under
        ``dm_scope = "unified"`` the drain could only answer under someone else's
        identity. *person_origin* is the message's own
        ``InboundMessage.person_origin``, which the drained replay's start priority
        is read from.
        """
        # Imported here, not at module level: this facade's bound names are pinned to
        # the split's base (the composition contract test).
        from kiro_crew.messaging.queue_drain import person_tag

        assert self.client is not None
        async with self._queue.lock:
            if not self.sessions.enqueue(
                session_key,
                str(time.time()),
                text,
                force=False,
                attachments=list(attachments or []),
                **person_tag(person_origin),
                # Rides WITH the message, for the same reason its attachments do:
                # the drain re-enters with `interpret_commands=False` on text the
                # modifier was already stripped from, so a request left behind here
                # is one no later parse can recover.
                privacy_request=privacy_request,
                **_origin_kwargs(origin),
            ):
                return False
            await self._queue.create_or_grow_locked(
                session_key, self._receipt_surface(chat_id, thread), text, _entry_owner(origin)
            )
            return True

    async def _receipt_flip_locked(
        self,
        session_key: str,
        chat_id: int,
        answered: list[str],
        deferred: int = 0,
        *,
        owner: str,
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record and drop the
        live entry so the next mid-turn burst opens a fresh receipt. Caller MUST
        hold ``self._queue.lock`` (the drain holds it across dequeue + flip).

        ``answered`` is the subset actually answered by this turn (capped at
        ``_MAX_COLLAPSE``); the count reflects it -- not the full queued list --
        so a >cap burst doesn't overstate what this turn answers. ``deferred``
        (>0 only past the cap) is noted so the remainder isn't silently implied.

        ``owner`` is WHOSE messages those are, which the flip needs because one bubble
        can list several principals': a GROUP chat gives every member one chat address
        and one session key, so a drain answering one member must leave the others'
        lines -- and the entry that is their only handle -- alone. REQUIRED and
        keyword-only, the same way the registry transition it forwards to spells it:
        this wrapper has exactly one caller and that caller always can name the
        principal, so an omission is a type error rather than a silent return to
        retiring the whole bubble.

        ``chat_id`` is the chat the receipt BUBBLE lives in, which the drain takes
        from the queued entry's own origin rather than from the turn that opened the
        queue -- ``create_or_grow_locked`` posted that bubble into the chat of
        whoever queued first. The ``None`` thread is correct and not an omission:
        ``flip_answering_locked`` only ever EDITS, and ``edit_message`` addresses a
        message by its id, which already identifies it within its Topic.
        """
        assert self.client is not None
        await self._queue.flip_answering_locked(
            session_key, self._receipt_surface(chat_id, None), answered, deferred, owner=owner
        )

    # dispatch/commands.py
    _handle_dashboard = _commands._handle_dashboard

    # dispatch/voice.py
    _voice_enabled = _voice._voice_enabled
    _handle_voice = _voice._handle_voice
    _speak_reply = _voice._speak_reply

    async def _handle_stop(
        self,
        route: tuple[str, str],
        chat_id: int,
        *,
        origin: _QueuedOrigin,
        session_key: str | None = None,
    ) -> None:
        """Hard cancel: abort the in-flight turn and clear THIS caller's queued messages.

        The cooperative-cancel contract, the lock ordering across
        ``clear_queue`` + the receipt finalize, and both replies live in
        :func:`~kiro_crew.messaging.commands.stop_running_turn`; this supplies
        Telegram's address. The receipt surface is built with no ``thread``
        because ``editMessageText`` is not threaded -- the message id already
        identifies the message within its Topic -- while the reply itself must
        land back in the originating Topic.

        *origin* is this caller's own, recorded the same way their queued entries were,
        so the clear matches their entries and no one else's: under
        ``dm_scope = "unified"`` this queue also holds other people's messages, and on
        another transport too.
        """
        assert self.client is not None
        await stop_running_turn(
            self.sessions,
            session_key or self._session_key(route),
            queue=self._queue,
            surface=self._receipt_surface(chat_id, None),
            owner=_entry_owner(origin),
            deliver=lambda text: self._reply(chat_id, text, thread=self._route_thread(route)),
        )

    # dispatch/commands.py
    _handle_yolo = _commands._handle_yolo

    # dispatch/pickers.py
    _model_choices = _pickers._model_choices
    _prune_pickers = staticmethod(_pickers._prune_pickers)
    _consume_picker = _pickers._consume_picker
    _handle_model = _pickers._handle_model
    _apply_model = _pickers._apply_model

    # dispatch/addressing.py
    _addresses_this_bot = _addressing._addresses_this_bot
    _activation_outcome = _addressing._activation_outcome

    def _rotated_session_key(self, route: tuple[str, str]) -> str:
        """Settle idle/daily rotation for *route*, then return its session key.

        The single place rotation is resolved, and every path that needs a key a
        SIDE EFFECT will be attached to goes through it. Resolving the key without
        settling rotation first is the bug this exists to make unreachable: the
        pre-rotation key can be one the very next message abandons, so a privacy
        mode applied to it protects a session that is already dead, and reports
        success while doing it.

        Not used by the mid-turn busy check, deliberately: that one must ask about
        the CURRENT generation, because rotating first could mint a new key, miss
        the running turn, and let a second concurrent turn bypass steer/queue.
        Reading is safe on a stale key; WRITING to one is not.

        ``maybe_rotate`` is time-based, so calling it more than once inside one
        message's handling is a no-op after the first.
        """
        self._conv.maybe_rotate(
            route,
            time.time(),
            idle_minutes=int(self._live_cfg().messaging.idle_reset_minutes),
            daily_reset_hour=int(self._live_cfg().messaging.daily_reset_hour),
        )
        return self._session_key(route)

    # dispatch/addressing.py
    _reply_target = staticmethod(_addressing._reply_target)

    async def _blocks_memory_reads(self, session_key: str) -> bool:
        """True when this session must take NO memory or lessons into its prompt.

        The read counterpart of :meth:`_session_restricted`. ``temporary`` is the
        only mode that blocks reads, and for a RESUMED ``dashboard:`` key that fact
        lives on the slot, not in this channel's tracker — so
        ``privacy_mode.is_temporary`` alone would let stored memories into the
        model for a temporary dashboard session.
        """
        from kiro_crew.dashboard.handlers._shared import _probe_persisted_session

        return await session_blocks_reads(
            self.dashboard_state,
            session_key,
            persisted_probe=_probe_persisted_session,
        )

    async def _session_restricted(self, session_key: str) -> bool:
        """True when this session is incognito or temporary, so nothing may persist.

        The same predicate the upload ceiling reads
        (:func:`kiro_crew.messaging.upload_gate.session_is_restricted`), asked here
        before the durable history write. A resumed Telegram turn can carry a
        ``dashboard:`` key whose restriction lives on its live slot rather than in
        this process's channel trackers, which is exactly the case
        ``privacy_mode.is_restricted`` cannot see.

        The LIVE slot is the authoritative rung and the one the exposure needs: a
        restricted tab that is open is exactly the case that would otherwise write.
        With the tab closed this falls back to the persisted ``memory_mode``, which
        restricts on an incognito/temporary marker AND on an unreadable mode whose
        transcript EXISTS — an ambiguous stem or a header no normal session wrote,
        where an incognito session can hide. ``unknown_denies`` is off here, unlike
        the upload ceiling, only so a truly ABSENT record still records: nothing on
        disk claims that session is restricted, and denying there would stop
        recording every conversation not yet written. A legacy header missing the
        field reads ``persistent``, so it never reaches the unknown case.

        The persisted-transcript probe is passed IN for the same reason as in
        :meth:`_uploads_restricted`: ``messaging`` may not import ``dashboard``, so
        the import lives here and stays function-local because the dashboard
        gateway imports the channel transports.
        """
        from kiro_crew.dashboard.handlers._shared import _probe_persisted_session

        return await session_is_restricted(
            self.dashboard_state,
            session_key,
            persisted_probe=_probe_persisted_session,
            unknown_denies=False,
        )

    async def _uploads_restricted(self, session_key: str) -> bool:
        """True when this session must not ship local file bytes to Telegram.

        The ladder and its fail-closed reasoning live in
        :func:`kiro_crew.messaging.upload_gate.uploads_restricted`, shared with the
        Discord dispatcher; this supplies Telegram's dashboard state and audit
        label.

        A resumed Telegram turn can carry a ``dashboard:`` key, so this gate is
        active rather than preparatory. A forum Topic is readable by every member
        of its supergroup, making the restricted-session ceiling mandatory before
        any local file bytes are inspected or delivered.

        The persisted-transcript probe is passed IN because ``messaging`` may not
        import ``dashboard``; this package may, so the import lives here, and stays
        function-local because the dashboard gateway imports the channel
        transports.
        """
        from kiro_crew.dashboard.handlers._shared import _probe_persisted_session

        return await uploads_restricted(
            self.dashboard_state,
            session_key,
            channel_type="telegram",
            persisted_probe=_probe_persisted_session,
        )

    # dispatch/pickers.py
    _installed_agent_names = staticmethod(_pickers._installed_agent_names)
    _agent_choices = _pickers._agent_choices
    _handle_agent = _pickers._handle_agent
    _apply_agent = _pickers._apply_agent

    # ── /title and the service-backed commands ─────────────────────────────

    async def _handle_title(
        self,
        route: tuple[str, str],
        chat_id: int,
        arg: str,
        *,
        session_key: str | None = None,
    ) -> None:
        """Rename this conversation, so the dashboard sidebar row is legible.

        Without this the title is frozen at the first 40 characters of the first
        message and can never be corrected. The text is user-authored and lands
        in a persisted transcript and the dashboard, so it is redacted and capped.
        """
        thread = self._route_thread(route)
        title = " ".join(redact(arg).split())[:_TITLE_MAX_CHARS]
        if not title:
            await self._reply(chat_id, "Usage: /title <text>", thread=thread)
            return
        if self.conv_log is None:
            await self._reply(chat_id, "No conversation log to rename.", thread=thread)
            return
        # The rotated key, because a title is DURABLE: renaming the generation the
        # idle window has just retired leaves the next message in a different,
        # untitled session and the rename looks like it was lost.
        titled_key = session_key or self._rotated_session_key(route)
        # A restricted session writes NOTHING, and a title is not an exception: the
        # metadata write CREATES the transcript file, so on a `/temporary` or
        # `/incognito` conversation this one command would persist user-authored
        # content for a mode that promised not to. `_persist_turn` gates the same
        # way, and so does Slack's own `/title`; this is another write on the same
        # promise rather than a new rule.
        #
        # The shared predicate is required here because *titled_key* may be a
        # RESUMED ``dashboard:`` key. ``privacy_mode.is_restricted`` is only the
        # answer for Telegram-native conversations; it reads a channel-local
        # process tracker that a dashboard slot never populates and therefore
        # fails open for an incognito dashboard session.
        if await self._session_restricted(titled_key):
            await self._reply(
                chat_id,
                "🔒 This conversation is private, so its name isn't saved.",
                thread=thread,
            )
            return
        try:
            # Circular dependency: dashboard boot imports channel transports, so
            # the channel-to-slot bridge stays local like the turn projection.
            from kiro_crew.dashboard.channel_slots import rename_channel_title_live

            renamed_live = await rename_channel_title_live(
                self.dashboard_state,
                titled_key,
                title,
            )
            if not renamed_live:
                await asyncio.to_thread(self.conv_log.set_title, titled_key, title)
        except Exception:
            logger.warning("telegram /title: set_title failed", exc_info=True)
            await self._reply(chat_id, "⚠️ Couldn't rename this conversation.", thread=thread)
            return
        await self._reply(chat_id, f"✅ Renamed to “{title}”.", thread=thread)

    # dispatch/commands.py
    _handle_cron = _commands._handle_cron
    _handle_spawn = _commands._handle_spawn
    _handle_task = _commands._handle_task

    # dispatch/callbacks.py
    on_callback = _callbacks.on_callback

    # dispatch/spawn_approval.py
    deliver_spawn_approval = _spawn_approval.deliver_spawn_approval
    _spawn_prompt_destination_permitted = _spawn_approval._spawn_prompt_destination_permitted
    _spawn_chat_target = _spawn_approval._spawn_chat_target

    # ── Helpers ────────────────────────────────────────────────────────────

    def _callback_session_key(
        self,
        route: tuple[str, str],
        chat_id: int,
        thread_id: int | None,
        user_id: int,
        chat_type: str,
    ) -> str:
        """The current callback target, or ``""`` when routing is unsafe."""
        resolution = self._session_resume.resolve_inbound(chat_id, thread_id)
        if resolution.ambiguous:
            return ""
        if resolution.key is not None and not self._session_resume.is_owner(
            user_id, chat_id, chat_type
        ):
            return ""
        return resolution.key or self._session_key(route)

    def _authorized(self, user_id: int) -> bool:
        # Deny-by-default (callbacks bypass transport.receive, so re-check here).
        return bool(user_id) and bool(self._allowed) and user_id in self._allowed

    def _configured_agent(self) -> str:
        """The agent a conversation uses when the user has picked none."""
        return self.agent or self.cfg.agent.default_agent or _DEFAULT_KIROCREW_AGENT

    def _resolve_agent(self, route: tuple[str, str] | None = None) -> str:
        """The kiro-cli agent for *route*: an explicit /agent pick, else the default.

        Route-aware because the agent is part of the session key
        (``build_dm_session_key``), which is also why a pick necessarily starts a
        fresh conversation rather than swapping the spec under a live one — the
        spec decides which MCP servers and skills that kiro-cli process loaded at
        spawn, so there is nothing to swap.
        """
        if route is not None:
            picked = self._agent_pref.get(route)
            if picked:
                return picked
        return self._configured_agent()

    def _route_key(
        self,
        *,
        chat_type: str,
        user_id: int,
        chat_id: int,
        thread: str | int | None,
    ) -> tuple[str, str]:
        """Map an inbound message/callback to its conversation-identity key.

        Returns ``(slot, comp)`` where ``slot`` selects the session namespace:
          * private DM -> ``(CHAT_TYPE_DIRECT, str(user_id))`` -- byte-for-byte
            the pre-forum identity, so DM keys are unchanged.
          * supergroup forum Topic -> ``(CHAT_TYPE_FORUM, "{chat_id}:{thread}")``.

        A threadless supergroup (General) message is denied at the forum gate
        and never reaches here; the ``str(chat_id)`` fallback below is defensive
        dead code (kept for safety), NOT a served route.

        The tuple is used as the ``ConversationState`` key (per-topic generation)
        and, via ``_session_key``, as the session-key ``comp`` + ``chat_type``.
        """
        if chat_type in ("group", "supergroup"):
            comp = f"{chat_id}:{thread}" if thread else str(chat_id)
            return CHAT_TYPE_FORUM, comp
        return CHAT_TYPE_DIRECT, str(user_id)

    @staticmethod
    def _route_thread(route: tuple[str, str]) -> int | None:
        """The forum Topic id for a ``route``, or None for a DM.

        Mirrors ``_route_key``'s ``comp`` encoding: a forum Topic route carries
        ``"{chat_id}:{thread}"`` -> the Topic id; a DM (direct) route -> None.
        An authorized forum turn always carries a Topic (General is denied at
        the gate), so the threadless-``comp`` -> None case is only the defensive
        fallback. Threads every dispatcher-originated send back into the
        SAME Topic the turn came from.
        """
        slot, comp = route
        if slot == CHAT_TYPE_FORUM and ":" in comp:
            return int(comp.split(":", 1)[1])
        return None

    async def _notify(self, chat_id: int, note: str, *, thread: int | None = None) -> None:
        """Send a one-line notice, adapting ``_reply`` to a ``None``-returning hook.

        A method rather than a closure per call site: the shared helpers that take a
        ``notify=`` callback (``privacy_mode.apply_mode``) are reached from several
        branches, and a nested adapter defined under one of them is one refactor from
        a ``NameError`` in another.
        """
        await self._reply(chat_id, note, thread=thread)

    # dispatch/commands.py
    _require_direct_chat = _commands._require_direct_chat
    _reply_markdown = _commands._reply_markdown

    async def _reply(
        self, chat_id: int, text: str, *, thread: int | None = None, **kw: Any
    ) -> int | None:
        """Send a user-facing chat message, threaded into the originating forum
        Topic (``thread``) or the DM chat (``thread`` is None).

        Single choke point for every dispatcher-originated send (command
        confirmations, queue receipts, the soft-threshold notice, ``[OPTIONS:]``
        echoes) so a forum turn's side messages land in the user's Topic. A
        threadless supergroup General message is denied at the gate, so no served
        send ever lands in the supergroup's General chat. ``answer_callback`` is
        intentionally NOT routed here -- it is a callback ack, not a chat send.
        """
        assert self.client is not None
        return await self.client.send_message(chat_id, text, message_thread_id=thread, **kw)

    def _session_key(self, route: tuple[str, str]) -> str:
        slot, comp = route
        gen = self._conv.current_gen(route)
        return build_dm_session_key(
            "telegram",
            self._resolve_agent(route),
            comp,
            gen=gen,
            dm_scope=str(self.cfg.messaging.dm_scope),
            chat_type=slot,
        )

    def _seed_gen(self, route: tuple[str, str]) -> int:
        slot, comp = route
        return seed_generation(
            self.sessions,
            channel="telegram",
            agent=self._resolve_agent(route),
            user_id=comp,
            dm_scope=str(self.cfg.messaging.dm_scope),
            chat_type=slot,
        )

    def _origin_mirror_link(self, route: tuple[str, str], chat_id: int) -> ChannelLink:
        """The one Telegram conversation spelling shared by mirror and resume paths."""
        return self._session_resume.link_for(chat_id, self._route_thread(route))

    def _bind_origin_mirror(self, session_key: str, route: tuple[str, str], chat_id: int) -> None:
        """Mirror this conversation's dashboard tab back to Telegram, unasked.

        The rule, the re-assert and the opt-out live in
        :func:`~kiro_crew.messaging.link.bind_origin_mirror`, shared with the
        Discord dispatcher; this only supplies Telegram's spelling of "this
        conversation".

        Synchronous and called ON the loop, like every other session-map
        mutation. Interleaving is ordered by ``session_map._MAP_LOCK``, not by the
        loop; what keeps the call here is that the write is BOUNDED — one
        whole-map rewrite, on a conversation's first turn only.
        """
        bind_origin_mirror(
            self.sessions,
            key=session_key,
            location=self._origin_mirror_link(route, chat_id),
        )

    # dispatch/commands.py
    _handle_link = _commands._handle_link
    _handle_unlink = _commands._handle_unlink

    def _persist_turn(
        self,
        session_key: str,
        user_text: str,
        reply_text: str,
        is_new: bool,
        agent: str | None = None,
        mirror_mids: tuple[str, str] | None = None,
        auto_title_pending: bool = False,
    ) -> None:
        """Persist one atomic turn, deduplicating rows already projected live.

        ``auto_title_pending`` says a generated name is on its way for this
        conversation, so the deterministic fallback below is skipped: auto-title's
        guard refuses a record that already carries a name, so writing one here
        would make the first forty characters of the first message the permanent
        name and retain the claim, leaving nothing to retry. The caller decides it
        once and uses the same value for its own dispatch, so the two cannot
        disagree. It defaults to False, so a caller that does not dispatch
        auto-title writes the fallback unconditionally.

        ``privacy_mode.is_restricted`` is this channel's OWN privacy gate and is
        checked here, at the only writer, so it covers the turn, the drained queue
        and the steered continuation alike.

        It is NOT the whole ceiling for a RESUMED dashboard session. That
        predicate reads a process-local tracker which only an inbound CHANNEL
        message populates, so it answers ``False`` for an incognito dashboard slot
        and fails open. That rung is decided by the CALLER, through
        :meth:`_session_restricted`, which skips this call entirely — it needs the
        live slot registry and, failing that, an await on the persisted transcript,
        neither reachable from the worker thread this runs in. A second caller must
        make the same check before calling.
        """
        if self.conv_log is None or privacy_mode.is_restricted(session_key):
            return
        with self.conv_log.atomic_appends(session_key):
            if mirror_mids is not None:
                user_mid, assistant_mid = mirror_mids
                self.conv_log.append_if_absent(
                    session_key,
                    "user",
                    user_text,
                    agent=agent,
                    mid=user_mid,
                )
                if reply_text:
                    self.conv_log.append_if_absent(
                        session_key,
                        "assistant",
                        reply_text,
                        agent=agent,
                        mid=assistant_mid,
                    )
            else:
                self.conv_log.append(
                    session_key,
                    "user",
                    user_text,
                    agent=agent,
                    mid=mint_row_mid(),
                )
                if reply_text:
                    self.conv_log.append(
                        session_key,
                        "assistant",
                        reply_text,
                        agent=agent,
                        mid=mint_row_mid(),
                    )
            if is_new and not auto_title_pending and not auto_title.is_titled(session_key):
                title = (user_text or "").strip().replace("\n", " ")[:40] or "Telegram"
                self.conv_log.set_title(session_key, title)

    async def _maybe_notice(
        self, chat_id: int, route: tuple[str, str], session_key: str, provider: Any
    ) -> None:
        """Soft-threshold context warning as a SEPARATE message (not persisted).

        Kept out of the streamed answer buffer so it is never persisted into the
        assistant turn and replayed next turn as though the assistant said it.
        The hard-compaction backstop is the backend autocompactor
        (``session.autocompact_pct``).
        """
        pct = self.sessions.check_context_usage(session_key, provider)
        soft_pct = self._soft_threshold()
        if recycle_backend(provider):
            # Crew restarts this backend's session at the threshold; warn once
            # before it, offering a fresh start instead of a compaction.
            if recycle_warning_should_send(self.sessions, self._conv, route, session_key, pct):
                assert self.client is not None
                await self._reply(
                    chat_id, context_recycle_warning(), thread=self._route_thread(route)
                )
            return
        if pct >= soft_pct and compact_unsupported_backend(provider):
            # Capability gate: the nudge advises /compact, which this
            # backend refuses — it compacts on its own as context fills, so
            # there is nothing for the user to act on.
            return
        if pct >= soft_pct and not self._conv.is_awaiting(route):
            self._conv.set_awaiting(route)
            assert self.client is not None
            await self._reply(
                chat_id,
                "⚠️ Context is getting long. Use /compact to compress or " "/new to start fresh.",
                thread=self._route_thread(route),
            )

    # dispatch/commands.py
    _handle_compact = _commands._handle_compact
