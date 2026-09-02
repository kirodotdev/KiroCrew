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
"""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

from kiro_crew.acp.client import AcpError
from kiro_crew.agent_discovery import list_agents
from kiro_crew.config.loader import ACTIVATION_MENTION, ACTIVATION_OFF
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.history import is_incognito_transcript, mint_row_mid
from kiro_crew.hooks import TOOL_AUTO_APPROVE, TOOL_DENY
from kiro_crew.messaging import auto_title, privacy_mode
from kiro_crew.messaging.attachments import IngestLimits, append_attachment_context
from kiro_crew.messaging.attachments import cleanup as cleanup_attachments
from kiro_crew.messaging.commands import (
    YOLO_PHRASING_PLAIN,
    compact_unsupported_backend,
    compact_unsupported_reply,
    cron_command_reply,
    format_ttl,
    lists_host_state,
    parse_dashboard_ttl,
    run_yolo_command,
    spawn_task_reply,
    stop_running_turn,
    task_arg_reply,
)
from kiro_crew.messaging.dispatch import (
    build_auto_approve,
    build_directive_consumer,
    delivery_is_muted,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.link import (
    CHAT_TYPE_DIRECT,
    CHAT_TYPE_FORUM,
    ChannelLink,
    bind_origin_mirror,
    build_dm_session_key,
    rebind_conversation_location,
    release_conversation_location,
    seed_generation,
)
from kiro_crew.messaging.renderer import (
    SilentRenderer,
    display_safe,
    session_provenance_tag,
)
from kiro_crew.messaging.session_resume import SEARCH_FETCH_LIMIT, history_dashboard_key
from kiro_crew.messaging.session_trust import add_trusted_session, is_session_trusted
from kiro_crew.messaging.sessions_view import collect_recent_sessions_audited
from kiro_crew.messaging.transport import InboundMessage
from kiro_crew.messaging.upload_gate import uploads_restricted
from kiro_crew.platform.context import redact_via_context
from kiro_crew.safety_override import safety_override
from kiro_crew.security import redact, redact_local_paths
from kiro_crew.sel import sel
from kiro_crew.session_allocation import SessionClosingError
from kiro_crew.stats import Stats
from kiro_crew.telegram.attachments import process_telegram_attachments
from kiro_crew.telegram.commands import (
    ConversationState,
    build_help_text,
    is_bare_mid_turn_override,
    parse_command,
    parse_command_argument,
    parse_dashboard_argument,
    parse_mid_turn_override,
)
from kiro_crew.telegram.renderer import (
    TelegramApprovalDecider,
    TelegramRenderer,
    md_to_telegram_html_safe,
)
from kiro_crew.telegram.transport import (
    TELEGRAM_CAPABILITIES,
    TelegramInboundMessage,
    forum_gate_outcome,
)
from kiro_crew.voice_reply import synthesis_settings, synthesize_and_deliver

if TYPE_CHECKING:
    from kiro_crew.config.loader import KiroCrewConfig
    from kiro_crew.context import ContextBuilder
    from kiro_crew.cron import CronService
    from kiro_crew.history import ConversationLog
    from kiro_crew.session import SessionManager
    from kiro_crew.subagent import SubagentManager
    from kiro_crew.taskrunner import TaskRunner
    from kiro_crew.telegram.client import TelegramCallback, TelegramClient

from kiro_crew.messaging.queue_receipt import MAX_COLLAPSE as _MAX_COLLAPSE
from kiro_crew.messaging.queue_receipt import STEER_ACK_EMOJI as _STEER_ACK_EMOJI
from kiro_crew.messaging.queue_receipt import (
    ReceiptQueue,
    ReceiptSurface,
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

#: A legacy button carries no proof of which session authored its model-written
#: label, so applying it to the current session would be a cross-session injection.
_UNTAGGED_OPTIONS_REFUSAL = (
    "🔘 These buttons predate a session-safety update, so which conversation "
    "they belong to cannot be verified and your choice was NOT applied. "
    "Type it as a message instead."
)

#: A busy-path queue or steer retains only bare text, dropping the provenance
#: required to validate the choice when it later executes.
_BUSY_OPTIONS_REFUSAL = (
    "🔘 That conversation is busy with another turn, so your choice was NOT "
    "applied. Type it as a message once the turn finishes."
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

    Only a *permanent* :class:`AcpError` (``transient is False``) yields a
    reason: its message is already user-facing and actionable (e.g. names the
    models the account does include), and the generic "please try again"
    placeholder would be actively wrong for it. Transient and unclassified
    failures keep the retry wording, and any other exception type returns
    None — arbitrary internal errors must never leak into chat (CWE-209).

    The text is untrusted output: credentials/exfil URLs and local filesystem
    paths are redacted, newlines are collapsed, and the length is hard-capped.
    """
    if not isinstance(exc, AcpError) or exc.transient is not False:
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


# How long a /model picker stays pressable, and how many pickers are retained.
# Both are bounds on unbounded growth (one entry per press-less /model), not UX
# knobs: an expired or evicted picker answers "reopen /model" rather than acting
# on a stale list.
_MODEL_PICKER_TTL_SECS = 300.0
_MODEL_PICKER_MAX = 50
#: Buttons a picker shows. Telegram renders a one-per-row keyboard fine at this
#: size, and the list is the account's own model set, not a catalogue.
#: Shared by both pickers, like the TTL and the retention cap above: the lists
#: are this account's own models and this machine's own agent specs, not
#: catalogues, so one bound fits both.
_PICKER_LIMIT = 24

#: ``/sessions`` rows, matching the Slack surfaces' own default.
_SESSIONS_LIMIT = 10
#: Per-row caps, so one pathological title cannot consume the whole message.
_SESSION_TITLE_CHARS = 60
_SESSION_AGENT_CHARS = 24
_SESSION_QUERY_CHARS = 100
#: ``/title`` ceiling. The dashboard sidebar row truncates well before this; the
#: cap is here so a persisted transcript never carries an unbounded title.
_TITLE_MAX_CHARS = 80


@dataclass
class _Picker:
    """A posted keyboard, resolving a button index back to the value it names.

    One record type for ``/model`` and ``/agent``: both cap ``callback_data`` at
    64 bytes, both routinely carry ids longer than that, and both therefore send
    an INDEX into a retained table instead of the id itself.
    """

    route: tuple[str, str]
    created_at: float
    #: ``(value, label)`` in button order. A ``""`` value is the row that means
    #: "no explicit pick" — Auto for a model, the configured default for an agent.
    choices: tuple[tuple[str, str], ...]


#: Answers shorter than this are not spoken. Speaking "Done." spends a message
#: and a notification to say less than the text bubble already did, and Telegram's
#: rate limit is per chat. Slack applies the same floor.
_VOICE_MIN_CHARS = 50

#: Container -> mime for the synthesizers that ship. Only OGG/Opus can take the
#: native voice-note bubble (``sendVoice``); anything else goes as ``sendAudio``,
#: which the client decides from the mime we declare here.
_AUDIO_MIMES = {
    ".ogg": "audio/ogg",
    ".opus": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
}


def _audio_mime(path: str) -> str:
    """Mime for a synthesized audio file, by extension.

    The extension is trustworthy HERE and nowhere else: this file was written by
    our own synthesizer into a temp dir, not named by the model. An inbound
    attachment is sniffed from its leading bytes instead.
    """
    return _AUDIO_MIMES.get(os.path.splitext(path)[1].lower(), "application/octet-stream")


def _read_bytes(path: str) -> bytes:
    """Read a synthesized audio file. Blocking; callers hand it to a thread."""
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        logger.warning("telegram: could not read synthesized audio", exc_info=True)
        return b""


#: Cache of compiled @handle matchers. One handle per process in practice (the
#: bot's own), so this is a single entry; keyed anyway because tests set several.
_MENTION_RES: dict[str, "re.Pattern[str]"] = {}


def _mention_re(handle: str) -> "re.Pattern[str]":
    """A case-insensitive matcher for ``@handle`` as a whole token.

    ``(?![A-Za-z0-9_])`` is the whole point: without it `@kirocrewbot` matches
    inside `@kirocrewbot2`, so a message aimed at another bot in the same Topic
    activates this one. The leading `(?<![A-Za-z0-9_@])` stops a match inside an
    email-like or doubled-@ token for the same reason.
    """
    cached = _MENTION_RES.get(handle)
    if cached is None:
        cached = re.compile(
            r"(?<![A-Za-z0-9_@])@" + re.escape(handle) + r"(?![A-Za-z0-9_])",
            re.IGNORECASE,
        )
        _MENTION_RES[handle] = cached
    return cached


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
        self.dashboard_state: Any = None
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
        # The mid-turn queue receipt: one in-place "queued" bubble per session,
        # plus the lock that serializes check-then-send-then-store against the
        # end-of-turn drain. Both now live in messaging/queue_receipt.py so
        # Telegram and Discord cannot drift on the lock discipline.
        self._queue = ReceiptQueue()
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
        # Inbound channels-governance gate (off-loop) — recheck per message so a
        # host-profile deny added after connect stops dispatch without a restart
        # (the startup gate only blocks CONNECTING). Silently drop on deny.
        if not await channel_inbound_permitted("telegram"):
            logger.info("telegram inbound dropped: denied by channels governance policy")
            return
        # Counted here, matching where Slack counts it: an inbound message the
        # governance gate refused never happened as far as the operator's own
        # traffic figures go, but everything past this point did. Without these
        # three counters `/status` — a command this channel offers — reports
        # "msgs 0 (ok 0 / fail 0)" forever on a Telegram-only install, and
        # Stats.daily_report says "no messages" for a bot that has been answering
        # all day.
        Stats().inc_message_received()
        # Forum activation gate: whether the bot should ANSWER here, as opposed to
        # whether it MAY (that is the transport's fail-closed forum authZ, already
        # passed). Slack has had this per channel since before the transport path
        # existed; without it an allow-listed Topic cannot host a conversation
        # between humans, because every message starts a turn.
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
        user_id = int(msg.user_id)
        chat_id = int(msg.conversation_id)
        text = msg.text
        # Route to the conversation identity. DM (private) -> (direct, user_id),
        # reproducing the pre-forum key EXACTLY; an authorized supergroup forum
        # message always carries a Topic thread -> (forum, "chat:thread"). A
        # threadless General message never reaches here (the forum gate in
        # transport.receive / on_callback denies it); the threadless (forum,
        # "chat") branch below is defensive dead code, not a served path.
        # ``thread`` is the raw Topic id passed to the renderer so its outbound
        # messages thread into the Topic. Everything downstream (session key,
        # generation counter, awaiting flag) keys on ``route`` so /new, idle
        # rotation and /compact are per-topic, not per-user.
        route = self._route_key(
            chat_type=getattr(msg, "chat_type", "private"),
            user_id=user_id,
            chat_id=chat_id,
            thread=getattr(msg, "thread_id", None),
        )
        thread = getattr(msg, "thread_id", None)
        # The Topic id (int) used to thread EVERY dispatcher-originated reply for
        # this turn back into the user's Topic (command confirmations, receipts,
        # the soft-threshold notice); None only for a DM (an authorized forum
        # turn always carries a Topic — General is denied at the gate).
        reply_thread = self._route_thread(route)

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
        if cmd == "new":
            self._conv.bump_gen(route)
            await self._reply(chat_id, "✅ New conversation started.", thread=reply_thread)
            return
        if cmd == "compact":
            self._conv.clear_awaiting(route)
            await self._handle_compact(route, chat_id)
            return
        if cmd == "link":
            await self._handle_link(route, chat_id)
            return
        if cmd == "unlink":
            await self._handle_unlink(route, chat_id)
            return
        if cmd == "help":
            await self._reply(chat_id, _HELP_TEXT, thread=reply_thread)
            return
        if cmd == "stop":
            await self._handle_stop(route, chat_id)
            return
        if cmd == "model":
            await self._handle_model(route, chat_id, parse_command_argument(text))
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
            rest = parse_command_argument(text)
            if not rest:
                applied = await privacy_mode.apply_mode(
                    cmd,
                    # Rotation FIRST: a bare modifier returns before the turn path
                    # would have rotated, so keying on the un-rotated generation
                    # protects a session the next message abandons.
                    self._rotated_session_key(route),
                    source="telegram",
                    caller=str(user_id),
                    sessions=self.sessions,
                    notify=lambda note: self._notify(chat_id, note, thread=reply_thread),
                )
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
            await self._handle_sessions(
                chat_id,
                parse_command_argument(text),
                caller=str(user_id),
                thread=reply_thread,
            )
            return
        if cmd == "title":
            await self._handle_title(route, chat_id, parse_command_argument(text))
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
                await self._handle_spawn(route, chat_id, arg, thread=reply_thread)
            else:
                await self._handle_task(chat_id, arg, route=route, thread=reply_thread)
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

        # ── Mid-turn concurrency: check the CURRENT-generation key for an
        # in-flight turn BEFORE any idle/daily rotation. Rotating first could
        # mint a new key and miss the running turn, letting a second concurrent
        # turn bypass steer/queue. Surface the message (steer or queue) instead
        # of a silent block.
        session_key = self._session_key(route)
        if origin_tag and session_provenance_tag(session_key) != origin_tag:
            # The button belongs to a session this route no longer targets. This
            # gate precedes the busy read so a stale press neither queues nor
            # reveals whether the replacement conversation is running a turn.
            await self._reply(chat_id, _STALE_OPTIONS_REFUSAL, thread=reply_thread)
            return
        if self.sessions.is_busy(session_key):
            if origin_tag:
                # Queue and steer paths store only text. Letting a tagged choice
                # enter either path would replay it later with no tag to validate.
                await self._reply(chat_id, _BUSY_OPTIONS_REFUSAL, thread=reply_thread)
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

        session_key = self._rotated_session_key(route)
        if origin_tag and session_provenance_tag(session_key) != origin_tag:
            # Idle/daily rotation can change the native key after the pre-busy
            # check. Revalidate the final key so the choice cannot cross it.
            await self._reply(chat_id, _STALE_OPTIONS_REFUSAL, thread=reply_thread)
            return
        # Restore a privacy mode set before a restart. The in-memory trackers are
        # empty on a cold process, so without this a session the operator marked
        # incognito yesterday reads as unrestricted today and this turn's transcript
        # is written. The durable flag lives on the session map; this is the only
        # place that reads it back onto the final key.
        privacy_mode.hydrate(self.sessions, session_key)
        if privacy_request:
            # Applied AFTER the rotation, so it lands on the key this turn actually
            # runs under. Notified through the same adapter the bare-command branch
            # uses, so a deferred modifier confirms exactly like an immediate one.
            await privacy_mode.apply_mode(
                privacy_request,
                session_key,
                source="telegram",
                caller=str(user_id),
                sessions=self.sessions,
                notify=lambda note: self._notify(chat_id, note, thread=reply_thread),
            )
        channel_id = f"telegram:{user_id}"
        # Resolve the kiro-cli agent: this route's /agent pick wins, else an
        # explicit override, else the configured default, else the canonical
        # "kirocrew" agent — so the session loads kirocrew-core (spawn_run)
        # instead of kiro-cli's bare built-in default. Mirrors
        # slack/transport_dispatch.py.
        agent = self._resolve_agent(route)

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
            message_thread_id=int(thread) if thread else None,
            show_thinking=self.cfg.telegram.show_thinking,
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
        failure_reason: str | None = None
        attachment_temp_paths: list[str] = []
        try:
            # Ack placeholder first (before the potentially slow cold-start);
            # on_turn_start is idempotent so the driver's later call no-ops.
            # Skipped when muted, as in the Discord twin.
            if not muted:
                await renderer.on_turn_start()
            provider, is_new, resumed = await self.sessions.get_or_create(
                session_key,
                agent=agent,
                channel_id=channel_id,
                # "" is the Auto row's stored value; collapse it to None so Auto
                # means "as if never picked". get_or_create gates its own model
                # resolution on `model is None`, so passing "" would skip that
                # and land on the provider factory's narrower fallback instead.
                model=self._model_pref.get(route) or None,
            )
            _acquired = True
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
            if is_new:
                await self.sessions.set_channel(session_key, channel_id)
            # Bind this chat as the session's outbound mirror so a turn the user
            # later takes from the dashboard is delivered back here. Slack gets
            # this from its own per-turn thread binding; Telegram had it only
            # behind an explicit /link. Called ON the loop, like every other
            # session-map mutation: `_MAP_LOCK` is what orders it against a
            # concurrent mutation, and the write is bounded — one whole-map
            # rewrite, on a conversation's first turn only.
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
            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                self.ctx_builder.build_message,
                text,
                is_new,
                session_key,
                channel_id=channel_id,
                agent=agent,
                resumed=resumed,
                runtime_source="telegram",
                # Temporary mode reads NO memory, which is the half the transcript
                # gate cannot cover: refusing to WRITE still leaves yesterday's
                # memories and lessons in today's prompt. Incognito deliberately
                # still reads — that is the documented difference between the two.
                blocks_reads=privacy_mode.is_temporary(session_key),
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
                closing_gate=lambda: self.sessions.begin_turn(session_key),
            )
            accumulated = await driver.run(full_message)

            # ── Post-turn bookkeeping (each guarded so a failure here can't
            # fall through to the except and re-record the successful turn). ──
            self.sessions.record_success(session_key)
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
            # Auto-title, fire-and-forget. Without it a conversation's name is
            # frozen at the first forty characters of the first message forever —
            # the deterministic fallback ``_persist_turn`` writes — and the only
            # correction is a manual /title. Claim-and-spawn, never awaited: the
            # answer has already been delivered, so the user waits on nothing.
            #
            # Requires ``accumulated``: a turn that produced no text has nothing to
            # name, and titling it would spend a background turn to be told SKIP.
            # Skipped for a restricted session, which persists nothing to title.
            # Isolated like every other bookkeeping step here, so failing even to
            # SPAWN the task never re-records this successful turn as a failure.
            try:
                if (
                    accumulated
                    and not privacy_mode.is_restricted(session_key)
                    and auto_title.try_claim(session_key)
                ):
                    _title_task = asyncio.create_task(
                        auto_title.maybe_auto_title(
                            self.sessions,
                            self.conv_log,
                            session_key,
                            text,
                            accumulated,
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
            try:
                await asyncio.to_thread(
                    self._persist_turn, session_key, text, accumulated, is_new, agent
                )
            except Exception:
                logger.warning(
                    "Telegram: persist_turn failed session=%s", session_key, exc_info=True
                )
            if is_new:
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
        except Exception as exc:
            logger.exception("Telegram transport_dispatch: error handling message")
            # Permanent, user-actionable failures (e.g. model entitlement)
            # surface their own bounded reason instead of the misleading
            # generic retry text; everything else stays generic (None).
            failure_reason = _user_safe_failure_reason(exc)
            if _acquired:
                await self.sessions.record_failure(session_key)
                Stats().inc_message_failed()
        finally:
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
        if drain:
            await self._drain_queue(
                session_key,
                user_id,
                chat_id,
                chat_type=getattr(msg, "chat_type", "private"),
                thread=thread,
            )

    async def _handle_busy(
        self,
        session_key: str,
        msg: InboundMessage,
        text: str,
        override_mode: str | None,
        *,
        thread: int | None = None,
        privacy_request: str = "",
        caller: str = "system",
    ) -> None:
        """A message arrived mid-turn: steer the running turn or queue for after
        it. ``text`` is the message with any ``/queue``|``/steer`` directive
        stripped; ``override_mode`` ('queue' | 'steer' | None) forces the path for
        THIS message, overriding the global ``queue_mode``.

        *privacy_request* is a modifier the caller stripped off *text*, and the two
        branches owe it different things because they run the request under different
        keys. A STEERED message folds into the turn already running on
        ``session_key``, so the mark belongs on that key now, before the steer, or
        the running turn's transcript is written before anything marks it. A QUEUED
        message runs later under whatever key the drained turn resolves, so the
        request rides ALONG with it and is applied there. Applying it here for a
        queued message would mark a key the drain may have rotated past, which is the
        failure the deferred apply exists to avoid.
        """
        assert self.client is not None
        chat_id = int(msg.conversation_id)
        mode = override_mode or self.cfg.messaging.queue_mode
        # An attachment-bearing message can never take the steer path: ``steer``
        # forwards TEXT ONLY, so steering a photo/document message would deliver
        # its caption and silently drop every file. Such a message always goes to
        # the queue path below, which carries ``attachments`` through the drain.
        # Mirrors discord/transport_dispatch.py's identical gate -- Telegram was
        # missing it, and album buffering makes it far more reachable: a follow-up
        # typed during the debounce window starts a turn, so the album's own flush
        # arrives mid-turn and would have been steered as caption-only.
        if mode != "queue" and not msg.attachments:
            provider = self.sessions.get_provider(session_key)
            steer = getattr(provider, "steer", None)
            # Only steer when a turn is GENUINELY in flight. ``is_busy`` stays
            # True through post-turn bookkeeping (record_success / _persist_turn
            # / _maybe_notice / SEL audit -- all await points), so without this
            # guard a steer could reach kiro-cli for a prompt that already ended
            # -> silently swallowed (no fresh turn, no queue entry), and the
            # steer-ack reaction would land on a message whose turn already
            # finished. When no live turn, fall through to the queue/handle path
            # below (mirrors the queue path's ``force=False`` fallback), so the
            # message is re-run or queued instead of lost.
            has_active = getattr(provider, "has_active_turn", None)
            live = has_active is None or bool(has_active())
            steered = bool(
                live
                and getattr(provider, "supports_steer", False)
                and steer is not None
                and await steer(text)
            )
            if steered:
                if privacy_request:
                    # The turn this folded into is running on THIS key, and it writes
                    # its transcript when it finishes. Marked after the steer landed,
                    # so a refused steer does not leave the session restricted for a
                    # message that fell through to the queue path instead.
                    await privacy_mode.apply_mode(
                        privacy_request,
                        session_key,
                        source="telegram",
                        caller=caller,
                        sessions=self.sessions,
                        notify=lambda note: self._notify(chat_id, note, thread=thread),
                    )
                # Record the user's OWN words on the running turn's renderer so
                # it can render an inline "↪️ steered: <text>" chip (never the
                # redacted backend echo). Best-effort: no active renderer -> skip.
                r = self._active_renderers.get(session_key)
                if r is not None:
                    r.note_steer(text)
                # Instant, no-extra-bubble ack: react to the user's steer message
                # so a mid-turn steer isn't silent while it waits for the next
                # generation boundary. The steered reply lands at the end of the
                # turn's output (no pre/post split -- that retroactive slice of a
                # single stream leaked fragments across the cut). Best-effort --
                # reactions need Bot API 7.0+.
                steer_mid = getattr(msg, "message_id", 0)
                if steer_mid:
                    try:
                        await self.client.set_message_reaction(chat_id, steer_mid, _STEER_ACK_EMOJI)
                    except Exception:
                        logger.debug("telegram: steer ack reaction failed", exc_info=True)
                return
        # queue mode (or /queue override, or steer unavailable). Enqueue + receipt
        # happen atomically under ``self._queue.lock`` (see ``_enqueue_with_receipt``)
        # so the end-of-turn drain -- which takes the same lock to dequeue + flip
        # -- cannot interleave between the enqueue and the receipt and orphan a
        # bubble. If the turn finished in the window the message is not queued, so
        # we run it now (re-entering handle_message, which re-strips the directive
        # and runs it as a fresh turn) instead of stranding it.
        if not await self._enqueue_with_receipt(
            session_key,
            chat_id,
            text,
            thread=thread,
            attachments=list(msg.attachments) if msg.attachments else None,
            privacy_request=privacy_request,
        ):
            # Not queued, so re-run it now. The ORIGINAL msg, whose text still
            # carries the modifier, so command parsing re-derives the request rather
            # than this path having to re-thread it.
            await self.handle_message(msg)

    async def _drain_queue(
        self,
        session_key: str,
        user_id: int,
        chat_id: int,
        *,
        chat_type: str = "private",
        thread: str | None = None,
    ) -> None:
        """Collapse every message queued during the just-finished turn into ONE
        combined turn (order preserved, blank-line joined) and answer them
        together, rather than replaying each as a separate turn.

        The dequeue + receipt flip run together under ``self._queue.lock`` so a
        concurrent mid-turn ``_enqueue_with_receipt`` (which takes the same lock)
        cannot interleave and leave an orphaned receipt. The combined turn itself
        runs OUTSIDE the lock -- messages that arrive during it open a fresh
        receipt and drain after the next turn. Only the queued text is replayed
        (matching what ``enqueue`` persists for DM channels: text only).
        """
        # Iterate rather than recurse: one burst can span multiple
        # attachment-capped turns, and a message deferred by the cap must drain
        # in THIS pump rather than waiting for unrelated future user input.
        # Mirrors the Discord drain.
        while True:
            texts: list[str] = []
            all_attachments: list[Any] = []
            remainder: list[tuple[str, str, dict]] = []
            privacy_requests: list[str] = []
            defer_rest = False
            async with self._queue.lock:
                # Drain the ENTIRE queue under the lock, then split: the first
                # _MAX_COLLAPSE messages collapse into this turn; the rest are
                # re-enqueued IN ORIGINAL ORDER (the queue is now empty, so
                # re-adding preserves FIFO) to drain after the next turn. This
                # bounds the combined prompt without dropping or reordering surplus.
                while True:
                    item = self.sessions.dequeue(session_key)
                    if item is None:
                        break
                    item_attachments = list(item[2].get("attachments") or [])
                    # Never collapse past the shared ingestion cap: the extra files
                    # would be dropped inside ingest_attachments with the user given
                    # no indication, so defer instead. Mirrors the Discord drain.
                    exceeds_attachment_cap = bool(
                        texts
                        and item_attachments
                        and len(all_attachments) + len(item_attachments)
                        > _MAX_COLLAPSED_ATTACHMENTS
                    )
                    if not defer_rest and len(texts) < _MAX_COLLAPSE and not exceeds_attachment_cap:
                        texts.append(item[1])
                        all_attachments.extend(item_attachments)
                        requested = item[2].get("privacy_request") or ""
                        if isinstance(requested, str) and requested:
                            privacy_requests.append(requested)
                    else:
                        # Once one message no longer fits, defer it AND everything
                        # behind it, so queue order stays exact.
                        defer_rest = True
                        remainder.append(item)
                for _ts, rtext, rkw in remainder:
                    self.sessions.enqueue(
                        session_key,
                        str(time.time()),
                        rtext,
                        force=True,
                        attachments=list(rkw.get("attachments") or []),
                        # Re-enqueued verbatim, modifier included: a deferred message
                        # drains in a LATER iteration of this pump, and dropping the
                        # request here would unprotect exactly the messages the
                        # collapse cap pushed back.
                        privacy_request=rkw.get("privacy_request") or "",
                    )
                if texts:
                    await self._receipt_flip_locked(session_key, chat_id, texts, len(remainder))
            if not texts:
                return
            if remainder:
                logger.debug(
                    "telegram: drain deferred %d message(s) for %s to respect the "
                    "collapse cap (%d) / attachment cap (%d); they drain in the "
                    "next iteration of this pump, in order",
                    len(remainder),
                    session_key,
                    _MAX_COLLAPSE,
                    _MAX_COLLAPSED_ATTACHMENTS,
                )
            combined = "\n\n".join(texts)
            await self.handle_message(
                TelegramInboundMessage(
                    channel_type="telegram",
                    user_id=str(user_id),
                    conversation_id=str(chat_id),
                    text=combined,
                    # Carry the turn's ORIGINAL route so the drained turn resolves to
                    # the SAME forum session key -- a plain DM-shaped InboundMessage
                    # would drain a queued forum message under the DM key instead.
                    thread_id=thread,
                    chat_type=chat_type,
                    attachments=all_attachments,
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
                # in the same burst be silently downgraded to whatever led it.
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

            async def send_receipt(self, body: str) -> Any | None:
                return await reply(chat_id, body, thread=thread)

            async def edit_receipt(self, msg_id: Any, body: str) -> None:
                await client.edit_message(chat_id, msg_id, body)

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
        """
        assert self.client is not None
        async with self._queue.lock:
            if not self.sessions.enqueue(
                session_key,
                str(time.time()),
                text,
                force=False,
                attachments=list(attachments or []),
                # Rides WITH the message, for the same reason its attachments do:
                # the drain re-enters with `interpret_commands=False` on text the
                # modifier was already stripped from, so a request left behind here
                # is one no later parse can recover.
                privacy_request=privacy_request,
            ):
                return False
            await self._queue.create_or_grow_locked(
                session_key, self._receipt_surface(chat_id, thread), text
            )
            return True

    async def _receipt_flip_locked(
        self, session_key: str, chat_id: int, answered: list[str], deferred: int = 0
    ) -> None:
        """Flip the receipt to a durable "▶️ Now answering" record and drop the
        live entry so the next mid-turn burst opens a fresh receipt. Caller MUST
        hold ``self._queue.lock`` (the drain holds it across dequeue + flip).

        ``answered`` is the subset actually answered by this turn (capped at
        ``_MAX_COLLAPSE``); the count reflects it -- not the full queued list --
        so a >cap burst doesn't overstate what this turn answers. ``deferred``
        (>0 only past the cap) is noted so the remainder isn't silently implied.
        """
        assert self.client is not None
        await self._queue.flip_answering_locked(
            session_key, self._receipt_surface(chat_id, None), answered, deferred
        )

    async def _handle_dashboard(
        self, route: tuple[str, str], chat_id: int, text: str, user_id: int
    ) -> None:
        """Generate and send a presigned dashboard login link.

        Mirrors the Slack ``/kirocrew dashboard`` implementation: calls
        ``generate_token`` directly (never via shell) and builds the URL from
        the ``dashboard.url`` config (``KIROCREW_PORT`` overrides the port,
        matching every other link producer).

        DM-only: a presigned link posted into a forum Topic would hand a
        dashboard login to every member of the supergroup, so group requests
        are refused with a pointer to DM — the same token-leak policy as
        Slack's always-DM delivery.
        """
        assert self.client is not None
        from kiro_crew.dashboard.token_auth import (
            MAX_SESSION_TTL_SECS,
            generate_token,
            parse_duration,
        )
        from kiro_crew.dashboard.urls import dashboard_origin, parse_dashboard_url

        thread = self._route_thread(route)
        if route[0] != CHAT_TYPE_DIRECT:
            await self._reply(
                chat_id,
                "🔒 Dashboard links are only sent in a direct message — "
                "DM me `/kirocrew dashboard`.",
                thread=thread,
            )
            return
        ttl_secs = min(
            parse_dashboard_ttl(parse_dashboard_argument(text), parse_duration=parse_duration),
            MAX_SESSION_TTL_SECS,
        )
        try:
            token = generate_token(str(user_id), ttl_seconds=ttl_secs)
            origin = dashboard_origin(self.cfg.dashboard.url)
            if not origin:
                # No configured dashboard.url: fall back to the local port
                # (parse_dashboard_url applies the KIROCREW_PORT override).
                _, port = parse_dashboard_url(self.cfg.dashboard.url)
                origin = f"http://localhost:{port}"
            url = f"{origin}/?token={token}"
            ttl_display = format_ttl(ttl_secs)
            # Credential issuance MUST be audited (backend-security-controls):
            # mirrors slack.dashboard_token and telegram.yolo_mode above.
            sel().log_api_access(
                caller=str(user_id),
                operation="telegram.dashboard_token",
                outcome="ok",
                source="telegram",
                resources=f"ttl={ttl_secs}",
            )
            await self._reply(
                chat_id,
                f"🔗 Dashboard link (valid {ttl_display}):\n{url}",
                thread=thread,
            )
        except Exception as exc:
            logger.warning("telegram /kirocrew dashboard: token generation failed", exc_info=True)
            try:
                sel().log_api_access(
                    caller=str(user_id),
                    operation="telegram.dashboard_token",
                    outcome="error",
                    source="telegram",
                    resources=f"ttl={ttl_secs}",
                )
            except Exception:
                # The audit trail must never turn a user-facing failure reply
                # into a crash; the warning above already captured the error.
                pass
            await self._reply(
                chat_id,
                f"⚠️ Could not generate dashboard link: {exc}",
                thread=thread,
            )

    def _voice_enabled(self, route: tuple[str, str]) -> bool:
        """Whether this conversation speaks its answers.

        Per-route ``/voice`` toggle first, then the configured default. Absent
        rather than pre-seeded so a later change to ``telegram.voice_replies``
        reaches every conversation the operator has not overridden.
        """
        pref = self._voice_pref.get(route)
        if pref is not None:
            return pref
        return bool(getattr(self.cfg.telegram, "voice_replies", False))

    async def _handle_voice(
        self, route: tuple[str, str], chat_id: int, arg: str, thread: int | None
    ) -> None:
        """``/voice on|off`` — speak this conversation's answers, or stop.

        A bare ``/voice`` reports the current state rather than toggling: a
        toggle whose direction depends on state the user cannot see is how you end
        up turning voice ON in a room where you wanted it off.
        """
        want = arg.strip().lower()
        if want in ("on", "off"):
            self._voice_pref[route] = want == "on"
            state = "on" if want == "on" else "off"
            await self._reply(chat_id, f"🔊 Voice replies {state}.", thread=thread)
            return
        now = "on" if self._voice_enabled(route) else "off"
        await self._reply(
            chat_id,
            f"🔊 Voice replies are *{now}*. Use `/voice on` or `/voice off`.",
            thread=thread,
        )

    async def _speak_reply(
        self, route: tuple[str, str], chat_id: int, text: str, thread: int | None
    ) -> None:
        """Synthesize *text* and send it as a voice/audio message. Never raises.

        Runs AFTER the text answer has landed, not instead of it: TTS depends on a
        local binary or a paid service, and an answer that only exists as audio is
        lost whenever either is unavailable. Sent silently, since the text reply
        already notified.

        Short answers are skipped — the same floor Slack applies. Speaking "Done."
        spends a message and a notification to say less than the text already did.

        Every failure is swallowed and logged: this is an enhancement on a turn
        that has already succeeded, so a TTS problem must not surface as a failed
        turn or re-post anything.
        """
        if len(text) < _VOICE_MIN_CHARS or self.client is None:
            return
        # Through the shared display sink, like every other outbound Telegram text.
        # This leg bypasses the renderer, which is where a turn normally gets that
        # floor, and the driver's pass is BYTE-level: it sees `AKIA**IOSFODNN7...**`
        # as broken because the `**` sits inside the key. A synthesizer reads the
        # characters, not the markup, so the credential the byte pass missed would be
        # SPOKEN, and audio is the one egress a reader cannot un-see. Length is
        # checked first so a short answer costs no scan. Off-loop, because this is a
        # full credential/exfil pass over a whole answer.
        text = await asyncio.to_thread(display_safe, text)
        raw = getattr(self.cfg, "raw", {}) or {}
        section = raw.get("voice_reply") if isinstance(raw, dict) else None
        settings = synthesis_settings(section if isinstance(section, dict) else None)

        async def _deliver(path: str) -> bool:
            data = await asyncio.to_thread(_read_bytes, path)
            if not data:
                return False
            mid = await self.client.send_voice(  # type: ignore[union-attr]
                chat_id,
                data,
                filename=os.path.basename(path) or "reply.wav",
                mime=_audio_mime(path),
                message_thread_id=thread,
            )
            return mid is not None

        try:
            spoken = await synthesize_and_deliver(_deliver, text, **settings)
        except Exception:
            logger.warning("telegram: voice reply failed for %s", route, exc_info=True)
            return
        if not spoken:
            logger.info("telegram: voice reply produced no audio for %s", route)

    async def _handle_stop(self, route: tuple[str, str], chat_id: int) -> None:
        """Hard cancel: abort the in-flight turn and clear everything.

        The cooperative-cancel contract, the lock ordering across
        ``clear_queue`` + the receipt finalize, and both replies live in
        :func:`~kiro_crew.messaging.commands.stop_running_turn`; this supplies
        Telegram's address. The receipt surface is built with no ``thread``
        because ``editMessageText`` is not threaded -- the message id already
        identifies the message within its Topic -- while the reply itself must
        land back in the originating Topic.
        """
        assert self.client is not None
        reply = await stop_running_turn(
            self.sessions,
            self._session_key(route),
            queue=self._queue,
            surface=self._receipt_surface(chat_id, None),
        )
        await self._reply(chat_id, reply, thread=self._route_thread(route))

    # ── /yolo (global auto-approve grant) ──────────────────────────────────

    async def _handle_yolo(
        self, chat_id: int, arg: str, user_id: int, *, thread: int | None = None
    ) -> None:
        """Report or change the global auto-approve grant.

        The ladder, its replies, the off-loop mutators and the SEL row live in
        :func:`~kiro_crew.messaging.commands.run_yolo_command`. Reachable only by
        an allow-listed Telegram user, because ``transport.receive`` is
        deny-by-default and owner-only before dispatch ever runs, which is why
        the user id is trustworthy as the audited caller.
        """
        reply = await run_yolo_command(
            arg,
            source="telegram",
            caller=str(user_id),
            phrasing=YOLO_PHRASING_PLAIN,
        )
        await self._reply(chat_id, reply, thread=thread)

    # ── /model (inline-button model picker) ────────────────────────────────

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
        except Exception:  # pragma: no cover - defensive
            logger.warning("telegram /model: available_models failed", exc_info=True)
            return tuple(rows)
        for entry in entries:
            model_id = str(entry.get("modelId") or "").strip()
            # "auto" is already offered as the first row; listing it twice would
            # give the same choice two buttons.
            if not model_id or model_id == "auto":
                continue
            rows.append((model_id, str(entry.get("name") or model_id)))
        return tuple(rows[:_PICKER_LIMIT])

    @staticmethod
    def _prune_pickers(table: dict[str, _Picker], now: float) -> None:
        """Drop expired pickers, then the oldest ones past the retention cap.

        Both bounds exist because every press-less picker leaves an entry behind;
        an expired or evicted one answers "reopen the command" rather than acting
        on a stale list.
        """
        for token, picker in list(table.items()):
            if now - picker.created_at > _MODEL_PICKER_TTL_SECS:
                table.pop(token, None)
        while len(table) > _MODEL_PICKER_MAX:
            table.pop(min(table, key=lambda t: table[t].created_at), None)

    async def _consume_picker(
        self,
        cb: "TelegramCallback",
        data: str,
        table: dict[str, _Picker],
        *,
        noun: str,
        command: str,
    ) -> tuple[_Picker, str, str] | None:
        """Resolve a picker press to ``(picker, value, label)``, or None on a miss.

        Consumes the picker BEFORE the caller applies it: the apply takes a
        round-trip, and a second press in that window would otherwise apply twice.
        A miss covers expired, evicted and already-consumed alike — deliberately
        one wording, because "expired" would be wrong for the picker a double-press
        merely used, which is the case a user actually hits.
        """
        assert self.client is not None
        token = f"{cb.chat_id}:{cb.message_id}"
        picker = table.get(token)
        expired = picker is not None and (time.time() - picker.created_at > _MODEL_PICKER_TTL_SECS)
        try:
            index = int(data.partition(":")[2])
        except ValueError:
            index = -1
        if picker is None or expired or not (0 <= index < len(picker.choices)):
            table.pop(token, None)
            await self.client.edit_message(
                cb.chat_id,
                cb.message_id,
                f"⌛ This {noun} list is no longer active — send {command} again.",
                reply_markup={"inline_keyboard": []},
            )
            return None
        table.pop(token, None)
        value, label = picker.choices[index]
        return picker, value, label

    async def _handle_model(self, route: tuple[str, str], chat_id: int, arg: str) -> None:
        """Post the model keyboard (or report the current pick for a bare arg).

        Deliberately button-only: a free-text model id means guessing at names
        the user has no way to enumerate, and a typo lands as a rejected
        ``set_model`` mid-conversation. Any argument is treated as "show me the
        list" rather than parsed.
        """
        assert self.client is not None
        session_key = self._session_key(route)
        thread = self._route_thread(route)
        choices = self._model_choices(session_key)
        if len(choices) <= 1:
            await self._reply(
                chat_id,
                "No model list available yet — send a message first, then /model.",
                thread=thread,
            )
            return

        current = self._model_pref.get(route, "")
        current_label = next(
            (label for mid, label in choices if mid == current),
            current or "Auto",
        )
        header = f"Current model: {current_label}\nPick one:"
        if arg.strip():
            # An argument is not an id to apply — say so once, then show the
            # list anyway so the message is still a step forward.
            header = f"/model takes no argument — pick from the list.\n\n{header}"
        keyboard = [
            [{"text": f"{'• ' if mid == current else ''}{label}", "callback_data": f"m:{index}"}]
            for index, (mid, label) in enumerate(choices)
        ]
        message_id = await self._reply(
            chat_id,
            header,
            thread=thread,
            reply_markup={"inline_keyboard": keyboard},
        )
        if message_id is None:
            return
        now = time.time()
        self._prune_pickers(self._model_pickers, now)
        self._model_pickers[f"{chat_id}:{message_id}"] = _Picker(
            route=route, created_at=now, choices=choices
        )

    async def _apply_model(self, route: tuple[str, str], model_id: str) -> str:
        """Record *model_id* for *route* and push it to the live session.

        *model_id* comes verbatim from the session's advertised list, so it is
        already the id this backend accepts — no canonical translation, which
        would differ per backend and could mangle an id that was correct.

        The preference is stored unconditionally so it reaches the NEXT session
        even when there is nothing live to switch (the common case right after
        ``/new``). When a session does exist, the switch is attempted in place —
        ``session/set_model`` carries the conversation across — and the semaphore
        is taken atomically so the switch cannot interleave JSON-RPC with a turn
        on the same stdio channel.

        Returns the user-facing outcome line.
        """
        label = model_id or "Auto"
        self._model_pref[route] = model_id
        session_key = self._session_key(route)
        live = self.sessions.has_session(session_key)
        # Two different promises, because the preference reaches a session only
        # at creation: ``get_or_create`` returns a reused session from its fast
        # path before it consults ``model=``. With nothing live the next message
        # starts the session, so it genuinely lands then; with a session already
        # up, only a fresh conversation picks it up.
        deferred = f"✅ Model set to {label} — it applies to your next message."
        next_new = (
            f"✅ Model set to {label} — this conversation keeps its current "
            f"model; the switch applies to your next one (/new)."
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
                f"your next one (/new)."
            )
        try:
            provider = self.sessions.get_provider(session_key)
            set_model = getattr(getattr(provider, "client", None), "set_model", None)
            if set_model is None:
                return next_new
            await set_model(model_id)
        except Exception as exc:
            logger.warning(
                "telegram /model: live set_model failed for %s: %s",
                session_key,
                type(exc).__name__,
                exc_info=True,
            )
            # The stored preference still stands, so the next session gets it —
            # but do not claim the running conversation switched when it did not.
            return (
                f"⚠️ Couldn't switch this conversation to {label} "
                f"({type(exc).__name__}) — it applies to your next "
                f"conversation (/new)."
            )
        finally:
            self.sessions.release(session_key)
        return f"✅ Now using {label}."

    def _addresses_this_bot(self, msg: InboundMessage) -> bool:
        """Whether *msg* addresses this bot, by @handle or by replying to it.

        Two routes, because Telegram users use both and a gate that recognised only
        the first would look broken to anyone who answers a bot by long-pressing its
        message:

        * the bot's own ``@handle`` appears in the text, case-insensitively. Only
          THIS bot's handle counts — a command aimed at another bot in the same
          Topic is not ours, the same reasoning ``_strip_bot_mention`` already
          applies to the ``/cmd@Other`` suffix.
        * the message replies to one sent by this bot, matched on ``bot_id``.
          ``is_bot`` on the replied-to sender is not enough: several bots can share
          a Topic.

        Both inputs are unresolved until ``getMe`` lands at startup
        (``bot_username`` empty, ``bot_id`` zero), which makes this answer False
        rather than True — the gate then holds until the identity is known instead of
        opening on a value it does not have yet.
        """
        if self.bot_id and getattr(msg, "reply_to_user_id", 0) == self.bot_id:
            return True
        handle = self.bot_username.strip().lstrip("@")
        if not handle:
            return False
        # Telegram's OWN classification, not a text scan. It marks a handle inside a
        # URL as a `url`/`text_link` entity rather than a `mention`, and a scan
        # cannot tell the two apart: `https://host/@thebot/x` satisfies any
        # `@handle` pattern, and `_flatten_text_links` appends a formatted link's
        # TARGET into the text, so anyone who can post a link could hand the scan a
        # handle to find. Comparison is on the lowercased handle, which is also what
        # makes it exact — Telegram usernames extend one another (`@kirocrewbot`,
        # `@kirocrewbot2`, `@kirocrewbot_dev` may all sit in one Topic), and an
        # entity names one username rather than a span to be matched.
        if getattr(msg, "has_entities", False):
            return handle.lower() in getattr(msg, "mentions", ())
        # No entity list: a synthesized message (an album with no captions, a legacy
        # or hand-built envelope). Fall back to the token matcher rather than
        # refusing, since "never parsed" is not "nobody was mentioned" — and such a
        # message has no entities precisely because it also has no auto-detected
        # URL for the matcher to trip over. `(?![A-Za-z0-9_])` is the same grammar
        # `_strip_bot_mention` uses, so `@kirocrewbot` does not match inside
        # `@kirocrewbot2`.
        return _mention_re(handle).search(msg.text or "") is not None

    def _activation_outcome(self, msg: InboundMessage) -> str | None:
        """``None`` to serve this message, else the SEL outcome to audit and drop.

        Scoped to non-private chats: a 1:1 DM is unconditionally served, matching
        Slack, whose separate ``slack_dm_activation`` also defaults to ``always``.
        Mixing the two would mean an operator narrowing a noisy Topic silently
        muted their own DM.

        Mirrors ``forum_gate_outcome``'s shape — ``str | None`` — so both gates
        audit through one code path and a reader can see they are the same kind of
        decision at two different altitudes: may it, then should it.

        Deliberately WITHOUT Slack's ``thread_follow`` escape hatch, which lets an
        already-active thread continue unaddressed. Slack needs it because a Slack
        thread offers no way to aim a message at the bot specifically; Telegram
        does — replying to one of its messages, which ``_addresses_this_bot``
        already treats as addressing it. Adding a second, implicit route would make
        ``mention`` mean "mention, or some window after the last answer", which is
        the sort of rule an operator cannot predict from its name.
        """
        if getattr(msg, "chat_type", "private") == "private":
            return None
        # A press on the bot's own inline keyboard is addressing the bot by
        # construction — there is no @handle to type and no message to reply to —
        # so it is served in every mode, `off` included: the operator who set `off`
        # still expects their own tap to do something, and the keyboard only exists
        # because this bot posted it.
        if getattr(msg, "from_widget", False):
            return None
        activation = self.cfg.telegram.forum_activation
        if activation == ACTIVATION_OFF:
            return "denied_activation_off"
        if activation == ACTIVATION_MENTION and not self._addresses_this_bot(msg):
            return "denied_activation_mention_only"
        return None

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
            idle_minutes=self.cfg.messaging.idle_reset_minutes,
            daily_reset_hour=self.cfg.messaging.daily_reset_hour,
        )
        return self._session_key(route)

    @staticmethod
    def _reply_target(msg: InboundMessage, *, interpret_commands: bool) -> int | None:
        """The message this turn should visibly answer, or ``None`` for no quote.

        Slack attaches every answer to what triggered it (``thread_ts or msg_ts``),
        which costs nothing there because a thread is the unit of conversation.
        Telegram has no such unit below the Topic, so attaching unconditionally
        would put a quote block above every reply in a 1:1 DM — where the answer
        already follows the question with nothing in between, so the quote adds a
        line of chrome and no information.

        It IS attached in the two cases where the link is genuinely ambiguous:

        * a **non-private chat** (a forum Topic), where several allow-listed
          participants can be talking at once and a flat answer belongs to nobody
          in particular;
        * a **drained queue turn** (``interpret_commands=False``, the marker the
          drain path passes), which is answered after the turn that was already
          running and therefore lands well below the message it answers.

        Returns ``None`` when the id is absent rather than guessing — the client's
        ``allow_sending_without_reply`` covers a target deleted after this point,
        but a zero id is not a target at all.
        """
        if getattr(msg, "chat_type", "private") == "private" and interpret_commands:
            return None
        return getattr(msg, "message_id", 0) or None

    async def _uploads_restricted(self, session_key: str) -> bool:
        """True when this session must not ship local file bytes to Telegram.

        The ladder and its fail-closed reasoning live in
        :func:`kiro_crew.messaging.upload_gate.uploads_restricted`, shared with the
        Discord dispatcher; this supplies Telegram's dashboard state and audit
        label.

        It cannot fire here YET, and that is worth stating rather than implying:
        the gate keys on a ``dashboard:`` session key, and this channel derives
        its key from the route alone (``supports_session_resume`` is False), so no
        Telegram turn carries one. Wired regardless — a forum Topic is readable by
        every member of its supergroup, so the day inbound resume lands the
        ceiling has to already be on the path rather than be remembered.

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

    # ── /agent (inline-button agent picker) ────────────────────────────────

    @staticmethod
    def _installed_agent_names() -> list[str]:
        """Every installed agent spec's name, user-level scope.

        ``list_agents`` caches on a directory signature but still reads and
        parses each JSON on a miss, so callers run it off the loop.
        """
        return sorted({info.name for info in list_agents() if info.name})

    def _agent_choices(self, names: list[str]) -> tuple[tuple[str, str], ...]:
        """``(agent_id, label)`` rows to offer, "" first for the configured default.

        Sourced from the installed agent specs, so the list is what this machine
        can actually load — a static catalogue would offer an agent whose spec is
        absent and fail at the next session start, which is exactly the failure
        mode the ``/model`` picker avoids by listing only advertised ids.
        """
        configured = self._configured_agent()
        rows: list[tuple[str, str]] = [("", f"Default ({configured})")]
        rows.extend((name, name) for name in names[:_PICKER_LIMIT])
        return tuple(rows)

    async def _handle_agent(self, route: tuple[str, str], chat_id: int, arg: str) -> None:
        """Post the agent keyboard, or report the current pick when there is none.

        Button-only, for the same reason ``/model`` is: a free-text agent name is
        a guess at something the user cannot enumerate, and a typo lands as a
        cold-start failure on the next message rather than an error here.
        """
        assert self.client is not None
        thread = self._route_thread(route)
        try:
            names = await asyncio.to_thread(self._installed_agent_names)
        except Exception:
            logger.warning("telegram /agent: agent discovery failed", exc_info=True)
            names = []
        choices = self._agent_choices(names)
        if len(choices) <= 1:
            await self._reply(chat_id, "No agent list available on this machine.", thread=thread)
            return
        current = self._agent_pref.get(route, "")
        current_label = next(
            (label for aid, label in choices if aid == current), current or "Default"
        )
        header = f"Current agent: {current_label}\nPick one:"
        if arg.strip():
            header = f"/agent takes no argument — pick from the list.\n\n{header}"
        keyboard = [
            [{"text": f"{'• ' if aid == current else ''}{label}", "callback_data": f"g:{index}"}]
            for index, (aid, label) in enumerate(choices)
        ]
        message_id = await self._reply(
            chat_id, header, thread=thread, reply_markup={"inline_keyboard": keyboard}
        )
        if message_id is None:
            return
        now = time.time()
        self._prune_pickers(self._agent_pickers, now)
        self._agent_pickers[f"{chat_id}:{message_id}"] = _Picker(
            route=route, created_at=now, choices=choices
        )

    async def _apply_agent(self, route: tuple[str, str], agent_id: str) -> str:
        """Record *agent_id* for *route* and report what it changed.

        The agent is part of the session key, so a pick necessarily opens a FRESH
        conversation — there is no in-place swap to claim, and the message says so
        rather than implying the running conversation changed spec. The previous
        conversation is not destroyed: switching back reaches the same key again.

        REFUSED while a reply is streaming, for the same reason ``/compact``
        refuses: everything about a running turn is keyed on the session key —
        ``_active_renderers`` for the steer chip, the queue receipt, ``/stop``'s
        provider lookup — so moving the key out from under it would leave that
        turn running with no route back to it, and a ``/stop`` would report
        nothing running while the answer kept arriving.
        """
        label = agent_id or f"Default ({self._configured_agent()})"
        key = self._session_key(route)
        if self.sessions.is_busy(key):
            return (
                f"⏳ Still working on your last message — send /agent again once "
                f"it finishes to switch to {label}."
            )
        had = self.sessions.has_session(key)
        if agent_id:
            self._agent_pref[route] = agent_id
        else:
            self._agent_pref.pop(route, None)
        if not had:
            return f"✅ Agent set to {label}."
        return (
            f"✅ Agent set to {label} — this starts a fresh conversation. "
            f"Switch back to return to the previous one."
        )

    # ── /sessions, /title and the service-backed commands ──────────────────

    async def _handle_sessions(
        self,
        chat_id: int,
        query: str = "",
        *,
        caller: str = "",
        thread: int | None = None,
    ) -> None:
        """List recent conversations, or search their titles and message content.

        Still read-only. A Resume button would need per-chat inbound rerouting (the
        machinery Discord carries in ``discord/session_resume.py``) which this
        channel does not have, and a button that binds a session the next typed
        message then bypasses is worse than no button. Search uses ConversationLog's
        dashboard ranking rather than a Telegram-only title filter, but renders the
        same bounded title/agent rows as the recent list.

        *caller* is the requesting user's id, and the audit record's subject. It was
        the constant ``"telegram"``, which is the SOURCE, not the subject: with more
        than one entry in ``allowed_user_ids`` — the forum case this channel serves —
        a read of every conversation's titles must be attributable to a participant.
        """
        normalized_query = " ".join(query.split())
        rows: list[dict] | None
        if normalized_query:
            operation = "telegram.sessions_data_access"
            if self.conv_log is None:
                sel().log_api_access(
                    caller=caller or "telegram",
                    operation=operation,
                    outcome="error",
                    source="telegram",
                    resources="0 sessions read (conversation log unavailable)",
                )
                await self._reply(chat_id, "Sessions unavailable.", thread=thread)
                return
            try:
                found = await asyncio.to_thread(
                    self.conv_log.search_sessions,
                    query,
                    SEARCH_FETCH_LIMIT,
                )
                rows = [
                    row
                    for row in found
                    if isinstance(row, dict) and not is_incognito_transcript(row.get("memory_mode"))
                ][:_SESSIONS_LIMIT]
            except Exception as exc:
                sel().log_api_access(
                    caller=caller or "telegram",
                    operation=operation,
                    outcome="error",
                    source="telegram",
                    resources="0 sessions read (search failed)",
                    error=display_safe(str(exc))[:200],
                )
                logger.exception("telegram: session search failed")
                await self._reply(chat_id, "Sessions unavailable.", thread=thread)
                return
            sel().log_api_access(
                caller=caller or "telegram",
                operation=operation,
                outcome="allowed",
                source="telegram",
                resources=f"{len(rows)} sessions read",
            )
        else:
            rows = await collect_recent_sessions_audited(
                self.sessions,
                caller=caller or "telegram",
                source="telegram",
                limit=_SESSIONS_LIMIT,
                with_messages=False,
            )
            if rows is None:
                await self._reply(chat_id, "Sessions unavailable.", thread=thread)
                return

        query_label = display_safe(normalized_query)[:_SESSION_QUERY_CHARS]
        if not rows:
            if normalized_query:
                await self._reply(
                    chat_id,
                    f"No conversations matched “{query_label}”. Try fewer words, "
                    "or use /sessions to see the most recent conversations.",
                    thread=thread,
                )
            else:
                await self._reply(chat_id, "No recent conversations.", thread=thread)
            return

        if normalized_query:
            lines = [f"🔎 Conversation search for “{query_label}”:"]
        else:
            lines = ["🧵 Recent conversations:"]
        for row in rows:
            key = str(row.get("key") or "")
            if normalized_query:
                logical_key = (
                    history_dashboard_key(key) or self.sessions.channel_key_for_stem(key) or key
                )
                active = bool(logical_key and self.sessions.has_session(logical_key))
            else:
                active = bool(row["active"])
            mark = "🟢" if active else "⚫"
            title = redact(str(row.get("title") or key or "Untitled session"))[
                :_SESSION_TITLE_CHARS
            ]
            agent = redact(str(row.get("agent") or "kirocrew"))[:_SESSION_AGENT_CHARS]
            lines.append(f"{mark} {title} — {agent}")
        lines.append("")
        if normalized_query:
            lines.append("Open one with /kirocrew dashboard.")
        else:
            lines.append("Search with /sessions <words>, or open one with /kirocrew dashboard.")
        await self._reply(chat_id, "\n".join(lines), thread=thread)

    async def _handle_title(self, route: tuple[str, str], chat_id: int, arg: str) -> None:
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
        titled_key = self._rotated_session_key(route)
        # A restricted session writes NOTHING, and a title is not an exception: the
        # metadata write CREATES the transcript file, so on a `/temporary` or
        # `/incognito` conversation this one command would persist user-authored
        # content for a mode that promised not to. `_persist_turn` gates the same
        # way, and so does Slack's own `/title`; this is the third write on the same
        # promise rather than a new rule.
        #
        # The predicate is the channel's in-process tracker, NOT the transcript's
        # `memory_mode` header: the dashboard deliberately writes an incognito
        # transcript and marks it, discarding on close, so a gate down in
        # `ConversationLog` would refuse a write that path is entitled to make.
        if privacy_mode.is_restricted(titled_key):
            await self._reply(
                chat_id,
                "🔒 This conversation is private, so its name isn't saved.",
                thread=thread,
            )
            return
        try:
            await asyncio.to_thread(self.conv_log.set_title, titled_key, title)
        except Exception:
            logger.warning("telegram /title: set_title failed", exc_info=True)
            await self._reply(chat_id, "⚠️ Couldn't rename this conversation.", thread=thread)
            return
        await self._reply(chat_id, f"✅ Renamed to “{title}”.", thread=thread)

    async def _handle_cron(
        self, chat_id: int, arg: str, *, caller: str = "", thread: int | None = None
    ) -> None:
        """List / pause / resume / remove scheduled jobs, via the shared layer.

        *caller* is the Telegram user id, threaded through so ``remove all``'s SEL
        audit names the person who issued it rather than only the surface. Same
        attribution the Slack, dashboard, MCP and CLI paths carry.
        """
        if self.cron_service is None:
            await self._reply(chat_id, "Cron is not running on this instance.", thread=thread)
            return
        reply = await cron_command_reply(
            f"cron {arg}".strip(), self.cron_service, source="telegram", caller=caller
        )
        if reply is None:
            await self._reply(
                chat_id,
                "Usage: /cron list | pause <id> | resume <id> | remove <id>|all",
                thread=thread,
            )
            return
        await self._reply_markdown(chat_id, reply, thread=thread)

    async def _handle_spawn(
        self, route: tuple[str, str], chat_id: int, arg: str, *, thread: int | None = None
    ) -> None:
        """Run a task in a background subagent, or list the running ones."""
        if self.subagent_manager is None:
            await self._reply(
                chat_id, "Subagents are not available on this instance.", thread=thread
            )
            return
        # Rotated: the subagent's completion arrives later and is routed by this
        # key, so binding it to a generation the next message abandons sends the
        # result to a conversation nobody is reading.
        reply = spawn_task_reply(arg, self.subagent_manager, self._rotated_session_key(route))
        if reply is None:
            await self._reply(chat_id, "Usage: /spawn <task>  ·  /spawn list", thread=thread)
            return
        await self._reply_markdown(chat_id, reply, thread=thread)

    async def _handle_task(
        self,
        chat_id: int,
        arg: str,
        *,
        route: tuple[str, str],
        thread: int | None = None,
    ) -> None:
        """Drive the task runner: ``run <spec>`` / ``status`` / ``cancel``.

        The originating session key rides along so a task that later blocks on an
        approval can tell THIS conversation, rather than only the Slack owner's DM
        — which is the whole notice a Telegram-only operator would never see.
        """
        if self.task_runner is None:
            await self._reply(
                chat_id, "The task runner is not available on this instance.", thread=thread
            )
            return
        # Rotated, for the same reason as /spawn: the approval notice comes back
        # later and is routed by this key.
        reply = await task_arg_reply(
            arg, self.task_runner, session_key=self._rotated_session_key(route)
        )
        if reply is None:
            await self._reply(
                chat_id,
                "Usage: /task run <spec-path> | /task status | /task cancel",
                thread=thread,
            )
            return
        await self._reply_markdown(chat_id, reply, thread=thread)

    # ── Inline-button handler (client's on_callback) ───────────────────────

    async def on_callback(self, cb: "TelegramCallback") -> None:
        """Route an inline-keyboard press: approval decisions or [OPTIONS:]."""
        assert self.client is not None
        # Auth first (deny-by-default short-circuit): don't even ack an
        # unauthorized user's press — avoids a wasted Bot API round-trip.
        if not self._authorized(cb.user_id):
            return
        # Chat-type gate — an authZ boundary that MUST mirror
        # ``transport.receive`` EXACTLY: buttons live on messages the bot sent,
        # so a press can originate from a private DM or an allow-listed
        # supergroup forum Topic. Uses the SHARED ``forum_gate_outcome`` predicate
        # so this fail-closed decision can never drift from the inbound path.
        # NEVER honor a callback from an ordinary group, a non-allow-listed
        # supergroup, or the supergroup General chat (no thread). This gate is
        # ADDITIONAL to the owner/user authorization above, not a replacement.
        # The allow-list source here is LIVE cfg (self.cfg.telegram.*), whereas
        # the transport uses its construction-time frozen copy; that source
        # difference is DELIBERATE (see forum_gate_outcome).
        outcome = forum_gate_outcome(
            cb.chat_type,
            cb.chat_id,
            getattr(cb, "message_thread_id", None),
            allow_forum=self.cfg.telegram.allow_forum,
            allowed_forum_chat_ids=self.cfg.telegram.allowed_forum_chat_ids,
        )
        if outcome is not None:
            sel().log_api_access(
                caller=str(cb.user_id) or "unknown",
                operation="telegram_transport.on_callback",
                outcome=outcome,
                source="telegram",
            )
            return
        # Answer FIRST (after auth) to dismiss the button spinner — the governance
        # check below does off-loop profile-store I/O that could otherwise delay
        # the callback answer past Telegram's expectation. Answering is a no-op UI
        # dismissal; it does NOT resolve the approval or start a turn.
        await self.client.answer_callback(cb.callback_query_id)

        data = cb.data or ""

        # Inbound channels-governance gate (off-loop) — a callback press RESOLVES a
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
        if not _is_reject_press and not await channel_inbound_permitted("telegram"):
            logger.info("telegram callback dropped: denied by channels governance policy")
            return

        # Route the callback to the same conversation identity its turn used so
        # an approval/[OPTIONS:] press resolves against the correct session key:
        # a private press -> (direct, user_id); an allow-listed forum press ->
        # the per-Topic forum key (chat_type + message_thread_id carried through).
        route = self._route_key(
            chat_type=cb.chat_type,
            user_id=cb.user_id,
            chat_id=cb.chat_id,
            thread=getattr(cb, "message_thread_id", None),
        )
        # Topic id to thread the [OPTIONS:] echo sends back into (None for a DM).
        cb_thread = self._route_thread(route)

        # Tool-approval decision: "a:<request_id>:<nonce>:<1|0>".
        if data.startswith("a:"):
            # "a:<request_id>:<nonce>:<flag>". Parsed from the RIGHT so a request id
            # containing a colon cannot shift the fields: the flag and the nonce are
            # the last two segments and the id is whatever precedes them. A button
            # rendered before the nonce existed leaves `nonce` holding part of the id
            # and fails the constant-time compare, which is the correct answer — it
            # is a press from an earlier process.
            body = data[2:]
            rest, _, flag = body.rpartition(":")
            rid, _, nonce = rest.rpartition(":")
            trust = flag == "t"
            approved = flag in ("1", "t")
            session_key = self._session_key(route)
            key = TelegramApprovalDecider.key(session_key, rid)
            # Asked BEFORE the grant, because Trust is the one press with a side
            # effect that OUTLIVES the prompt: it auto-approves every later tool in
            # this conversation and writes the session's approval policy to `auto`
            # so subagents inherit it. The registry is empty after a gateway
            # restart, so without this every Trust button still in the chat's
            # scrollback would silently re-grant standing authority while the reply
            # said the approval had expired.
            pending = TelegramApprovalDecider.is_pending(key, nonce)
            if trust and pending:
                # Granted BEFORE resolving, so the tool this very prompt is asking
                # about is covered by the grant the press just made — resolving
                # first would approve this one by the button and then let the NEXT
                # tool race the write.
                add_trusted_session(session_key, self.sessions)
                sel().log_api_access(
                    caller=str(cb.user_id) or "unknown",
                    operation="telegram.trust_session",
                    outcome="allowed",
                    source="telegram",
                    resources=f"session={session_key}",
                )
            elif trust:
                # Audited as a refusal rather than dropped: an operator pressing
                # Trust and getting nothing needs the reason to be findable.
                sel().log_api_access(
                    caller=str(cb.user_id) or "unknown",
                    operation="telegram.trust_session",
                    outcome="denied",
                    source="telegram",
                    resources=f"session={session_key}",
                    error="no_pending_approval",
                )
            resolved = TelegramApprovalDecider.resolve_global(key, approved, nonce=nonce)
            if resolved:
                if trust:
                    verdict = "🤝 Trusted — this conversation's tools auto-approve."
                else:
                    verdict = "✅ Approved" if approved else "🚫 Denied"
            else:
                # No pending decision to resolve — the request already timed out
                # (decider denies by default and pops the key), was answered, or the
                # press came from a STALE keyboard whose nonce no longer matches
                # (request ids restart at 1 per provider process, so an old button can
                # name an id that is live again for a different tool).
                # Don't imply the press took effect: a post-timeout "Approve" on
                # an already-denied tool must not display "Approved".
                verdict = "⌛ This approval already expired."
            await self.client.edit_message(
                cb.chat_id, cb.message_id, verdict, reply_markup={"inline_keyboard": []}
            )
            return

        # Model pick: "m:<index>" into the picker posted on this message.
        # A picker press: "m:<index>" for a model, "g:<index>" for an agent. Both
        # resolve through one helper — the staleness contract (consume before
        # applying, and the wording that must not claim "expired" for a picker that
        # was simply used) is one decision, and two copies of it means a fix to
        # double-press or eviction handling reaches one picker.
        for prefix, table, noun, command, apply, operation, resource in (
            (
                "m:",
                self._model_pickers,
                "model",
                "/model",
                self._apply_model,
                "telegram.set_model",
                "model",
            ),
            (
                "g:",
                self._agent_pickers,
                "agent",
                "/agent",
                self._apply_agent,
                "telegram.set_agent",
                "agent",
            ),
        ):
            if not data.startswith(prefix):
                continue
            taken = await self._consume_picker(cb, data, table, noun=noun, command=command)
            if taken is None:
                return
            picker, value, label = taken
            outcome = await apply(picker.route, value)
            sel().log_api_access(
                caller=str(cb.user_id) or "unknown",
                operation=operation,
                outcome="allowed",
                source="telegram",
                resources=f"{resource}={label}",
            )
            # One edit carries both the result text and the retired keyboard, so
            # the buttons never outlive the choice they represent.
            await self.client.edit_message(
                cb.chat_id, cb.message_id, outcome, reply_markup={"inline_keyboard": []}
            )
            return

        # [OPTIONS:] choice: ``opt:<index>:<origin-tag>``. The label is
        # recovered from the button text; the tag binds it to the session that
        # authored the keyboard.
        if data.startswith("opt:"):
            parts = data.split(":", 2)
            origin_tag = parts[2] if len(parts) == 3 else ""
            choice_text = cb.label
            # Retire the keyboard but KEEP the original answer text intact --
            # tapping an option must not overwrite the answer bubble. The choice
            # is handled as a fresh turn whose reply arrives as a NEW message.
            await self.client.edit_message_reply_markup(
                cb.chat_id, cb.message_id, {"inline_keyboard": []}
            )
            if not origin_tag:
                # A button created before provenance existed cannot prove which
                # session its model-authored text belongs to. Never infer that
                # from whatever session happens to be current now.
                await self._reply(
                    cb.chat_id,
                    _UNTAGGED_OPTIONS_REFUSAL,
                    thread=cb_thread,
                )
                return
            if not choice_text:
                await self._reply(
                    cb.chat_id,
                    "⚠️ Couldn't read that choice — please type it instead.",
                    thread=cb_thread,
                )
                return
            # Echo the picked option as its own block (a button tap can't
            # render as a real user message), then re-dispatch as a fresh turn.
            echoed = await self._reply(
                cb.chat_id,
                f"<blockquote>{html.escape(choice_text)}</blockquote>",
                thread=cb_thread,
                parse_mode="HTML",
                retry_plain=False,
            )
            if echoed is None:  # malformed HTML -> plain fallback
                await self._reply(cb.chat_id, f"» {choice_text}", thread=cb_thread)
            # Re-inject the choice with the callback's ORIGINAL route so a forum
            # press stays under the same Topic. ``from_widget`` bypasses forum
            # activation because tapping this bot's own keyboard addresses it.
            synthetic = TelegramInboundMessage(
                channel_type="telegram",
                user_id=str(cb.user_id),
                conversation_id=str(cb.chat_id),
                text=choice_text,
                thread_id=(
                    str(cb.message_thread_id) if getattr(cb, "message_thread_id", None) else None
                ),
                chat_type=cb.chat_type,
                from_widget=True,
            )
            # The label is MODEL-AUTHORED. A leading command token is ordinary
            # turn content, never permission for the model to execute `/new`,
            # `/dashboard`, `/yolo`, or any future command. The non-empty tag
            # still asks handle_message to validate current and final affinity.
            await self.handle_message(
                synthetic,
                interpret_commands=False,
                origin_tag=origin_tag,
            )

    # ── Helpers ────────────────────────────────────────────────────────────

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
        fallback. Used to thread every dispatcher-originated send back into the
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

    async def _require_direct_chat(
        self,
        cmd: str,
        route: tuple[str, str],
        chat_id: int,
        user_id: int,
        *,
        thread: int | None,
        subject: str,
    ) -> bool:
        """Refuse a host-wide listing outside a DM. True = the caller may proceed.

        The allow-list gates who may DRIVE a turn, not who can READ the reply, and a
        forum Topic is readable by the whole supergroup. So a command whose answer
        names state belonging to the host rather than to this conversation -- every
        session on the box, every scheduled job -- would disclose it to members who
        were never allow-listed at all. Same rule `/kirocrew dashboard` follows.

        Scoped to the LISTINGS, and per argument rather than per command. It is not
        "host-wide command" as a category: `/spawn <task>` and `/task run <spec>` act
        on THIS conversation's session and report on their own work, and `/stop` and
        `/compact` are how a forum operator drives the Topic they are in. Refusing
        those would break the forum surface to no benefit, since a caller who can
        reach them already had to be allow-listed.

        But the same command changes scope with its argument, which the command name
        does not show: `/spawn list` renders every subagent on the box with its task
        text, and `/task status` reports the one global runner. `lists_host_state`
        (`messaging/commands.py`) is the answer to that, held next to the functions
        that build those replies -- reading `/spawn <task>` and generalizing to
        `/spawn` is precisely how the listing got through the first time.

        Slack's equivalents have no such shape -- their reply lands in a DM or in a
        thread the caller is already in -- so this is the Telegram-specific half of
        the same rule rather than a divergence from parity.
        """
        if route[0] == CHAT_TYPE_DIRECT:
            # A DM is the right AUDIENCE, and for a host-wide listing it also has to
            # be the right PERSON. `allowed_user_ids` is a list of people permitted
            # to talk to the agent, not a claim that any one of them is the operator,
            # so with several entries a listing of every conversation on the host
            # hands one allow-listed human another's conversation titles -- under the
            # default per-peer dm_scope those are separate sessions belonging to
            # separate people. This is the rule the owner notification already
            # follows for the same reason (messaging.md: "a channel must be able to
            # NAME the owner: exactly one configured target, or nothing", which cites
            # `/sessions`' owner-only rule as its premise); applying it here is that
            # rule reaching the surface it was named after.
            #
            # It costs an operator who lists two of their own accounts, which is the
            # same cost main accepted there: the count is over ALL configured
            # entries, because a two-person allow-list is a guess either way and
            # guessing wrong discloses a third party's titles.
            if len(self._allowed) <= 1:
                return True
            sel().log_api_access(
                caller=str(user_id),
                operation=f"telegram.{cmd}_command",
                outcome="denied",
                source="telegram",
                resources=f"allowed_identities={len(self._allowed)}",
                error="no_unambiguous_owner",
            )
            await self._reply(
                chat_id,
                f"🔒 The {subject} names conversations across this whole install, so "
                "it is only sent when `telegram.allowed_user_ids` holds a single "
                "operator. It currently holds several, and the agent cannot tell "
                "which of them owns the install.",
                thread=thread,
            )
            return False
        sel().log_api_access(
            caller=str(user_id),
            operation=f"telegram.{cmd}_command",
            outcome="denied",
            source="telegram",
            resources=f"chat={chat_id}",
            error="shared_topic_audience",
        )
        await self._reply(
            chat_id,
            f"🔒 The {subject} is only sent in a direct message. DM me `/{cmd}`.",
            thread=thread,
        )
        return False

    async def _reply_markdown(
        self, chat_id: int, text: str, *, thread: int | None = None
    ) -> int | None:
        """Send a reply whose text carries markdown, rendered rather than literal.

        The shared command replies (``messaging/commands.py``) are written in the
        markdown Slack renders natively — ``*Your cron jobs:*``, backticked job
        ids — and Telegram's ``send_message`` defaults to plaintext, so posting
        them unrendered shows the asterisks and backticks to the user. Converted
        through the renderer's own translator so there is one markdown→Telegram
        grammar, with ``retry_plain`` so a conversion Telegram rejects degrades to
        readable text rather than failing the reply.

        Rendering markup is what makes this a redaction sink, and not all of this
        text is ours: ``/cron list`` and ``/tasks`` echo job and task names an LLM
        wrote, so a credential split by ``**`` survives the byte-level pass and
        the translator would rejoin the halves into one rendered key.
        ``md_to_telegram_html_safe`` redacts against the rendered form first;
        off-loop because that scan is the expensive half.
        """
        return await self._reply(
            chat_id,
            await asyncio.to_thread(md_to_telegram_html_safe, text),
            thread=thread,
            parse_mode="HTML",
        )

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
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=slot,
        )

    def _seed_gen(self, route: tuple[str, str]) -> int:
        slot, comp = route
        return seed_generation(
            self.sessions,
            channel="telegram",
            agent=self._resolve_agent(route),
            user_id=comp,
            dm_scope=self.cfg.messaging.dm_scope,
            chat_type=slot,
        )

    def _origin_mirror_link(self, route: tuple[str, str], chat_id: int) -> ChannelLink:
        """The mirror location for the chat a conversation is being read in.

        One definition shared by the automatic bind, ``/link`` and ``/unlink``:
        an unlink matches an occupied location by VALUE, so a second spelling of
        "this chat" would let the release miss the binding the bind wrote.

        Carries the forum Topic so dashboard-mirrored replies for a forum-linked
        session thread back into the SAME Topic (via
        ``_deliver_cross_surface_reply``'s ``thread_id=link.thread_id``), not the
        supergroup General. ``None`` only for a DM — an authorized forum turn
        always carries a Topic, General being denied at the gate.
        """
        topic = self._route_thread(route)
        return ChannelLink(
            "telegram",
            channel_id=str(chat_id),
            thread_id=(str(topic) if topic is not None else None),
        )

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

    async def _handle_link(self, route: tuple[str, str], chat_id: int) -> None:
        """Re-enable mirroring of this conversation's dashboard tab back here.

        The rebind sequence, its batching and its reply live in the shared
        :func:`~kiro_crew.messaging.link.rebind_conversation_location`, the
        counterpart of the ``release_conversation_location`` that ``/unlink``
        uses; this only supplies Telegram's spelling of "this conversation" and
        of the unlink command.
        """
        assert self.client is not None
        # Through the shared helper, which owns the claim-before-withdrawal
        # ordering and the single batched write. The key is ROTATED: a mirror
        # binding is DURABLE and re-read on the next inbound turn, so writing it
        # against a generation the idle window has retired would leave the very
        # next message unlinked again.
        reply = rebind_conversation_location(
            self.sessions,
            key=self._rotated_session_key(route),
            location=self._origin_mirror_link(route, chat_id),
            unlink_command="/unlink",
        )
        await self._reply(chat_id, reply, thread=self._route_thread(route))

    async def _handle_unlink(self, route: tuple[str, str], chat_id: int) -> None:
        assert self.client is not None
        # Rotated, for the same reason as /link: the opt-out is durable and is
        # re-read per turn, so it has to land on the key the next turn will use.
        key = self._rotated_session_key(route)
        # Persist the refusal BEFORE releasing: mirroring is re-asserted on every
        # inbound turn, so a release alone would be undone by the user's next
        # message. Batched with the release so the pair is one whole-map write
        # instead of four. No dashboard nudge here: a swept slot's link chip is
        # refreshed by the periodic channel_slot_reconciler push.
        with self.sessions.batched_save():
            self.sessions.set_mirror_opt_out(key, True)
            reply, _swept = release_conversation_location(
                self.sessions,
                key=key,
                location=self._origin_mirror_link(route, chat_id),
                channel="telegram",
            )
        await self._reply(chat_id, reply, thread=self._route_thread(route))

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
        # The privacy modes' central promise: an incognito or temporary
        # conversation writes no transcript. Checked HERE rather than at each
        # caller because this is the only writer, so one gate covers the turn, the
        # drained queue and the steered continuation alike.
        if privacy_mode.is_restricted(session_key):
            return
        # This row is an EGRESS: persisted, then served to dashboard readers. The turn
        # has already run, so scrubbing here cannot rewrite the prompt the model saw.

        user_text = redact_via_context(user_text)
        self.conv_log.append(session_key, "user", user_text, agent=agent, mid=mint_row_mid())
        if reply_text:
            self.conv_log.append(
                session_key, "assistant", reply_text, agent=agent, mid=mint_row_mid()
            )
        if is_new and not auto_title.is_titled(session_key):
            # Skipped when auto-title has CLAIMED this session, because the two
            # writers race and the loser is always the generated one: the fallback
            # runs first (it is synchronous, on the turn), and
            # ``_record_is_untitled`` then refuses the generated title because the
            # record already has one. The effect is not a cosmetic downgrade — it
            # makes auto-titling inert on this channel while still spending a
            # background turn per conversation to produce a name nobody sees.
            #
            # The claim, not the completion, is the right thing to check: a claimed
            # session is one where a name is on its way, and if that turn fails
            # ``maybe_auto_title`` releases the claim so the next exchange retries.
            # A conversation that is briefly untitled is a better outcome than one
            # permanently named after its first forty characters.
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
        soft_pct = self.cfg.telegram.soft_threshold_pct
        if pct >= soft_pct and compact_unsupported_backend(provider):
            # Capability gate (#8156): the nudge advises /compact, which this
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

    async def _handle_compact(self, route: tuple[str, str], chat_id: int) -> None:
        """In-place ACP ``/compact`` on the user's session (mirrors Slack).

        Holds the per-session semaphore for the WHOLE compaction. Each Telegram
        update is dispatched as its own task, so a bare ``locked()`` check
        followed by ``stream_command`` would race: a normal turn could take the
        semaphore in the window between the check and the stream, and the two
        would then interleave JSON-RPC on one stdio channel and corrupt session
        state. ``try_acquire()`` takes the semaphore atomically (or refuses if a
        turn is already in flight); the ``finally`` always releases it.
        """
        assert self.client is not None
        session_key = self._session_key(route)
        thread = self._route_thread(route)
        # Atomically take the turn semaphore, or refuse. Distinguish "busy" (a
        # turn is streaming) from "no session yet" for the user-facing note.
        if not await self.sessions.try_acquire(session_key):
            if self.sessions.has_session(session_key):
                await self._reply(
                    chat_id,
                    "⏳ Still working on your last message — try /compact once it finishes.",
                    thread=thread,
                )
            else:
                await self._reply(chat_id, "No active session to compact.", thread=thread)
            return
        try:
            provider = self.sessions.get_provider(session_key)
            if provider is None:
                await self._reply(chat_id, "No active session to compact.", thread=thread)
                return

            # Capability gate (#8156, mirroring the dashboard's #7800 gate): a
            # backend that cannot serve a manual /compact treats the prompt as
            # ordinary text and never answers, so dispatching would strand the
            # 120s wait below. Informational, never an error.
            unsupported = compact_unsupported_backend(provider)
            if unsupported:
                await self._reply(chat_id, compact_unsupported_reply(unsupported), thread=thread)
                return

            status_id = await self._reply(chat_id, "🔄 Compacting context…", thread=thread)
            result_text: str | None = None
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
                    err = cr.get("summary", "")
                    result_text = f"❌ Compaction failed: {err}" if err else "❌ Compaction failed."
                else:
                    result_text = "⚠️ Compaction timed out."
            except Exception:
                logger.warning("Telegram /compact failed for %s", session_key, exc_info=True)
                result_text = "❌ Compaction failed unexpectedly."
                # Drop the wedged native conversation, NOT the session's channel
                # identity: the map entry carries the mirror binding, so a full
                # ``destroy`` would silently unlink a mirrored conversation.
                # Housekeeping never unlinks (see ``SessionMap.prune`` and
                # ``SessionManager._recycle_held``).
                try:
                    await self.sessions.discard_conversation(session_key)
                except Exception:
                    logger.debug("Telegram: discard after compact failure failed", exc_info=True)

            final = result_text or "✅ Context compacted."
            if status_id:
                await self.client.edit_message(chat_id, status_id, final)
            else:
                await self._reply(chat_id, final, thread=thread)
        finally:
            # Always release the semaphore we took. No-op if the except path
            # already tore the session down (release() looks up by key).
            self.sessions.release(session_key)
