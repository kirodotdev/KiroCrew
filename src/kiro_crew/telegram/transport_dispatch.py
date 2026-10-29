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

Dependency direction is ``telegram -> messaging`` (allowed). The turn engine
(``dispatch/turn_engine.py``) wires the security ``tool_gate`` and spawn auto-approve
inline off ``ctx_builder.hooks`` (channel-neutral), so neither it nor this module
imports ``kiro_crew.slack``.

``TelegramDispatcher`` is composed from owners in :mod:`kiro_crew.telegram.dispatch`:
queued-origin identity (``origin``), forum activation and reply targeting
(``addressing``), the mid-turn steer-or-queue arm (``midturn``), the turn engine with
its transcript write and context notice (``turn_engine``), the queue drain and the
receipt wrappers it shares with the steer path (``turn_queue``), the ``/model`` and
``/agent`` pickers (``pickers``), inline-button routing (``callbacks``), spawn-approval
delivery (``spawn_approval``), command handlers (``commands``) and spoken replies
(``voice``). Each owner's methods are bound below as class attributes of the same
name. This module keeps the dispatcher's state, the inbound front door
(``handle_message``), ``/stop``, ``/title``, the restriction predicates, the
user-facing failure reason and the conversation-identity helpers, because repository
guards read them in this file; it stays the only import path and patch surface.
``# noqa: F401`` marks an import an owner reads through this module at call time, or
one this module's name surface keeps. Which owner takes new work is recorded in
``docs/system-specs/modules/messaging.md`` (Telegram channel).
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

from kiro_crew import runtime_death  # noqa: F401
from kiro_crew.agent_discovery import AgentInfo, list_agents  # noqa: F401
from kiro_crew.agent_sdk.drivers.acp_vocab import is_user_worded_failure
from kiro_crew.agent_switch_command import ChannelAgentPicks
from kiro_crew.config import live
from kiro_crew.config.loader import ACTIVATION_MENTION, ACTIVATION_OFF  # noqa: F401
from kiro_crew.config.sections import _clamp_pct
from kiro_crew.constants import DENY_CAUSE_APPROVAL_TIMEOUT  # noqa: F401
from kiro_crew.context import session_store_for_turn  # noqa: F401
from kiro_crew.executors import run_in_embed_pool  # noqa: F401
from kiro_crew.history import mint_row_mid  # noqa: F401
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY, hook_gate_kwargs  # noqa: F401
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging import auto_title, privacy_mode, turn_ceiling  # noqa: F401
from kiro_crew.messaging.attachments import IngestLimits, append_attachment_context  # noqa: F401
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments  # noqa: F401
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
from kiro_crew.messaging.dispatch import (  # noqa: F401
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
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver  # noqa: F401
from kiro_crew.messaging.identity import (  # noqa: F401
    channel_inbound_permitted,
    publish_turn_identity,
)
from kiro_crew.messaging.inbound_spool import InboundRoute, spool_refused_turn  # noqa: F401
from kiro_crew.messaging.link import (  # noqa: F401
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    CHAT_TYPE_PRIVATE_TOPIC,
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
from kiro_crew.messaging.session_resume import (  # noqa: F401
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
from kiro_crew.messaging.turn_ceiling import TurnCeilingExceeded  # noqa: F401
from kiro_crew.messaging.upload_gate import (
    session_blocks_reads,
    session_is_restricted,
    uploads_restricted,
)
from kiro_crew.safety_override import safety_override  # noqa: F401
from kiro_crew.security import redact, redact_local_paths
from kiro_crew.sel import sel
from kiro_crew.session_allocation import SessionClosingError  # noqa: F401
from kiro_crew.session_map import ConversationOwnershipConflict  # noqa: F401
from kiro_crew.stats import Stats
from kiro_crew.telegram.attachments import process_telegram_attachments  # noqa: F401
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
from kiro_crew.telegram.dispatch import turn_engine as _turn_engine
from kiro_crew.telegram.dispatch import turn_queue as _turn_queue
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
from kiro_crew.telegram.dispatch.turn_engine import _APPROVAL_REGISTRY  # noqa: F401
from kiro_crew.telegram.dispatch.turn_queue import _MAX_COLLAPSED_ATTACHMENTS  # noqa: F401
from kiro_crew.telegram.dispatch.voice import (  # noqa: F401
    _AUDIO_MIMES,
    _VOICE_MIN_CHARS,
    _audio_mime,
    _read_bytes,
)
from kiro_crew.telegram.renderer import TelegramApprovalDecider  # noqa: F401
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

from kiro_crew.messaging.queue_receipt import MAX_COLLAPSE as _MAX_COLLAPSE  # noqa: F401
from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI  # noqa: F401
from kiro_crew.messaging.queue_receipt import (  # noqa: F401
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


_HELP_TEXT = build_help_text()


# Hard cap for a user-visible failure reason: one short chat message, never a
# traceback. Generous enough for the ACP entitlement message (which lists the
# models the account does include) while still bounding hostile input.
_FAILURE_REASON_MAX_CHARS = 500


def _user_safe_failure_reason(exc: BaseException) -> str | None:
    """A bounded, user-safe reason for a failed turn, or None for the generic text.

    A private-memory refusal or a backend failure Kiro Crew worded for a person
    (``is_user_worded_failure``) yields a reason: its message is already
    user-facing and actionable (e.g. names the models the account does
    include), and the generic "please try again" placeholder would be actively
    wrong for it. Every other failure keeps the retry wording, including a
    non-retryable ``AcpError`` whose text is the provider's own fallback
    message — arbitrary internal errors must never leak into chat (CWE-209).

    The text is untrusted output: credentials/exfil URLs and local filesystem
    paths are redacted, newlines are collapsed, and the length is hard-capped.
    """
    if not isinstance(exc, UnknownMemoryStore) and not is_user_worded_failure(exc):
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
        self._queue: ReceiptQueue = ReceiptQueue()
        self._session_resume: TelegramSessionResume = TelegramSessionResume(
            sessions, conv_log, self._allowed
        )
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
        # like the model preference, so it survives the idle/daily rotation; the
        # same store the text channels keep, and the picker's press switches it
        # through ``switch_channel_agent`` like theirs.
        self._agent_picks: ChannelAgentPicks[tuple[str, str]] = ChannelAgentPicks()
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
        await privacy_mode.hydrate_inbound(self.sessions, session_key)
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

    # dispatch/turn_engine.py
    _run_turn = _turn_engine._run_turn

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

    # dispatch/turn_queue.py
    _drain_queue = _turn_queue._drain_queue
    _pump_queue = _turn_queue._pump_queue
    _receipt_surface = _turn_queue._receipt_surface
    _enqueue_with_receipt = _turn_queue._enqueue_with_receipt
    _receipt_flip_locked = _turn_queue._receipt_flip_locked

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
    _agent_labels = staticmethod(_pickers._agent_labels)
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
        return self._agent_picks.resolve(route, self._configured_agent())

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
          * threadless private DM -> ``(CHAT_TYPE_DIRECT, str(user_id))`` --
            byte-for-byte the pre-forum identity, so a user who never turns on
            private-chat topics keeps the exact same DM key (and the General
            topic, which carries no ``message_thread_id``, stays on it too).
          * private-chat forum Topic -> ``(CHAT_TYPE_PRIVATE_TOPIC,
            "{chat_id}:{thread}")``. Telegram now supports forum topics inside a
            1:1 DM (Bot API ``Message.message_thread_id`` "for supergroups and
            private chats"); each topic is its own conversation, mirroring the
            supergroup case. It is still a 1:1 DM with one allow-listed user, so
            it stays on the ``direct`` side of the dashboard-link/host-listing
            token guards (see ``_is_private_route``), but it is per-topic
            isolated and never collapses under ``dm_scope=unified``.
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
        if thread:
            # A private chat carrying a message_thread_id is a direct-message
            # forum Topic: fold (chat_id, thread) so each topic is its own
            # session instead of sharing the one DM session.
            return CHAT_TYPE_PRIVATE_TOPIC, f"{chat_id}:{thread}"
        return CHAT_TYPE_DIRECT, str(user_id)

    @staticmethod
    def _is_private_route(route: tuple[str, str]) -> bool:
        """True when ``route`` is a 1:1 DM with the user -- the baseline direct
        DM OR a private-chat forum Topic.

        Both are the SAME person in the SAME private chat, so both pass the
        token-leak guards that gate reply AUDIENCE (``/kirocrew dashboard``,
        the host-wide listings in ``_require_direct_chat``): there is no group
        to read the reply. A supergroup forum Topic (``CHAT_TYPE_FORUM``) is
        readable by the whole supergroup, so it is NOT private here.

        This is deliberately distinct from the owner-DM session-control
        exemption (``session_control.owner_dm_refusal``), which stays
        ``direct`` + a single scope segment only: a private topic can hold a
        conversation but does not gain host-wide session-control authority (the
        fail-safe default), and that predicate is shared with Discord.
        """
        return route[0] in (CHAT_TYPE_DIRECT, CHAT_TYPE_PRIVATE_TOPIC)

    @staticmethod
    def _route_thread(route: tuple[str, str]) -> int | None:
        """The forum Topic id for a ``route``, or None for a threadless DM.

        Mirrors ``_route_key``'s ``comp`` encoding: a forum Topic route (group
        ``CHAT_TYPE_FORUM`` or private-chat ``CHAT_TYPE_PRIVATE_TOPIC``) carries
        ``"{chat_id}:{thread}"`` -> the Topic id; a threadless direct DM route
        -> None. An authorized supergroup turn always carries a Topic (General
        is denied at the gate), so the threadless-``comp`` -> None case is only
        the defensive fallback. Threads every dispatcher-originated send back
        into the SAME Topic the turn came from.
        """
        slot, comp = route
        if slot in (CHAT_TYPE_FORUM, CHAT_TYPE_PRIVATE_TOPIC) and ":" in comp:
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

    # dispatch/turn_engine.py
    _persist_turn = _turn_engine._persist_turn
    _maybe_notice = _turn_engine._maybe_notice

    # dispatch/commands.py
    _handle_compact = _commands._handle_compact
