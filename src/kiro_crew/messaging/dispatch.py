"""Shared channel turn pipeline — the one turn loop channel dispatchers run.

:class:`ChannelTurns` owns the sequence every channel dispatcher but Telegram's
and the Slack transport's runs around :class:`TurnDriver`:

    governance backstop
    -> mute substitution                 (read once; a SilentRenderer when muted)
    -> hook auto-reply                   (HOOK_REPLY short-circuits, no session)
    -> renderer.on_turn_start()          (typing indicator before cold start)
    -> sessions.get_or_create            (+ the crew-log opener, when declared)
    -> prepare + set_channel + origin/mirror bind
    -> publish_turn_identity
    -> ctx_builder.build_message         (off-loop, embeds block)
    -> TurnDriver.run                    (shared redaction + approval ladder)
    -> COMPACTION_FAILED: reset; replay once more if the failure was transient
                                         and nothing was emitted (bounded)
    -> post-turn: delivery-aware accounting, record, surface, threshold notice,
                  SEL audit              (each guarded independently)
    -> finally: approval sweep, TurnBracket.settle, renderer.close(), release
                                         (release gated on acquire)

What stays per-channel is what actually differs between them: the wire
protocol, event normalization, ``authorize()`` semantics, rendering, command
vocabulary, the ack strings and the ledger. Channels inject those as the
pipeline's adapters (a renderer or renderer factory, ``record``, ``notice``,
``surface``, ``prepare``) rather than subclassing, so a capability the pipeline
lacks widens it once instead of forking it. Where a dispatcher still diverges
from the default it says so with a :class:`Drift` member, never with a copy.
:func:`drive_turn` is the same pipeline for the dispatchers that still describe
each turn as a :class:`ChannelTurn`.

Dependency direction is ``<channel> -> messaging`` (never the reverse), so this
module must not import any channel package.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Optional

from kiro_crew import runtime_death
from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED
from kiro_crew.agent_sdk.backends import Routing, routing_for
from kiro_crew.agent_sdk.drivers.acp_vocab import is_runtime_death
from kiro_crew.context import session_store_for_turn
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.history import transcript_stem
from kiro_crew.hooks import (
    HOOK_REPLY,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    event_is_spawn_run,
    hook_gate_kwargs,
)
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging import turn_ceiling
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, DirectiveConsumer, TurnDriver
from kiro_crew.messaging.identity import channel_inbound_permitted, publish_turn_identity
from kiro_crew.messaging.inbound_spool import (
    InboundRoute,
    spool_refused_turn,
    spool_refused_turn_sync,
)
from kiro_crew.messaging.link import (
    DM_SCOPE_UNIFIED,
    ChannelLink,
    bind_origin_mirror,
    canonical_key,
    channel_namespace_of,
    is_channel_session_key,
    split_dm_session_key,
)
from kiro_crew.messaging.renderer import (
    DONE,
    PROMPT_CHOICE,
    STEER_CONSUMED,
    TEXT_CHUNK,
    TOOL_CALL,
    OutputEvent,
    Renderer,
    SilentRenderer,
)
from kiro_crew.messaging.session_resume import persisted_session_agent
from kiro_crew.messaging.turn_bracket import consume_reinjection  # noqa: F401 (re-exported)
from kiro_crew.messaging.turn_bracket import driver_turn_landed  # noqa: F401 (re-exported)
from kiro_crew.messaging.turn_bracket import rearm_reinjection  # noqa: F401 (re-exported)
from kiro_crew.messaging.turn_bracket import rollback_skill_bodies  # noqa: F401 (re-exported)
from kiro_crew.messaging.turn_bracket import stop_reason_landed  # noqa: F401 (re-exported)
from kiro_crew.messaging.turn_bracket import TurnBracket
from kiro_crew.messaging.turn_ceiling import TurnCeilingExceeded
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import MonitorDispatchResult
from kiro_crew.safety_override import safety_override
from kiro_crew.security import (
    redact,
    redact_credentials,
    redact_exfiltration_urls,
    redact_local_paths,
)
from kiro_crew.sel import sel

# Imported from the leaf that DEFINES it rather than through kiro_crew.session:
# this module deliberately types ``sessions`` as ``Any`` to stay off the session
# package's import graph, and session_allocation imports nothing from messaging,
# so this direction cannot cycle.
from kiro_crew.session_allocation import SessionBusyError, SessionClosingError
from kiro_crew.start_priority import StartPriority

logger = logging.getLogger(__name__)

#: The agent a ``deny_all_tools`` turn runs on. Its spec declares ``tools: []``
#: and no MCP servers (``agent._install_guest_agent``), so the backend mounts
#: nothing for the session: no tool exists to call, whatever the operator's own
#: agent auto-approves. That is the only enforcement that holds on the stock kiro
#: backend, where a tool named in ``allowedTools`` raises no permission request
#: and therefore never reaches the driver's ``deny_all_tools`` branch. A spec of
#: its own, not the background ``kirocrew-lite``: that helper may grow a tool one
#: day and its empty prompt reads as "no user to address" on backends that need
#: one, while this agent talks to a person. The spec pin in
#: ``test_messaging_dispatch.py::TestToollessAgentSpecIsTheBoundary`` makes any
#: drift loud.
TOOLLESS_TURN_AGENT = "kirocrew-guest"


#: What a sender whose turn was refused as un-tool-less-able reads. One line, no
#: internals: silence reads as the agent ignoring the person, and the operator's
#: side of the story is the SEL row, not this note.
TOOLLESS_TURN_REFUSAL_NOTE = "This account cannot answer you on its current setup. Ask its owner."


def toolless_turns_supported(backend: str) -> bool:
    """Whether a ``deny_all_tools`` turn can be driven on *backend* at all.

    True only where the agent spec is what the harness mounts
    (``Routing.AGENT_SPEC``): there ``tools: []`` removes every tool. Read by
    channel startup to warn an operator whose configuration admits non-operator
    traffic on a backend that will refuse every such turn.
    """
    return routing_for(backend) is Routing.AGENT_SPEC


def warn_if_toolless_turns_unservable(
    channel: str, *, admits_non_operators: bool, backend: str, admission: str
) -> bool:
    """Warn once, at a channel's start, when its admitted non-operators will all be refused.

    Shared with every ``deny_all_tools`` adopter because the refusal itself lives
    on the shared seam (:class:`ChannelTurns`); a channel supplies only the two facts
    it alone knows, whether its configuration admits anyone but the operator and
    how (``admission``, for the message). Returns True when it warned.
    """
    if not admits_non_operators or toolless_turns_supported(backend):
        return False
    logger.warning(
        "%s: non-operator senders are admitted (%s) but agent.acp_backend=%r cannot "
        "run a tool-less turn; every non-operator turn will be refused with a note. "
        "Use the kiro backend or narrow admission.",
        channel,
        admission,
        backend,
    )
    return True


class ToollessTurnUnavailable(RuntimeError):
    """A ``deny_all_tools`` turn cannot be made tool-less on this session.

    Two causes. The session key a channel hands in for such a turn must be one
    that only tool-less turns ever use; a key shared with the operator's own
    turns would hand the sender the operator's agent, tools included. And the
    tool-less agent spec is honoured only by a backend whose routing is
    ``Routing.AGENT_SPEC`` (the spawn names the agent, so ``tools: []`` is what
    the harness mounts); a harness that reads no agent spec keeps its own native
    tools, and a project-preapproved one raises no permission request for the
    driver to refuse. Raised instead of running the turn either way: refusing
    costs the sender one reply, running it costs the operator their machine.
    """


async def admit_inbound_callback(
    sessions: Any,
    *,
    channel_type: str,
    route: InboundRoute | None,
    restricted: bool | Callable[[], Awaitable[bool]] = False,
) -> bool:
    """Reserve one handler task or durably refuse it before pre-turn effects.

    A callable restriction is resolved only after admission refuses the task.
    Resume-capable channels use that form because their effective session takes
    an awaited routing decision to determine. The upstream client handler task
    remains census-visible during that decision, while the happy path keeps the
    synchronous reserve-before-effects boundary and pays no routing lookup.
    """
    reserve = getattr(sessions, "reserve_inbound_callback", None)
    if not callable(reserve):
        # Compatibility for focused embedders/test doubles that are not a
        # SessionManager. Production channel dispatch always receives the real
        # facade, whose structural contract requires this method.
        return True
    reservation = reserve()
    if reservation is None:
        try:
            refusal_restricted = bool(await restricted()) if callable(restricted) else restricted
        except Exception:
            logger.warning(
                "%s: could not resolve refused callback privacy; suppressing durable spool",
                channel_type,
                exc_info=True,
            )
            refusal_restricted = True
        if not refusal_restricted:
            if getattr(sessions, "update_restart_fenced", False):
                spool_refused_turn_sync(channel_type=channel_type, route=route)
            else:
                await spool_refused_turn(channel_type=channel_type, route=route)
        return False
    task = asyncio.current_task()
    if task is None:
        reservation.release()
        raise RuntimeError("inbound callback has no owning asyncio task")
    task.add_done_callback(lambda _done: reservation.release())
    return True


class _InboundCallbackAdmission:
    """Explicit callback lease for dispatchers running on a long-lived task."""

    def __init__(
        self,
        sessions: Any,
        *,
        channel_type: str,
        route: InboundRoute | None,
        restricted: bool,
    ) -> None:
        self._sessions = sessions
        self._channel_type = channel_type
        self._route = route
        self._restricted = restricted
        self._reservation: Any = None

    async def __aenter__(self) -> bool:
        reserve = getattr(self._sessions, "reserve_inbound_callback", None)
        if not callable(reserve):
            return True
        self._reservation = reserve()
        if self._reservation is not None:
            return True
        if not self._restricted:
            if getattr(self._sessions, "update_restart_fenced", False):
                spool_refused_turn_sync(channel_type=self._channel_type, route=self._route)
            else:
                await spool_refused_turn(channel_type=self._channel_type, route=self._route)
        return False

    async def __aexit__(self, _exc_type: Any, _exc: Any, _tb: Any) -> None:
        if self._reservation is not None:
            self._reservation.release()


def hold_inbound_callback(
    sessions: Any,
    *,
    channel_type: str,
    route: InboundRoute | None,
    restricted: bool = False,
) -> _InboundCallbackAdmission:
    """Hold callback census ownership only for the surrounding async scope."""
    return _InboundCallbackAdmission(
        sessions,
        channel_type=channel_type,
        route=route,
        restricted=restricted,
    )


@dataclass
class ChannelTurn:
    """Everything the pipeline needs that varies per channel.

    ``persist`` runs in a worker thread (it does blocking history I/O), so it
    must be a plain sync callable. ``notice`` and ``renderer`` are async.
    """

    channel_type: str
    """Governance member name, e.g. ``"weixin"``. Gates every inbound message.

    This equals the first ``:``-segment of ``session_key`` except for the
    ``dm_scope="unified"`` direct-DM bucket, whose key is ``unified:{agent}``.
    The surface name is the routing authority for governance and (future)
    control-plane operations. Every adopter builds its key via
    ``build_dm_session_key(channel_type, ...)``.
    """

    session_key: str
    """The session address. Passed through to ``sessions.*`` verbatim and never
    rebuilt here. The pipeline reads only its namespace
    (``channel_namespace_of``): to tell whether this channel is the session's
    origin when checking mute, and to skip origin/mirror binding on a
    ``unified:`` key. Keys are constructed channel-side via
    :func:`kiro_crew.messaging.link.build_dm_session_key`
    (``{surface}:{agent}:{chat_type}:{scope}[:genN]``, or
    ``unified:{agent}[:genN]`` for a unified direct DM).
    """

    conversation_id: str
    """Stable identity of the conversation on its transport (e.g.
    ``"weixin:{user_id}"``), used for session attribution/UI. Feeds the
    legacy ``channel_id=`` kwarg of ``sessions.get_or_create`` /
    ``set_channel`` — the old name survives at that API boundary only.
    Distinct from BOTH ``channel_type`` (the transport) and the app-platform
    ``channel:`` concept.
    """

    agent: str
    user_text: str
    renderer: Any
    """A :class:`kiro_crew.messaging.renderer.Renderer`."""

    approval_mode: str
    decider: Optional[Any] = None
    """``None`` for channels with no interactive approval affordance
    (deny-by-default for INTERACTIVE mode; ``auto``/``trust`` still work)."""

    auto_approve_session: Optional[Callable[[], bool]] = None
    """``() -> bool`` honoring a per-session Trust / operator YOLO grant.

    Without it, a channel that renders no approve/deny buttons is stuck: the
    INTERACTIVE ladder denies by default and there is no decider to say otherwise,
    so every tool call fails and the agent can only talk. Supplying this lets such
    a channel grant trust out of band (a ``/yolo``-style command) while the deny
    default stays intact for every session that has not opted in — and an operator
    who turned YOLO on no longer finds it silently inert on that channel.

    The grant is read PER REQUEST rather than captured at turn start, so taking or
    revoking it mid-turn takes effect on the next tool. The PreToolUse
    ``tool_gate`` still runs first, so a hard deny can never be overridden by it.

    ``None`` keeps the previous behavior exactly, so channels that do not set it
    are unaffected."""
    minimal_context: bool = False
    """Assemble the prompt WITHOUT the operator's private context.

    ``ContextBuilder.build_message`` gates memory, lessons, skills and prior
    conversation history on this, leaving date/time plus agent identity. Set it
    for a turn driven by someone the channel admits but who is not its operator:
    denying that sender's tools does not help, because the exposure is in the
    PROMPT and is assembled before any tool runs, so the operator's memory and
    profile can otherwise be quoted straight back to a peer.

    Every channel that admits a non-owner (an allowlisted peer, an ``open`` DM
    policy, a group member) has the same exposure, which is why the switch lives
    on the shared seam rather than in one dispatcher. Defaults False, so every
    existing adopter is byte-identical."""

    deny_all_tools: bool = False
    """Reject every tool this turn asks for, ahead of every auto-approve path.

    For a turn driven by someone the channel does not trust as its operator. The
    approval mode cannot express it: the PreToolUse hook may answer
    ``auto_approve`` and a session carrying Trust short-circuits, both before the
    interactive ladder is consulted. Nor is a permission request guaranteed to be
    raised at all: a tool the agent spec lists in ``allowedTools`` runs without
    one on the kiro backend, so the driver never sees it. The turn is therefore
    driven on :data:`TOOLLESS_TURN_AGENT`, whose spec mounts no tools and no MCP
    servers, and the driver's own refusal of any permission request that does
    arrive is the second line. The session key MUST be one that only such turns
    use (a per-peer bucket, never the operator's): a session already bound to
    another agent refuses the turn (:class:`ToollessTurnUnavailable`), and so does
    a backend whose routing is not ``Routing.AGENT_SPEC``, since only a harness
    that mounts what the spec names honours ``tools: []``. Defaults False, so
    every existing adopter is byte-identical."""

    unprompted: bool = False
    """The turn was not addressed to the agent (a rules-mode group message the
    model may answer or decline). A refusal of such a turn ends silently: a
    note nobody asked for is an unsolicited post into the room, and it would
    start the unprompted cooldown for a turn that never ran. Defaults False."""

    bind_provider: Optional[Callable[[Any], None]] = None
    """``(provider) -> None``, called once the session's provider exists.

    The hook for anything a renderer can only be told AFTER the session is
    resolved — a channel that uploads local files needs the provider's own cwd as
    the extraction root, and that is unknowable until ``get_or_create`` returns.
    A channel reading it BEFORE the turn gets ``None`` on the first message of
    every session generation, so the feature is silently off for exactly the turn
    that introduces it and mysteriously on afterwards.

    Guarded like the origin bind: whatever it authorizes is an enhancement to the
    turn, so a failure here degrades that one feature rather than dropping an
    answer the user is waiting for.
    """

    persist: Optional[Callable[[str, str, bool], None]] = None
    """``(user_text, reply_text, is_new) -> None``, called off the event loop."""

    notice: Optional[Callable[[str, Any], Awaitable[None]]] = None
    """``(session_key, provider) -> None`` post-turn threshold handling."""

    after_persist: Optional[Callable[[], Awaitable[None]]] = None
    """Optional loop-side callback after persistence, such as dashboard surfacing."""

    directive_consumer: Optional[DirectiveConsumer] = None
    """Session-directive consumer for this turn (``build_directive_consumer``).
    ``None`` leaves directive-tool results inert — the pre-consumer behavior."""

    model: Optional[str] = None
    """Model id for a NEW session, or ``None`` to let config decide.

    Reaches a session only at CREATION: ``get_or_create`` returns a reused
    session from its fast path before it consults this, so a channel-side model
    pick applies to the next fresh conversation rather than retroactively to the
    running one. A channel that offers a model command has to say so in its reply
    or the user reads the switch as broken.

    Never a hardcoded id — the value comes from what the session's backend
    advertised, which is the set THIS account may actually use.
    """

    origin_conversation: Optional[ChannelLink] = None
    """A :class:`~kiro_crew.messaging.link.ChannelLink` naming THIS conversation.

    Supplying it makes the conversation both the session's origin (so unattended
    output about the session — the auto-compact notice — has somewhere to go) and
    its own outbound mirror (so a turn the user later takes from the dashboard
    comes back here instead of leaving the chat looking dead).

    ``None`` means the channel opts out, and its conversations stay unmirrored.
    It is also ignored for a ``unified:`` key, which has no single conversation
    to bind. Both writes are steady-state READS after the first turn, so this costs a map
    lookup per turn rather than a rewrite; see
    :func:`kiro_crew.messaging.link.bind_origin_mirror` for why the bind must be
    re-asserted on every turn rather than only on a new session, and for the
    binding it deliberately declines to overwrite.

    It MUST be the same value the channel's own unlink command hands
    ``release_conversation_location``, which matches an occupied location by
    VALUE — a second spelling of "this conversation" would let the release miss
    the binding this wrote. Channels define it once and reuse it for both.
    """

    audit_caller: str = ""
    """SEL audit caller label; defaults to ``<channel_type>:unknown``."""

    inbound_route: Optional[InboundRoute] = None
    """How to reach this conversation if the SHUTDOWN GATE refuses the turn.

    Supplying it opts the channel into the durable inbound spool: a turn refused
    by ``_closing`` is written to disk with this route and replayed on the next
    gateway start (:mod:`kiro_crew.messaging.inbound_spool`), instead of the
    payload being discarded and the user answered with a generic fault.

    It cannot be derived from :attr:`conversation_id`, which is a session
    ATTRIBUTION id (``"weixin:{user}"``) rather than a reply target, so a channel
    has to declare its own address here -- it is the only place holding the
    normalized envelope. ``None`` (the default) means the channel has not adopted
    the spool and its refusal path is byte-identical to before.
    """

    inbound_restricted: bool = False
    """Suppress durable refusal spooling for an incognito/temporary session.

    The dispatcher resolves this against the final session key before entering
    the shared turn pipeline. It covers the later ``SessionClosingError`` race,
    after callback admission succeeded but before the provider turn opened.
    """

    user_display_name: Optional[str] = None
    """Human name of the sender, injected as ``[CURRENT USER]`` so the agent
    knows who it is talking to. ``None`` omits the block (byte-identical to before)."""

    start_priority: StartPriority = StartPriority.BACKGROUND
    """The cold start's place in the start queues: FOREGROUND only for a message a
    person sent (``person_origin`` on the inbound; rule ``kiro_crew.start_priority``)."""


#: Every spelling a channel accepts for "abort the running turn". The union of
#: the per-channel command tables (``/stop`` and ``/cancel`` everywhere, plus
#: Discord's ``!`` bang forms and WeCom's ``停止``), owned here because the
#: governance exemption below is channel-neutral.
#:
#: This is a MIRROR of data that lives in the channel packages, so it cannot be
#: kept correct by intention alone — and it already drifted once: WeCom shipped
#: ``停止`` in its own table and on its ``/help`` card while this set knew only
#: the ASCII spellings, so a denied WeCom conversation had no reachable
#: off-switch in the language that channel exists for. Deriving the union here
#: would mean the shared layer importing all nine channel packages, which
#: inverts the dependency it exists to hold. So the tripwire lives in
#: ``test_messaging_dispatch.py``, and it DISCOVERS the channel tables rather
#: than listing them: the earlier version checked Discord and Telegram by hand
#: and was blind to the three channels that actually diverged.
_CANCEL_ALIASES = frozenset(("/stop", "/cancel", "!stop", "!cancel", "停止"))


def is_pure_cancel(text: str, *, has_attachments: bool = False) -> bool:
    """Whether *text* is nothing but a cancellation, carrying nothing with it.

    PURE is the load-bearing word, and it is why the match is exact rather than a
    prefix or a search:

    * **Whole message.** A cancel alias with anything else on the line is an
      ordinary message that happens to start with one, and every channel's own
      ``parse_command`` already matches only the whole message.
    * **No attachments.** A channel fetches media AFTER it authorizes the message,
      so exempting an attachment-bearing cancel would spend an authenticated
      download, a transcription, and a ``channel_history`` write on a conversation
      policy has denied -- the exact leak the gate exists to stop -- before any
      turn is refused. Such a message is gated like any other.

    Case- and whitespace-insensitive, matching the per-channel tables.
    """
    if has_attachments:
        return False
    return text.strip().lower() in _CANCEL_ALIASES


async def inbound_permitted(
    channel_type: str, *, text: str = "", has_attachments: bool = False
) -> bool:
    """Per-message governance gate.

    Rechecked on every message (not just at connect) so a host-profile deny
    added while the transport is live stops dispatch without a restart. The
    pipeline calls this itself, so a channel cannot forget it.

    A PURE cancellation is the one exemption, matching the native Slack route's
    ``!stop`` carve-out: a denied channel must still be able to halt a runaway
    session it already STARTED, and on a channel with no interactive buttons
    (``max_buttons=0``) the typed cancel is the only affordance there is, so
    gating it makes the off-switch unreachable exactly when it is needed. Nothing
    else is exempt -- a restart is not a cancellation.

    Callers pass *text* and *has_attachments* only where a cancel can arrive: the
    dispatcher's per-message entry, ahead of command parsing. The defaults leave
    the gate strict, which is what keeps the pipeline's backstop a backstop -- a
    cancel runs no turn, so it never reaches it.
    """
    if await channel_inbound_permitted(channel_type):
        return True
    if text and is_pure_cancel(text, has_attachments=has_attachments):
        # Logged, not silent: an operator reading the trail needs to see that a
        # governed-off channel was still allowed to stop its own session.
        logger.info("%s cancellation allowed through a channels governance deny", channel_type)
        return True
    logger.info("%s inbound dropped: denied by channels governance policy", channel_type)
    return False


def build_tool_gate(ctx_builder: Any, *, session_key: str, agent: str) -> Callable[[Any], str]:
    """PreToolUse security gate, channel-neutral (off ``ctx_builder.hooks``).

    Sensitive-path keystone + governance ceiling + deny-list. Returns ``"deny"``
    (un-overridable), ``"auto_approve"``, or ``""`` (passthrough). Built here so
    no channel package needs to import ``kiro_crew.slack``.

    The gate also carries ``last_deny_reason``: the hook's own reason for the
    most recent ``"deny"`` and ``""`` otherwise, set on every call. The
    ``TurnDriver`` reads it after a deny and steers it into the running turn
    before the reject, so the model learns which rule blocked the call instead
    of reading kiro-cli's generic "User denied tool execution" -- the same
    attribute-on-a-callable shape ``ApprovalDecider.last_deny_cause`` uses. A
    plain callable without the attribute is a deny with no reason, as before.
    """

    def _tool_gate(event: Any) -> str:
        result = ctx_builder.hooks.on_tool_call(
            getattr(event, "title", "") or "",
            session_key=session_key,
            agent=agent,
            **hook_gate_kwargs(event),
        )
        denied = result.action == TOOL_DENY
        reason = str(getattr(result, "reason", "") or "") if denied else ""
        _tool_gate.last_deny_reason = reason  # type: ignore[attr-defined]
        if denied:
            return "deny"
        if result.action == TOOL_AUTO_APPROVE:
            return "auto_approve"
        return ""

    _tool_gate.last_deny_reason = ""  # type: ignore[attr-defined]
    return _tool_gate


def build_auto_approve(ctx_builder: Any) -> Callable[[Any], bool]:
    """Preserve the ``auto_approve_subagent_spawn`` hook for ``spawn_run``.

    The predicate takes the PERMISSION EVENT, not the title: the title is
    model-authored, so the spawn check keys on ``event_is_spawn_run``'s
    canonical identity (``tool_name`` from ``_meta.kiro``; without it the
    rung does not fire and the request falls to the approval ladder).
    """

    def _auto_approve(event: Any) -> bool:
        return bool(
            ctx_builder
            and ctx_builder.hooks
            and ctx_builder.hooks.auto_approve_subagent_spawn
            and event_is_spawn_run(event)
        )

    return _auto_approve


@dataclass
class _ChannelDirectiveState:
    """Minimal ``NudgeAuthzState`` stand-in for a turn with no gateway state.

    Carries the one thing the monitor-trio authorizer can validate for a
    channel session — ``sessions`` (Slack routability). The empty ``_slots`` /
    ``channel_transports`` make every other lookup fail CLOSED (deny), never
    crash.
    """

    sessions: Any
    channel_transports: dict[str, Any] = field(default_factory=dict)
    _slots: dict[str, Any] = field(default_factory=dict)


def build_directive_consumer(
    *,
    session_key: str,
    sessions: Any,
    dispatcher: Any = None,
) -> DirectiveConsumer:
    """Session-directive consumer for one channel turn (``TurnDriver`` injection).

    Applies a decoded directive against THIS turn's *session_key* via the shared
    ``apply_session_directive`` core — the same applier the dashboard's
    ``chat_runner`` consumer uses, so the security boundaries (the
    dashboard-only denial, the monitor-trio authorization chokepoint) live in
    exactly one place. A channel turn has no dashboard chat slot, so
    ``slot=None`` is passed and the dashboard-only directives are refused there
    (fail-closed).

    *dispatcher* supplies the live gateway state: each channel dispatcher gets
    ``dashboard_state`` attached at boot (``register_channel_transport``), and
    it is re-read per directive so a consumer built before that attachment
    still sees it. Slack's function-style dispatch has no dispatcher object;
    the minimal *sessions*-backed stand-in covers the Slack routability check,
    and everything it cannot answer fails CLOSED in the authorizer.
    """

    async def _consume(kind: str, args: dict[str, Any]) -> None:
        # Deferred import: the dashboard package imports every channel package
        # at boot, and the channel packages import this module (cycle).
        from kiro_crew.dashboard.session_directive_apply import apply_session_directive

        state: Any = getattr(dispatcher, "dashboard_state", None)
        if state is None:
            state = _ChannelDirectiveState(sessions=sessions)
        result = await apply_session_directive(
            state,
            None,
            session_key,
            kind,
            args,
            producer_is_channel=True,
        )
        # The channel surface never renders tool results, so the applier's
        # confirmation has no user-facing sink here; this log is the operator's
        # record (the applier itself SEL-audits every outcome). Failures log at
        # WARNING because a silently dropped effect is the defect this consumer
        # exists to remove. The string interpolates LLM-derived text (a stop
        # reason, a rejected path, exception args), so scrub it like every
        # other LLM-influenced output before it lands anywhere.
        result, _ = redact_exfiltration_urls(result)
        result, _ = redact_credentials(result)
        log = logger.warning if result.startswith(("Error", "Failed")) else logger.info
        log("session directive %s on %s: %s", kind, session_key, result)

    return _consume


def delivery_is_muted(sessions: Any, session_key: str, channel_type: str) -> bool:
    """True when output to *channel_type* must NOT be written back for this session.

    The primitive behind :func:`conversation_is_muted`, taking explicit arguments
    because :class:`ChannelTurns` reads it without a ``ChannelTurn`` and Telegram
    runs its OWN copy of the turn loop. Every channel that can be disconnected
    must consult this, or the dashboard control is a label with nothing behind it.

    ``origin`` is resolved rather than passed because a session can hold two
    non-Slack deliveries at once, and they mute independently: the conversation
    it was BORN in, and an explicit mirror it was told to post to. This turn came
    from the born-in conversation exactly when the session key IS a channel key
    in this turn's own namespace — a channel-born session's key is its
    conversation. Anything else arriving here is a mirror/resume binding, so it
    reads the mirror flag.

    Slack never reaches these pipelines (it drives its own gateway and is gated by
    ``slack_mirror_is_paused``), so no Slack special-case is needed here.

    Fails OPEN, matching the dashboard-side predicates: ``sessions`` is a bare
    ``MagicMock`` across much of the suite and would return a truthy child for an
    unstubbed accessor, which would silence every channel in the test suite. A
    muted conversation that stays noisy is a visible bug; a live conversation
    silently dead is a much worse one.
    """
    origin = is_channel_session_key(session_key) and (
        channel_namespace_of(session_key) == channel_type
    )
    try:
        return sessions.is_mirror_paused(session_key, origin=origin) is True
    except Exception:
        logger.debug(
            "%s: mirror pause lookup failed session=%s",
            channel_type,
            session_key,
            exc_info=True,
        )
        return False


def conversation_is_muted(sessions: Any, turn: ChannelTurn) -> bool:
    """:func:`delivery_is_muted` for a turn on the shared pipeline."""
    return delivery_is_muted(sessions, turn.session_key, turn.channel_type)


def predecessor_sid(sessions: Any, session_key: str) -> str:
    """The crew log *session_key*'s live session superseded -- read AFTER the allocation.

    The ``previous_sid`` producer for :func:`open_turn_crew_log`. It does not read
    the slot-to-session mapping at all: it returns what the allocation boundary
    captured for the key (``SessionManager.allocation_predecessor``), which the
    boundary reads under its own lock, in the same tick that registers a
    cold-started session and before that session's id is mapped. No read taken
    around ``get_or_create`` can stand in for that: a caller reading the mapping
    before its call can be suspended INSIDE the allocation, waiting for the turn
    permit, while a concurrent turn on the same key allocates an intermediate
    session and has it recycled by a failed compaction -- the caller's value then
    names the store before that intermediate one, its successor cites its
    grandparent, and the intermediate log falls off the succession chain. Read
    after the call, the mapping already names the successor itself. The
    boundary's capture is the only read that is neither too early nor too late,
    so this is consumed right after ``get_or_create`` returns, while this turn
    holds the key's permit.

    The emitter does the comparing: a warm claim hands back the value its live
    session was registered with and the log already exists, so nothing is
    written; only the creation of a cold successor's log cites its predecessor,
    and only after the emitter has checked that predecessor's header names the
    same slot. Best-effort: a store without the reader answers ``""``, which the
    emitter reads as "nothing to follow".
    """
    reader = getattr(sessions, "allocation_predecessor", None)
    if not callable(reader):
        return ""
    try:
        return str(reader(session_key) or "")
    except Exception:
        logger.debug("crew log: predecessor unreadable for %s", session_key, exc_info=True)
        return ""


def requested_model_sid(sessions: Any, session_key: str) -> str:
    """The model *session_key*'s live allocation SELECTED, or ``""`` -- read after the claim.

    The ``model_requested`` half of the requested/served pair the ``session/opened``
    entry records. The dispatcher's own choice is not the whole story: a call handed
    ``model=None`` has the allocation resolve an id from config itself, and that
    resolution is invisible in ``get_or_create``'s return, so only the stamp the
    allocation left on the session (``SessionManager.allocation_requested_model``,
    the value the provider was constructed with) can say what was asked for. The
    dashboard runner records the same stamp; without it a channel session's log
    would carry the served model alone and lose the selected side of the pair for
    good, the entry being append-only. Best-effort like :func:`predecessor_sid`: a
    store without the reader answers ``""``, which the emitter records as "no
    selection to report".
    """
    reader = getattr(sessions, "allocation_requested_model", None)
    if not callable(reader):
        return ""
    try:
        return str(reader(session_key) or "")
    except Exception:
        logger.debug("crew log: requested model unreadable for %s", session_key, exc_info=True)
        return ""


def slot_workspace(dashboard_state: Any, session_key: str) -> str:
    """The workspace the dashboard states for *session_key*'s conversation, or ``""``.

    The ``workspace`` producer for :func:`open_turn_crew_log`, and the SAME source
    the dashboard runner's writer reads: ``chat_runner._crew_log_workspace`` states
    ``slot.workspace`` off the live slot, and a channel conversation's slot is the
    one the dashboard surfaces it under -- ``channel_slot_name(session_key)``, the
    channel key folded to the filename charset, which is the ``slot`` field this
    opener already records (:func:`transcript_stem` spells the same fold). A tab
    opened on that conversation writes its ``session/opened`` from that slot, into
    the same crew log this dispatcher writes, and the emitter appends a
    ``session/class`` line whenever the class it is handed differs from the last
    one stated -- so if the two writers named different workspaces for one
    session, every switch between them would record a move that never happened.
    Reading the slot the other writer reads is what makes the two statements one.

    ``""`` when the conversation has no slot yet -- a channel slot is surfaced
    after its first persisted turn, so the log's opening entry states no workspace
    -- or when the gateway state is not attached. An unstated workspace is "not
    observed": the class fold holds the first workspace STATED and records a later
    different one as a move, so nothing is guessed here for the slot to contradict.
    Only a ``str`` counts as a statement, so a state double answering with an
    object of another shape states nothing rather than its ``repr``.
    """
    if dashboard_state is None:
        return ""
    try:
        getter = getattr(dashboard_state, "get_slot", None)
        slot = getter(transcript_stem(session_key)) if callable(getter) else None
    except Exception:
        logger.debug("crew log: slot unreadable for %s", session_key, exc_info=True)
        return ""
    workspace = getattr(slot, "workspace", "") if slot is not None else ""
    return workspace if isinstance(workspace, str) else ""


def open_turn_crew_log(
    provider: Any,
    *,
    session_key: str,
    agent: str,
    resumed: bool,
    ctx_builder: Any = None,
    previous_sid: str = "",
    model_requested: str = "",
    workspace: str = "",
) -> None:
    """Open the channel session's crew log ahead of its turn, as the dashboard runner does.

    ``crew_log_emit.on_session_opened`` is what CREATES a session's crew log, keyed
    by its ACP session id; ``chat_runner._run_chat`` calls it on every dashboard turn
    once the handle exists, and a warm reuse is silent. A channel conversation is
    not a dashboard turn, and without this call it opens no log at all.
    That is a hole the work ledger falls into: the ledger is a projection of the
    crew log, every ``work_ledger_record`` / ``work_report`` write appends one
    ``work/recorded`` entry to the ACTING session's log, and a write with nowhere
    to append is rolled back and refused (``crew_log_unrecorded``). An owner DM
    that session control admits as a conductor therefore reached the ledger and
    lost every write to it. Opening the log here, before ``TurnDriver.run``, is
    what makes that admission usable. Free while the emitter is off -- the emitter
    checks its own flag -- and it never raises, because the turn must not be lost
    to its own record.

    Only facts the dispatcher can establish are recorded; the emitter reads an
    absent field as "not observed", never as false. The ACP session id comes off
    *provider* (no id, no log: a turn that never got a session emits nothing, as
    on the dashboard). ``slot`` is the key the dashboard surfaces this conversation
    under -- the channel key folded to the filename charset, which is what
    ``channel_slot_name`` spells and what ``session_create`` stamps as
    ``_created_by`` on the workers this session dispatches, so the session tree
    joins the two. The served model and the cwd are read off the provider, the
    dashboard's own sources for them, and ``model_requested`` is the allocation's
    stamp of what was SELECTED (:func:`requested_model_sid`, read after the claim
    like the predecessor) -- the pair the entry records, since a call handed
    ``model=None`` has the allocation resolve the id itself and nothing else can
    say what it chose; ``resumed`` is ``get_or_create``'s answer.
    The class is stated only when the memory mode is known, from the gateway's
    live policy for the key (``ctx_builder.live_memory_mode_for_session``, wired by
    the dashboard state; a builder without it states no class, which readers
    refuse rather than assume), and it carries ``channel=True`` because a
    channel-born conversation is published to its channel by definition -- the
    same reading ``_crew_log_class`` takes off a linked slot. ``workspace`` is
    stated the way the dashboard writer states it, off the slot the dashboard
    surfaces this conversation under (:func:`slot_workspace`), because a tab on
    the conversation writes into this same log and the emitter records a
    ``session/class`` move whenever two statements of one session's class differ:
    every member the dashboard states, this opener states from the same source, or
    the two writers would take turns recording a move that never happened. No
    ``parent``: a
    conversation the person opened themselves is nobody's child. ``previous_sid``
    is the crew log this conversation's live session superseded, as the
    allocation boundary captured it while registering that session
    (:func:`predecessor_sid`, consumed by the dispatcher right after
    ``get_or_create`` returns): the emitter writes the ``previous`` edge only when
    it creates a log that names a different store, which is what keeps a
    conversation's history reachable across the cold successor a failed
    compaction leaves behind.

    For the channel's OWN sessions only. A dashboard session resumed into the chat
    (``!sessions``) is opened by the dashboard runner, which alone holds its
    lineage: an opener from here would create that log without its ``parent``.
    """
    # Imported here, not at module scope, on purpose: this module is on the
    # dashboard's boot path (``dashboard.handlers.crew_log`` reaches it through the
    # handlers package -> ``handlers.taskrunner`` -> ``taskrunner`` ->
    # ``task_executor``), and the crew log is optional -- a flag-off launch must
    # not load the storage package.
    # ``test_crew_log_routes.py::test_this_module_does_not_load_the_storage_package_at_import``
    # pins that from a clean interpreter and fails when this moves up; it is not a
    # circular import. ``handlers/crew_log.py`` and
    # ``work_ledger.rebuild_from_projection`` import the emitter the same way. The
    # ``top-level-imports`` convention is advisory; this boot-path invariant is
    # enforced, so the invariant wins.
    from kiro_crew.crew_log import emit as crew_log_emit

    try:
        session_id = crew_log_emit.session_id_of(provider)
        if not session_id:
            return
        memory_mode = ""
        live_mode = getattr(ctx_builder, "live_memory_mode_for_session", None)
        if callable(live_mode):
            try:
                memory_mode = str(live_mode(session_key) or "")
            except Exception:
                logger.debug("crew log: memory mode unreadable for %s", session_key, exc_info=True)
        crew_log_emit.on_session_opened(
            session_id,
            agent=agent or "",
            slot=transcript_stem(session_key),
            model=str(getattr(provider, "served_model", "") or ""),
            model_requested=model_requested,
            cwd=str(getattr(provider, "cwd", "") or ""),
            resumed=bool(resumed),
            memory=memory_mode,
            channel=True,
            workspace=workspace,
            previous_sid=previous_sid,
        )
    except Exception:
        logger.debug("crew log: opener skipped for %s", session_key, exc_info=True)


def hook_auto_reply(ctx_builder: Any, text: str) -> str | None:
    """The canned answer a user-defined ``on_message`` hook gives *text*, else None.

    ``None`` means no hook claimed the message (passthrough, modify, context
    injection, or no hooks at all), so the caller runs a normal turn. A string --
    including an empty one -- means a hook ANSWERED it and the turn must not run:
    that is the whole point of an auto-reply, and running the model anyway would
    both contradict the operator's rule and bill them for it.

    The text is redacted here because this path skips :class:`TurnDriver`, which
    is what redacts everything else on its way to a channel. The pair applied is
    the driver's own (exfiltration URLs, then credentials); mention syntax is
    deliberately NOT defanged, because a hook reply is operator-authored config
    rather than model or remote output, so an ``@name`` in it is intended.

    Every lookup is defensive: the hook manager is optional on this seam, and a
    channel that supplies a context builder without one must fall through to a
    normal turn rather than fail the message.

    Asking the hooks here means ``build_message`` asks them again on the turn
    path, which is what Slack does too and is safe because ``on_message`` is a
    pure pattern match over the text. The alternative -- reading the hook result
    ``build_message`` already returns -- is too late: by then the session has been
    cold-started, which is the cost an auto-reply exists to avoid.
    """
    hooks = getattr(ctx_builder, "hooks", None)
    on_message = getattr(hooks, "on_message", None)
    if not callable(on_message):
        return None
    result = on_message(text)
    if getattr(result, "action", "") != HOOK_REPLY:
        return None
    return redact(str(getattr(result, "text", "") or ""))


# Retries granted to a turn abandoned after a TRANSIENT compaction failure (a
# throttled or 5xx'd summarization call). Per turn, so the budget is
# the turn's own and a throttle that keeps firing costs a bounded number of
# cold starts. Same count as the dashboard's _COMPACTION_FAILED_RETRIES and for
# the same reason: a throttle still firing after two session resets is not
# clearing inside this turn, and every attempt costs the summarization call
# again.
_COMPACTION_FAILED_RETRIES = 2


#: Event kinds after which a verbatim replay is unsafe: text or a tool
#: call has landed (replay could repeat a side effect), a permission prompt was
#: shown, or a mid-turn steer was folded into the turn (replaying
#: ``user_text`` would drop the accepted correction).
_EMITTED_KINDS = frozenset({TEXT_CHUNK, TOOL_CALL, PROMPT_CHOICE, STEER_CONSUMED})


class _TransientCompactionRetryGuard(Renderer):
    """The renderer the driver sees while :class:`ChannelTurns` may still retry.

    A turn abandoned after a transient compaction failure is replayed INSIDE the
    same turn, into the same renderer: the shared pipeline has no queue of its
    own to put the message back on (five of the channels riding it keep none),
    and the channel's renderer is one message's output
    surface that finalizes on its first DONE -- so a completion delivered for
    the abandoned attempt would close the reply before the replay could write
    it. This guard therefore holds that one DONE back, and only that one:

    * the completion must be ``STOP_REASON_COMPACTION_FAILED``;
    * nothing may have been emitted through the guard this turn -- verbatim
      replay is only safe before any text, tool call or permission prompt has
      landed, exactly the guard the dashboard's transient siblings use. A
      consumed mid-turn steer counts too: the backend folded a correction the
      replayed ``user_text`` does not carry, so re-running the original prompt
      would silently discard what the user was told was accepted;
    * the provider must report ``last_compaction_transient`` as ``True`` --
      compared against ``True``, not read for truthiness, so a provider that
      never set the attribute (or exposes an auto-created stand-in for it)
      cannot read as transient by accident;
    * a retry must still be available.

    Every other event passes straight through, and a held DONE is released
    (``release_held``) when the pipeline decides not to replay after all.

    Whole events are forwarded through the inner renderer's own ``dispatch``
    rather than routed to its ``on_*`` handlers from here, so the bookkeeping
    that method does on the way (``current_tool_name``) lands on the object
    whose handlers read it. The ``on_*`` delegates below exist because the
    contract declares them abstract; the driver itself reaches its renderer
    through ``dispatch`` and ``on_turn_start`` only.
    """

    def __init__(self, renderer: Any) -> None:
        # Same defensive read as the SilentRenderer substitution: the turn's
        # renderer is typed ``Any`` and must not fail to wrap for lacking it.
        capabilities: Any = getattr(renderer, "capabilities", None)
        super().__init__(capabilities)
        self.channel_type = getattr(renderer, "channel_type", "") or ""
        #: The channel's renderer, which every forwarded event reaches.
        self.inner = renderer
        #: The provider of the CURRENT attempt; reassigned by the pipeline after
        #: each ``get_or_create``, since a reset replaces it.
        self.provider: Any = None
        self.emitted = False
        self.retries_used = 0
        self._held_done: OutputEvent | None = None

    @property
    def held(self) -> bool:
        """Whether the last completion was withheld pending a replay."""
        return self._held_done is not None

    async def on_turn_start(self) -> None:
        await self.inner.on_turn_start()

    async def close(self) -> None:
        await self.inner.close()

    async def on_text_chunk(self, text: str) -> None:
        self.emitted = True
        await self.inner.on_text_chunk(text)

    async def on_thinking(self, text: str) -> None:
        await self.inner.on_thinking(text)

    async def on_tool_call(
        self, tool_call_id: str, title: str, tool_kind: str = "", tool_purpose: str = ""
    ) -> None:
        self.emitted = True
        await self.inner.on_tool_call(tool_call_id, title, tool_kind, tool_purpose)

    async def on_prompt_choice(
        self,
        options: list[dict[str, Any]],
        request_id: str | int,
        tool_title: str = "",
        tool_purpose: str = "",
        tool_input: str = "",
    ) -> None:
        self.emitted = True
        await self.inner.on_prompt_choice(options, request_id, tool_title, tool_purpose, tool_input)

    async def on_compaction(self, context_usage_pct: float) -> None:
        await self.inner.on_compaction(context_usage_pct)

    async def on_done(self, stop_reason: str = "") -> None:
        await self.inner.on_done(stop_reason)

    async def on_steer_consumed(self, summary: str = "") -> None:
        self.emitted = True
        await self.inner.on_steer_consumed(summary)

    async def dispatch(self, event: OutputEvent) -> None:
        if event.kind == DONE and self._should_hold(event):
            self._held_done = event
            self.retries_used += 1
            return
        if event.kind in _EMITTED_KINDS:
            self.emitted = True
        await self.inner.dispatch(event)

    def _should_hold(self, event: OutputEvent) -> bool:
        return (
            event.stop_reason == STOP_REASON_COMPACTION_FAILED
            and not self.emitted
            and getattr(self.provider, "last_compaction_transient", False) is True
            and self.retries_used < _COMPACTION_FAILED_RETRIES
        )

    async def release_held(self) -> None:
        """Deliver the withheld completion: the replay is not happening."""
        held, self._held_done = self._held_done, None
        if held is not None:
            await self.inner.dispatch(held)

    def drop_held(self) -> None:
        """Forget the withheld completion: the replay's own DONE supersedes it."""
        self._held_done = None


def session_stop_generation(sessions: Any, session_key: str) -> int:
    """The session manager's user-Stop count for *session_key*, read defensively.

    ``SessionManager.stop_turn`` and ``note_stop`` bump it before anything is
    awaited, on every surface that can stop the session. A turn snapshots it
    when it acquires its session and treats any later change as a user Stop --
    the same reading the dashboard runner takes. Doubles for ``sessions`` may
    lack the method or answer with a non-int; both read as 0, so a stand-in
    predating the counter never turns a missing attribute into a stopped turn.
    """
    reader = getattr(sessions, "stop_generation", None)
    if not callable(reader):
        return 0
    try:
        value = reader(session_key)
    except Exception:
        return 0
    return value if isinstance(value, int) else 0


def session_conversation_generation(sessions: Any, session_key: str) -> int:
    """The highest generation persisted for *session_key*'s conversation bucket.

    ``/new`` on every channel of this pipeline advances the conversation's
    generation and persists it (``reserve_new_generation`` ->
    ``SessionManager.reserve_generation``) BEFORE acknowledging, so a key whose
    bucket has since grown a higher generation is a retired conversation. A turn
    snapshots this when it acquires its session and treats any later increase
    as supersession -- the same reading it takes of the Stop counter. Keys
    without a generation grammar (a Slack thread) and doubles lacking the reader
    both read as 0, so neither can turn into a false supersession.
    """
    parsed = split_dm_session_key(canonical_key(session_key))
    reader = getattr(sessions, "max_generation", None)
    if parsed is None or not callable(reader):
        return 0
    try:
        value = reader(parsed[0])
    except Exception:
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


async def await_replay_gap(sessions: Any, session_key: str) -> None:
    """Wait out an open replay gap on *session_key* before writing its transcript.

    ``get_or_create`` waits on the gap for every turn that acquires a session,
    but a hook auto-reply never acquires one: it answers from the context
    builder's hooks and persists the exchange straight away. Arriving while an
    older message on the same key sits between its reset and its replay, that
    persist would land in the transcript AHEAD of the replayed turn -- the
    reader saw the older message first, the record would say otherwise. So the
    hook paths wait here, right before their persist, the same way the
    allocation path waits before its claim. The gap owner's own task passes
    straight through, and a session stand-in without the method (the focused
    doubles across the suite) waits for nothing.
    """
    waiter = getattr(sessions, "await_replay_gap", None)
    if callable(waiter):
        await waiter(session_key)


def _set_replay_gap(sessions: Any, session_key: str, *, opened: bool) -> None:
    """Open or close the manager's replay gap for *session_key*, if it has one.

    While the gap is open a Stop that finds no live session is still recorded,
    and any OTHER task's ``get_or_create`` for the key waits -- so a newer
    message arriving between the reset and the replay's reacquire claims the
    successor after the replay, not ahead of it (``SessionManager.open_replay_gap``).
    The pipeline opens it before the first reset that precedes a replay and
    closes it only when the whole turn has settled and its permit is released:
    a waiter admitted earlier would park on the successor's semaphore, and a
    further retry's reset would pop that session from under it, stranding the
    message for good. Probed with ``getattr`` for the same reason as the reader
    above.
    """
    method = getattr(sessions, "open_replay_gap" if opened else "close_replay_gap", None)
    if callable(method):
        try:
            method(session_key)
        except Exception:
            logger.debug(
                "replay gap %s failed for %s",
                "open" if opened else "close",
                session_key,
                exc_info=True,
            )


def _toolless_turn_work_dir(session_key: str) -> Any:
    """The isolated cwd a ``deny_all_tools`` turn cold-starts in.

    The per-session work directory under the workspace root: created on first
    use, owned by this session key alone, and never a project checkout, so no
    ``.kiro/agents`` entry there can shadow the tool-less spec.
    """
    from kiro_crew.config.loader import _session_work_dir

    path = _session_work_dir(session_key)
    path.mkdir(parents=True, exist_ok=True)
    return path


def _provider_backend(provider: Any) -> str | None:
    """The ACP backend id a provider drives, ``None`` when it cannot be read.

    Read through ``provider.client.backend`` with ``getattr`` at both hops, the
    same mock-safe shape ``session._is_claude_backend`` uses. ``None`` (not
    ``""``) is the unreadable answer: the empty string IS ``ACP_BACKEND_KIRO``,
    so collapsing an absent client onto it would route an unknown harness as the
    one that honours the spec. The caller treats ``None`` as refused.
    """
    client = getattr(provider, "client", None)
    if client is None:
        return None
    backend = getattr(client, "backend", None)
    return backend if isinstance(backend, str) else None


def _refuse_unless_toolless(sessions: Any, session_key: str, provider: Any) -> None:
    """Raise :class:`ToollessTurnUnavailable` unless this turn can be tool-less.

    A ``deny_all_tools`` turn runs on :data:`TOOLLESS_TURN_AGENT`, and that holds
    only on a backend that mounts what the spec names, for a session bound to that
    agent. Each refusal writes its SEL row first.
    """
    # The tool-less agent is a SPEC, and only a backend that mounts
    # what the spec names honours it. On any other routing the
    # harness keeps its own native tools, and one a project has
    # pre-approved runs with no permission request for the driver to
    # refuse -- so the turn is refused instead. Positive identity:
    # the routing that holds, never the absence of another harness.
    backend = _provider_backend(provider)
    if backend is None or not toolless_turns_supported(backend):
        sel().log_api_access(
            caller=session_key,
            operation="turn_agent",
            outcome="denied",
            source="messaging",
            resources=(
                f"deny_all_tools turn on backend={'unknown' if backend is None else backend!r}: "
                "the tool-less agent spec is not honoured there"
            ),
        )
        raise ToollessTurnUnavailable(
            "deny_all_tools turn refused: this backend does not mount tools "
            "from the agent spec, so an untrusted sender's turn cannot be "
            "made tool-less on it"
        )
    # ``get_or_create`` ignores ``agent`` for a session that already
    # exists, so a key shared with a tooled session would silently run
    # this turn with the operator's tools. Read the binding back and
    # refuse rather than trust the key's shape.
    bound = sessions.get_agent(session_key) if hasattr(sessions, "get_agent") else ""
    if bound and bound != TOOLLESS_TURN_AGENT:
        sel().log_api_access(
            caller=session_key,
            operation="turn_agent",
            outcome="denied",
            source="messaging",
            resources=f"deny_all_tools turn on a session bound to agent={bound!r}",
        )
        raise ToollessTurnUnavailable(
            "deny_all_tools turn refused: its session is bound to an agent "
            "with tools; channels must key untrusted turns separately"
        )


def _breaker_threshold(sessions: Any) -> int | None:
    """The circuit breaker's OWN trip threshold, read from the manager applying it.

    Deliberately not a literal here. The number is handed to the allocation layer
    through ``AllocationConstants``, so a copy in this module would be a second
    value to keep in step with the counter the bound below stands in for -- and
    importing the one definition is not available either, because this module
    stays off the session package's import graph (see ``SessionClosingError``
    above). Reading it from the manager is therefore the only way to be sure the
    substitute bound and the real counter share a limit.

    ``None`` when it cannot be read, and the caller then charges exactly as it
    does today: an exemption whose bound is unknown is not an exemption.
    """
    build = getattr(sessions, "_allocation_deps", None)
    if not callable(build):
        return None
    try:
        threshold = build().constants.circuit_breaker_threshold
    except Exception:
        return None
    if isinstance(threshold, bool) or not isinstance(threshold, int) or threshold <= 0:
        return None
    return threshold


async def charge_turn_failure(
    sessions: Any,
    session_key: str,
    *,
    exc: BaseException,
    provider: object | None,
    channel_type: str,
) -> None:
    """Charge one failed turn to *session_key*'s breaker, unless a SHARED process died.

    The channel dispatchers catch a failed turn generically, so a dying runtime
    reaches them as one more exception and every tenant of that process charges
    its own breaker for it -- the misattribution :mod:`kiro_crew.runtime_death`
    exists to end, arriving by a path no typed handler covers. One helper rather
    than one copy per channel: four copies of an attribution rule drift, and the
    rule is identical because the counter is.

    Only a process death is ever exempt. Every other failure is the turn's own
    and charges exactly as before -- consulting the death record for an unrelated
    exception would exempt a real fault whenever some co-tenant's death happened
    to be recorded against the same provider.

    *provider* must be the one this turn ACQUIRED, never a fresh lookup. The
    recovery around these handlers replaces a dead session, so a lookup at
    failure time answers for the replacement and the question silently becomes
    "was the NEW runtime shared".

    The exemption is bounded, and at the limit it PERFORMS the actuator rather
    than charging the counter it stood in for. ``record_failure`` trips into this
    same reset, so a session on a permanently dying shared runtime recovers after
    the threshold rather than after twice it, and its own failure count is left
    alone -- it never misbehaved.
    """
    threshold = _breaker_threshold(sessions)
    if (
        threshold is None
        or not is_runtime_death(exc)
        or runtime_death.caused_by_this_session(provider)
    ):
        await sessions.record_failure(session_key)
        return
    streak = runtime_death.note_shared_death(session_key)
    if streak < threshold:
        logger.warning(
            "%s: %s lost a turn to a SHARED runtime's death (%d running) — "
            "not counting it toward the circuit breaker",
            channel_type,
            session_key,
            streak,
        )
        return
    logger.warning(
        "%s: the runtime %s shares has died %d times running — resetting it now, "
        "the same recovery the breaker performs",
        channel_type,
        session_key,
        streak,
    )
    try:
        await sessions.reset(session_key)
        # Same transfer rule as the two typed hand-overs: the reset spends the
        # streak, so it is forgotten here. Left in place it would sit at the
        # threshold forever and every later shared death would reset again --
        # the unexempted behaviour, arrived at by keeping the exemption's own
        # bookkeeping. Cleared only after the reset returns, so a failed reset
        # keeps the streak and the next death retries the actuator.
        runtime_death.clear_shared_deaths(session_key)
    except Exception:
        logger.warning(
            "%s: reset of %s after a shared runtime's deaths failed",
            channel_type,
            session_key,
            exc_info=True,
        )


class Drift(Enum):
    """Where ONE dispatcher still differs from the shared pipeline's default.

    Every member names a behaviour a dispatcher keeps (or, for
    :attr:`NO_DIRECTIVES`, will keep) when it moves onto :class:`ChannelTurns`, so
    the move changes nothing it can observe. None of
    them is a per-channel need: each is a convergence target, and the set may only
    SHRINK. A maintainer ruling either deletes a member (that dispatcher takes the
    default) or promotes it (every dispatcher takes the behaviour, and the member
    goes). ``test_channel_turn_structure`` pins each set to a literal with the
    ruling that would retire every member.
    """

    #: Skip the per-message governance recheck: the dispatcher already gates at its
    #: own entry, ahead of command parsing.
    NO_GOVERNANCE_BACKSTOP = "no_governance_backstop"
    #: Never ask the context builder's ``on_message`` hooks for an auto-reply.
    NO_HOOK_REPLY = "no_hook_reply"
    #: No session reset after a COMPACTION_FAILED terminal, no transient replay, no
    #: replay gap, no Stop and ``/new`` snapshots: the driver renders straight into
    #: the channel's renderer.
    NO_COMPACTION_RECOVERY = "no_compaction_recovery"
    #: Open the session's crew log at the allocation, before any further await,
    #: so a conductor DM's work-ledger writes have a log to land in. Skipped for a
    #: resumed dashboard key, whose opener (the dashboard runner) alone holds its
    #: lineage.
    OPENS_CREW_LOG = "opens_crew_log"
    #: Bind the origin and the own mirror ON the loop, not in a worker thread. The
    #: write is one bounded whole-map rewrite on a conversation's first turn, and
    #: ``session_map._MAP_LOCK`` orders it against every other guarded mutation, so
    #: the two placements are equally safe; this member records that two documented
    #: rationales still disagree about which one is right.
    BIND_ON_LOOP = "bind_on_loop"
    #: Hand the driver a tool gate without the hook's ``last_deny_reason``, so a
    #: denied call reads as "blocked by the PreToolUse security gate" to the model.
    GENERIC_DENY_REASON = "generic_deny_reason"
    #: When the stream ended without a terminal, hand the renderer a synthetic DONE
    #: carrying the driver's verdict BEFORE delivery is judged, so the bubble and the
    #: transcript row say one thing and a never-sent reply is not filed a success.
    SEAL_UNCLOSED_STREAM = "seal_unclosed_stream"
    #: Run no session-directive consumer: directive-tool results stay inert. Carried
    #: by WhatsApp once it moves onto the pipeline; it passes none today.
    NO_DIRECTIVES = "no_directives"


#: Discord's divergences. Everything but :attr:`Drift.NO_DIRECTIVES`: Discord
#: applies directives like the default.
DISCORD_DRIFT: frozenset[Drift] = frozenset(Drift) - {Drift.NO_DIRECTIVES}


class Audience(Enum):
    """Who may read what a turn assembles, which decides the turn's posture.

    * ``OWNER`` -- the operator's own conversation: full private context, the
      dispatcher's approval mode, a decider when the caller supplies one, and the
      grant (:class:`Approvals`) consulted per request.
    * ``SHARED`` -- a conversation others read too (a group): as ``OWNER`` but with
      ``minimal_context``, because the operator's memory, lessons and skills are
      assembled into the PROMPT before any tool runs.
    * ``GUEST`` -- a sender the channel admits but does not trust: ``minimal_context``
      and ``deny_all_tools`` on :data:`TOOLLESS_TURN_AGENT` in an isolated cwd, at
      INTERACTIVE with no decider and no grant, so nothing approves a tool.
    """

    OWNER = "owner"
    SHARED = "shared"
    GUEST = "guest"


@dataclass(frozen=True)
class Asker:
    """Who asked, and where the answer goes: everything that varies per message.

    ``session_key`` is final and OPAQUE: passed to ``sessions.*`` verbatim, never
    parsed or re-derived here. ``conversation_id`` is the attribution id handed on
    as the legacy ``channel_id=`` keyword. ``route`` is REQUIRED: it is what a
    shutdown refusal spools (``route.text``, never the prompt), and ``None`` says
    this message is never spooled. ``start_priority`` is REQUIRED too, because
    only the dispatcher knows whether a person sent the message.

    ``resumed`` says the key names a session this conversation RESUMED rather than
    created: it runs under its persisted agent and owes no ``set_channel`` and no
    new-session bookkeeping. With :attr:`Drift.OPENS_CREW_LOG` a resumed DASHBOARD
    key gets no crew-log opener (the dashboard runner alone holds its lineage),
    while a resumed CHANNEL history is still opened here, its only opener.
    ``origin`` names this conversation as the session's origin and own mirror;
    ``None`` leaves it unmirrored. ``addressed=False`` is a rules-mode message
    nobody addressed to the agent: no decider, no record, and the tool-less refusal
    ends silently (the turn-ceiling and member-memory refusals still render).
    ``model`` reaches a session only at creation.
    """

    session_key: str
    conversation_id: str
    route: InboundRoute | None
    start_priority: StartPriority
    audit_caller: str = ""
    reply_to: Any = None
    audience: Audience = Audience.OWNER
    addressed: bool = True
    resumed: bool = False
    origin: ChannelLink | None = None
    display_name: str | None = None
    model: str | None = None

    @property
    def can_prompt(self) -> bool:
        """Whether an approval prompt may be put to this asker at all."""
        return self.audience is not Audience.GUEST and self.addressed


def PROCESS_GRANT(_session_key: str, _decider: Any) -> bool:
    """The operator's process-wide YOLO grant, read per permission request."""
    return safety_override().is_active()


@dataclass(frozen=True)
class Approvals:
    """A dispatcher's trust sources beyond its decider.

    ``grant(session_key, decider)`` is consulted PER permission request, so taking
    or revoking it mid-turn takes effect on the next tool; the PreToolUse gate
    still runs first, so it never overrides a hard deny. ``None`` keeps the
    deny-by-default. ``discard(session_key)`` sweeps approval windows the turn
    armed and never awaited; it runs first in the turn's ``finally``.
    """

    grant: Callable[[str, Any], bool] | None = PROCESS_GRANT
    discard: Callable[[str], None] | None = None


@dataclass(frozen=True)
class TurnRecord:
    """One exchange for the channel's ledger, handed to its ``record`` adapter.

    ``answered`` is a turn that ran to completion, ``hook_reply`` one an
    ``on_message`` hook answered without a session, ``failed`` one that raised
    after it acquired its session. ``user_text`` is the prompt as prepared;
    ``reply_text`` is exactly what the driver accumulated (the partial text when
    ``failed``), so an adapter that files whitespace as no reply normalizes it
    itself. ``is_new`` is whether THIS message opened the conversation (the first
    acquire, never a replay's, never a resumed session). ``notice`` is the
    driver's empty-turn verdict; ``error`` the exception a ``failed`` turn died
    with, unredacted: the adapter that writes it owns its redaction.
    """

    kind: Literal["answered", "hook_reply", "failed"]
    session_key: str
    agent: str
    user_text: str
    reply_text: str
    is_new: bool
    notice: str = ""
    error: BaseException | None = None


def exchange_writer(
    persist: Callable[[str, str, str, bool, str], None],
) -> Callable[[TurnRecord], Awaitable[None]]:
    """A ``record`` adapter that writes one exchange through *persist*, off-loop.

    *persist* is ``(session_key, user_text, reply_text, is_new, agent)``, a plain
    blocking callable run in a worker thread. A ``failed`` turn writes nothing,
    which is what every exchange-only channel does today.
    """

    async def _record(record: TurnRecord) -> None:
        if record.kind == "failed":
            return
        await asyncio.to_thread(
            persist,
            record.session_key,
            record.user_text,
            record.reply_text,
            record.is_new,
            record.agent,
        )

    return _record


@dataclass(frozen=True)
class MonitorWake:
    """A durable monitor's wake, delivered as a turn on its owning conversation.

    The session is claimed WITHOUT waiting (a busy conversation answers BUSY so
    the wake retries its claim), the turn composes no turn ceiling and spools
    nothing, and ``still_current()`` is checked inside the pre-stream gate, after
    the claim: a conversation replaced since the gateway authorized the wake
    refuses it.
    """

    hook: MonitorCompletionHook
    still_current: Callable[[], bool]


class Verdict(Enum):
    """How one :meth:`ChannelTurns.answer` call ended."""

    DENIED = "denied"
    HOOK_REPLIED = "hook_replied"
    EMPTY = "empty"
    ANSWERED = "answered"
    UNDELIVERED = "undelivered"
    ABANDONED = "abandoned"
    CEILING = "ceiling"
    SHUTTING_DOWN = "shutting_down"
    TOOLLESS_REFUSED = "toolless_refused"
    MEMORY_REFUSED = "memory_refused"
    FAILED = "failed"
    BUSY = "busy"
    STALE = "stale"


@dataclass(frozen=True)
class TurnOutcome:
    """What :meth:`ChannelTurns.answer` reports back. ``monitor`` is set iff a wake was given."""

    verdict: Verdict
    reply_text: str = ""
    landed: bool = False
    is_new: bool = False
    stop_reason: str | None = None
    monitor: MonitorDispatchResult | None = None


#: A renderer, or ``async (muted) -> Renderer`` called once the mute is decided.
RendererSource = Renderer | Callable[[bool], Awaitable[Renderer]]


class _MonitorGenerationChanged(Exception):
    """The exact conversation a monitor wake was authorized for was replaced."""


def _monitor_result(verdict: Verdict, *, accepted: bool, acquired: bool) -> MonitorDispatchResult:
    """The wake's dispatch result for *verdict*: the table the monitor retries on."""
    if verdict in (Verdict.ANSWERED, Verdict.UNDELIVERED):
        return MonitorDispatchResult.DISPATCHED
    if verdict in (Verdict.BUSY, Verdict.SHUTTING_DOWN):
        return MonitorDispatchResult.BUSY
    if verdict is Verdict.FAILED and acquired:
        # The wake's provider turn either ran (and the claim is spent) or never
        # opened (and the claim may be retried).
        return MonitorDispatchResult.DISPATCHED if accepted else MonitorDispatchResult.BUSY
    return MonitorDispatchResult.UNAVAILABLE


def _without_deny_reason(gate: Callable[[Any], str]) -> Callable[[Any], str]:
    """*gate* as a plain callable, so the driver finds no ``last_deny_reason`` on it."""

    def _plain_gate(event: Any) -> str:
        return gate(event)

    return _plain_gate


def _record_success(sessions: Any, session_key: str, channel: str) -> None:
    """Count a delivered turn a success, guarded so a failure cannot re-charge it."""
    try:
        sessions.record_success(session_key)
        # Beside the counter it stands in for. The shared-death streak is a
        # reading of whether this session can get work done at all, so a
        # landed turn clears it exactly as it clears the consecutive-failure
        # count -- left uncleared it would be a lifetime total, and the bound
        # it feeds would stay permanently tripped while reporting the total as
        # a consecutive run.
        runtime_death.clear_shared_deaths(session_key)
    except Exception:
        logger.warning("%s: record_success failed session=%s", channel, session_key, exc_info=True)


class ChannelTurns:
    """The channel turn pipeline: one inbound message, answered end to end.

    Construction does no I/O and holds no per-message state, so a dispatcher may
    build one per gateway lifetime or one per message; ``sessions``,
    ``ctx_builder`` and ``conv_log`` are captured at construction, so a dispatcher
    whose attributes may be reassigned after it was built builds one per message
    (Discord's tests reassign them; its production code does not). ``dispatcher.approval_mode`` and the dashboard state are read on
    EVERY call, so a live reload applies to the next message.

    Adapters, all optional unless named:

    * ``agent()`` -- the agent a turn runs under (REQUIRED); a resumed session runs
      under the agent ``conv_log`` recorded for it instead, read off-loop.
    * ``record(TurnRecord)`` -- the channel's ledger. The pipeline decides WHEN it
      runs (under the permit when one is held, after any replay gap, at most once
      per ending, guarded on the answered and failed paths); the adapter decides
      WHAT it writes,
      including whether a restricted session writes anything.
      :func:`exchange_writer` is the exchange-only adapter.
    * ``notice(reply_to, session_key, provider)`` -- post-turn threshold handling.
    * ``surface()`` -- runs after the record of a turn that opened its conversation.
    * ``restricted(session_key)`` -- whether a shutdown refusal may NOT be spooled.
    * ``slot_state()`` -- the dashboard state the crew-log opener reads the slot's
      workspace from; defaults to ``dispatcher.dashboard_state``.
    * ``approvals`` -- the grant and the window sweep (:class:`Approvals`).
    * ``drift`` -- this dispatcher's remaining divergences (:class:`Drift`).
    """

    def __init__(
        self,
        channel: str,
        *,
        sessions: Any,
        ctx_builder: Any,
        dispatcher: Any,
        agent: Callable[[], str],
        record: Callable[[TurnRecord], Awaitable[None]] | None = None,
        notice: Callable[[Any, str, Any], Awaitable[None]] | None = None,
        surface: Callable[[], Awaitable[None]] | None = None,
        restricted: Callable[[str], Awaitable[bool]] | None = None,
        conv_log: Any = None,
        slot_state: Callable[[], Any] | None = None,
        approvals: Approvals = Approvals(),
        drift: frozenset[Drift] = frozenset(),
    ) -> None:
        self.channel = channel
        self._sessions = sessions
        self._ctx_builder = ctx_builder
        self._dispatcher = dispatcher
        self._agent = agent
        self._record = record
        self._notice = notice
        self._surface = surface
        self._restricted = restricted
        self._conv_log = conv_log
        self._slot_state = slot_state
        self._approvals = approvals
        self.drift = drift

    async def answer(
        self,
        asker: Asker,
        text: str,
        renderer: RendererSource,
        *,
        decider: Any = None,
        prepare: Callable[[Any, str], Awaitable[str]] | None = None,
        monitor: MonitorWake | None = None,
    ) -> TurnOutcome:
        """Answer *text* from *asker*, rendering into *renderer*.

        *renderer* is a :class:`Renderer`, or an async factory handed the mute
        decision: with a factory nothing is built before that decision, and on a
        monitor wake nothing is built before the claim. *decider* is honoured only
        when ``asker.can_prompt``. *prepare(provider, text)* runs once per attempt,
        after the session exists, and returns the prompt to build from; it must be
        idempotent, and an empty answer ends the turn EMPTY with nothing recorded.

        Every classified ending returns a :class:`TurnOutcome`. Once the turn body
        is entered only ``CancelledError`` propagates, after the turn is finalized --
        apart from the refusal-path collaborators (``restricted``, the ceiling's
        ``render_refusal``) and the ``finally``'s window sweep and ``release``, which
        propagate as they always did. Before the body, a failure in the renderer factory propagates unchanged when no
        session is held; on a monitor wake, whose claim it follows, it releases the
        claim and answers BUSY (a cancellation there propagates without releasing,
        as the dispatcher's own loop always did). A failure in ``record`` on the
        answered or failed path, or in ``notice``, ``surface``, the origin bind, the
        seal or the audit row, is logged and never changes the verdict; on the
        hook-reply path a ``record`` failure ends the turn FAILED, as
        :func:`drive_turn` always did.
        """
        guest = asker.audience is Audience.GUEST
        grant = None if guest else self._approvals.grant
        effective_decider = decider if asker.can_prompt else None
        session_key = asker.session_key
        return await self._run(
            asker,
            text,
            renderer,
            approval_mode=(APPROVAL_INTERACTIVE if guest else self._dispatcher.approval_mode),
            decider=effective_decider,
            auto_approve_session=(
                (lambda: grant(session_key, effective_decider)) if grant is not None else None
            ),
            minimal_context=asker.audience is not Audience.OWNER,
            deny_all_tools=guest,
            directive_consumer=(
                None
                if Drift.NO_DIRECTIVES in self.drift
                else build_directive_consumer(
                    session_key=session_key, sessions=self._sessions, dispatcher=self._dispatcher
                )
            ),
            prepare=prepare,
            monitor=monitor,
        )

    def _opens_crew_log(self, asker: Asker) -> bool:
        if Drift.OPENS_CREW_LOG not in self.drift:
            return False
        # A resumed DASHBOARD session is opened by the dashboard runner, which alone
        # holds its lineage; a resumed CHANNEL history has no other opener.
        return not asker.resumed or bool(channel_namespace_of(asker.session_key))

    def _open_crew_log(self, provider: Any, session_key: str, agent: str, resumed: bool) -> None:
        """Open the allocation's crew log. Never raises, never suspends."""
        if self._slot_state is not None:
            state = self._slot_state()
        else:
            state = getattr(self._dispatcher, "dashboard_state", None)
        open_turn_crew_log(
            provider,
            session_key=session_key,
            agent=agent,
            resumed=resumed,
            ctx_builder=self._ctx_builder,
            previous_sid=predecessor_sid(self._sessions, session_key),
            model_requested=requested_model_sid(self._sessions, session_key),
            workspace=slot_workspace(state, session_key),
        )

    def _outcome(
        self,
        verdict: Verdict,
        monitor: MonitorWake | None,
        *,
        acquired: bool = False,
        reply_text: str = "",
        landed: bool = False,
        is_new: bool = False,
        stop_reason: str | None = None,
    ) -> TurnOutcome:
        return TurnOutcome(
            verdict,
            reply_text=reply_text,
            landed=landed,
            is_new=is_new,
            stop_reason=stop_reason,
            monitor=(
                _monitor_result(verdict, accepted=bool(monitor.hook.accepted), acquired=acquired)
                if monitor is not None
                else None
            ),
        )

    async def _after_answer(self, asker: Asker, record: TurnRecord, provider: Any) -> None:
        """An answered turn's bookkeeping: record, surface, notice, audit.

        Each step is guarded independently, so a failure here cannot fall through to
        the turn's ``except`` and re-record a turn that actually completed.
        """
        channel = self.channel
        session_key = record.session_key
        if self._record is not None and asker.addressed:
            try:
                await self._record(record)
            except Exception:
                logger.warning(
                    "%s: persist_turn failed session=%s", channel, session_key, exc_info=True
                )
        if record.is_new and self._surface is not None:
            try:
                await self._surface()
            except Exception:
                logger.warning(
                    "%s: post-persist callback failed session=%s",
                    channel,
                    session_key,
                    exc_info=True,
                )
        if self._notice is not None:
            try:
                await self._notice(asker.reply_to, session_key, provider)
            except Exception:
                logger.warning(
                    "%s: maybe_notice failed session=%s", channel, session_key, exc_info=True
                )
        try:
            sel().log_api_access(
                caller=asker.audit_caller or f"{channel}:unknown",
                operation="transport_dispatch.handle",
                outcome="success",
                source=channel,
                resources=f"session={session_key}",
            )
        except Exception:
            logger.debug("%s: success audit failed", channel, exc_info=True)

    async def _run(
        self,
        asker: Asker,
        text: str,
        source: RendererSource,
        *,
        approval_mode: str,
        decider: Any,
        auto_approve_session: Callable[[], bool] | None,
        minimal_context: bool,
        deny_all_tools: bool,
        directive_consumer: DirectiveConsumer | None,
        prepare: Callable[[Any, str], Awaitable[str]] | None = None,
        bind: Callable[[Any], None] | None = None,
        is_muted: Callable[[], bool] | None = None,
        quiet: bool = False,
        monitor: MonitorWake | None = None,
    ) -> TurnOutcome:
        """Run one turn from fully resolved inputs; :meth:`answer` resolves them.

        *bind* is :func:`drive_turn`'s ``bind_provider``, guarded and run where
        that pipeline always ran it; it never ends the turn. *is_muted* replaces
        the mute read with :func:`drive_turn`'s :func:`conversation_is_muted`.
        *quiet* silences the tool-less refusal without touching the record, which
        is all ``ChannelTurn.unprompted`` ever meant.
        """
        channel = self.channel
        sessions = self._sessions
        ctx_builder = self._ctx_builder
        drift = self.drift
        recovery = Drift.NO_COMPACTION_RECOVERY not in drift
        session_key = asker.session_key
        # Enforced governance backstop. Channels SHOULD gate earlier (before any
        # side effect such as a command ack or a generation bump — see the weixin
        # dispatcher, which checks before parse_command), but the pipeline rechecks
        # so an adopter that forgets cannot execute a policy-denied turn. Denied
        # messages are dropped silently, before the typing indicator and before any
        # session is acquired.
        if Drift.NO_GOVERNANCE_BACKSTOP not in drift and not await inbound_permitted(channel):
            return self._outcome(Verdict.DENIED, monitor)
        agent = self._agent()
        if asker.resumed:
            # A resumed session must run as ITSELF: on a cold start ``get_or_create``
            # applies the agent it is handed, so the dispatcher's own agent would load
            # that conversation's transcript under a different system prompt and a
            # different allowedTools set -- a permission-boundary change, not a tone
            # change. Metadata is filesystem I/O, so it goes off-loop; a conversation
            # that recorded no agent keeps the dispatcher's.
            persisted = await asyncio.to_thread(
                persisted_session_agent, self._conv_log, session_key
            )
            if persisted:
                agent = persisted
        # A sender the channel does not trust talks to a tool-less agent, not to the
        # operator's. Decided here, on the shared seam, so no adopter can set the
        # flag and forget the agent that gives it teeth.
        session_agent = TOOLLESS_TURN_AGENT if deny_all_tools else agent
        _acquired = False
        # The provider THIS attempt acquired, for the failure handler's attribution
        # question. Bound before the try so every handler can read it -- an
        # attribution flag read on a path its assignment cannot reach is an
        # UnboundLocalError inside an except arm, not a guard. Stays None when
        # get_or_create never returned, and an unattributable death charges as before.
        _turn_provider: object | None = None
        provider: Any = None
        is_new = False
        resumed = False
        memory_store: Any = None
        claimed = False
        if monitor is not None:
            # A wake claims its session WITHOUT waiting, before anything is built: a
            # conversation already in a turn answers BUSY and the wake retries its
            # durable claim instead of queueing behind it.
            try:
                memory_store = await session_store_for_turn(ctx_builder, session_key)
                provider, is_new, resumed = await sessions.get_or_create(
                    session_key,
                    agent=session_agent,
                    channel_id=asker.conversation_id,
                    start_priority=asker.start_priority,
                    wait_if_busy=False,
                )
            except SessionBusyError:
                return self._outcome(Verdict.BUSY, monitor)
            except SessionClosingError:
                return self._outcome(Verdict.SHUTTING_DOWN, monitor)
            except Exception:
                logger.exception("%s monitor session claim failed", channel)
                return self._outcome(Verdict.FAILED, monitor)
            _acquired = claimed = True
            _turn_provider = provider
            if self._opens_crew_log(asker):
                self._open_crew_log(provider, session_key, agent, resumed)
        # Read ONCE, before anything is rendered: a disconnected conversation never
        # shows a typing indicator, never streams, and has nothing finalized into
        # it. The substitute is what the driver and the ``finally``'s close() both
        # use, so a real renderer opened nothing and has nothing of its own to
        # finalize -- a concrete close() is not inert (it posts an error placeholder
        # for a turn that produced no output, which a muted turn by definition did).
        muted = (
            is_muted()
            if is_muted is not None
            else delivery_is_muted(sessions, session_key, channel)
        )
        if isinstance(source, Renderer):
            renderer: Any = source
        else:
            try:
                renderer = await source(muted)
            except Exception:
                if claimed:
                    # A wake owns its claim before the renderer exists, unlike an
                    # inbound turn: fail closed and give the claim back.
                    sessions.release(session_key)
                    logger.exception("%s monitor pre-turn setup failed", channel)
                    return self._outcome(Verdict.BUSY, monitor, acquired=True)
                raise
        if muted:
            renderer = SilentRenderer(
                getattr(renderer, "capabilities", None),
                getattr(renderer, "channel_type", "") or channel,
            )
        bracket = TurnBracket(sessions, ctx_builder, session_key)
        driver: Any = None
        prompt = text
        accumulated = ""
        verdict = Verdict.FAILED
        try:
            # ── Hook auto-reply: answer and stop, without acquiring a session ──
            # A ``HOOK_REPLY`` from the context builder's user-defined hooks
            # short-circuits the turn exactly as it does on Slack: the canned reply
            # goes out, the exchange is recorded, and no ACP session is started, so a
            # message a hook already answers costs neither a cold start nor a
            # billable turn. Enforced HERE rather than per channel for the same
            # reason the governance gate is: a channel cannot honour a hook it never
            # calls. A wake is generated text and already holds its claim, so it is
            # never a hook's to answer.
            #
            # After the mute substitution so a disconnected conversation drops the
            # write like any other output, and BEFORE ``on_turn_start`` so no typing
            # indicator is opened for a turn that never runs. Inside the try so the
            # ``finally`` still finalizes the renderer; nothing is acquired, so
            # nothing is released.
            hook_reply = (
                hook_auto_reply(ctx_builder, text)
                if Drift.NO_HOOK_REPLY not in drift and monitor is None
                else None
            )
            if hook_reply is not None:
                if hook_reply:
                    await renderer.on_text_chunk(hook_reply)
                # ``on_done`` is what actually delivers on the buffered renderers, so
                # it runs even for an empty reply: the renderer then finalizes a
                # blank answer the same way it does one from the model.
                await renderer.on_done()
                if self._record is not None and asker.addressed:
                    # After the reply is out (a canned answer should not wait on a
                    # model turn) but BEFORE the record is written: an older message
                    # on this key may be between its reset and its replay, and the
                    # transcript must show that turn first, as the reader did.
                    await await_replay_gap(sessions, session_key)
                    # ``is_new`` is False: no session was created, so there is no
                    # new-session bookkeeping (title, dashboard surfacing) owed. What
                    # is recorded is the redacted text the user actually saw, so the
                    # transcript matches the conversation.
                    await self._record(
                        TurnRecord("hook_reply", session_key, agent, text, hook_reply, False)
                    )
                verdict = Verdict.HOOK_REPLIED
                return self._outcome(verdict, monitor)
            # Typing indicator first (before the potentially slow cold start);
            # on_turn_start is idempotent so the driver's later call no-ops.
            await renderer.on_turn_start()
            # ``model`` is passed ONLY when the asker named one, so a dispatcher that
            # does not offer a model command calls this with exactly the arguments it
            # always did.
            extra: dict[str, Any] = {"model": asker.model} if asker.model else {}
            if deny_all_tools:
                # ``crew_agent=""`` is the explicit "no crew" answer
                # (``config.loader.resolve_crew_identity``): without it a crew
                # ENROLLED under the tool-less agent's name would be made canonical by
                # the crew-namespace fallback, and its tooled template would start
                # under a binding that reads as the tool-less agent. The spec named
                # here must be the template itself, never a namesake crew.
                extra["crew_agent"] = ""
                # And the process must be a COLD start in this session's own work
                # directory: a warm-pool process was spawned in the operator's project
                # cwd, where a project-local spec under the same name (tools and all)
                # shadows the generated one the harness would otherwise load. An
                # explicit cwd that is not the pool's makes the pool ineligible
                # (``cwd_blocks_pool``) and the per-session directory carries no
                # project-local agents of its own.
                extra["cwd"] = str(await asyncio.to_thread(_toolless_turn_work_dir, session_key))
            # Bounded by the guard's own countdown: it holds a DONE only while it
            # still has a retry to grant, so this loop runs at most
            # ``1 + _COMPACTION_FAILED_RETRIES`` times. The driver renders through the
            # guard, so a retried attempt streams into the SAME still-open renderer
            # (nothing was emitted, so there is nothing to duplicate); every other
            # ``renderer`` use in this method keeps the real object.
            retry_guard = _TransientCompactionRetryGuard(renderer) if recovery else None
            # The user's Stop count for this key when the turn acquired its session.
            # Any later change means the user stopped this turn on SOME surface, and a
            # replay must then stay abandoned: the Stop may have landed while the key
            # had no live session at all (between the reset and the reacquire), where
            # nothing else could have cancelled it. ``None`` until the first acquire.
            stop_gen_at_entry: int | None = None
            # The conversation's persisted generation at the same moment: a ``/new``
            # issued since -- again including inside the reset gap -- retires this
            # key, and a replay would run the retired prompt and post its reply after
            # the fresh-conversation acknowledgement.
            conv_gen_at_entry: int | None = None
            # Whether THIS MESSAGE opened the conversation, from the first acquire.
            # A replay reacquires after a reset and may read ``is_new=True`` for a
            # conversation that has existed for hours; that attempt-local value is
            # right for building the replay's context (the fresh runtime needs the
            # session-start injection again) and wrong for the post-turn
            # bookkeeping, which would then re-run the new-conversation work --
            # title, dashboard surfacing -- over an existing conversation.
            turn_is_new: bool | None = None
            replaying = False
            while True:
                if not claimed:
                    # A linked member session must validate its own memory before a
                    # cold provider start. The same identity is then used for this
                    # turn's prompt.
                    memory_store = await session_store_for_turn(ctx_builder, session_key)
                    provider, is_new, resumed = await sessions.get_or_create(
                        session_key,
                        agent=session_agent,
                        channel_id=asker.conversation_id,
                        start_priority=asker.start_priority,
                        **extra,
                    )
                    _acquired = True
                    # Hold the provider this attempt obtained, for the failure
                    # handler's attribution question. Captured HERE rather than looked
                    # up when a failure is handled: the recovery paths replace a dead
                    # session, so a lookup at failure time answers for the replacement
                    # and the question silently becomes "was the NEW runtime shared".
                    # Re-bound on every pass of the retry loop, so an attempt is never
                    # judged by the runtime a previous attempt used.
                    _turn_provider = provider
                    if self._opens_crew_log(asker):
                        # The moment the allocation lands and before ANY further
                        # await: a turn that bails between the allocation and a later
                        # opener would leave a live session whose log is first created
                        # on the NEXT turn, by then a warm reuse, so its previous edge
                        # would never be written.
                        self._open_crew_log(provider, session_key, agent, resumed)
                # The wake's claim serves its first attempt only; a replay reacquires.
                claimed = False
                if deny_all_tools:
                    _refuse_unless_toolless(sessions, session_key, provider)
                if recovery:
                    if stop_gen_at_entry is None:
                        stop_gen_at_entry = session_stop_generation(sessions, session_key)
                    if conv_gen_at_entry is None:
                        conv_gen_at_entry = session_conversation_generation(sessions, session_key)
                if turn_is_new is None:
                    turn_is_new = is_new
                if retry_guard is not None:
                    retry_guard.provider = provider
                if prepare is not None:
                    # Whatever the channel can only resolve once the session exists
                    # (an upload root, an attachment fetch), before the session is
                    # bound or built for: a message with nothing left to send ends
                    # here, having changed nothing about the conversation.
                    prompt = await prepare(provider, text)
                    if not prompt:
                        verdict = Verdict.EMPTY
                        return self._outcome(verdict, monitor, acquired=True)
                if is_new and not asker.resumed:
                    # New-session bookkeeping belongs to this conversation's OWN
                    # session: ``get_or_create`` reports a resumed session new
                    # whenever its ACP session is merely cold, and ``set_channel``
                    # would stamp this conversation onto it.
                    await sessions.set_channel(session_key, asker.conversation_id)
                # Bind this conversation as the session's origin AND its own mirror,
                # so unattended notices and dashboard-side turns both reach the user
                # here. After get_or_create, because a cold-start failure leaves no
                # session to bind to; on EVERY turn, because the binding is what a
                # restart, an unlink elsewhere, or a rival claim can take away, and
                # only a self-healing bind cannot leave a live conversation silently
                # unmirrored.
                #
                # Guarded as a pair. An unbound conversation is a degraded turn -- the
                # user still gets their answer here, they just lose the dashboard
                # mirror -- whereas a raise on this line drops a turn they are waiting
                # on. ``bind_origin_mirror`` promises not to raise, but that promise
                # covers the ownership conflict it names, not a session accessor
                # failing.
                if asker.origin is not None:
                    # Captured non-None for the closure: the ``is not None`` narrowing
                    # does not reach into the nested function.
                    location = asker.origin

                    def _bind_origin() -> None:
                        # Both calls skip a ``unified:`` key, and for one reason:
                        # ``dm_scope="unified"`` collapses every allowed user's DM
                        # into a single bucket, so "the conversation this session is
                        # read in" has no single answer. Recording one would point the
                        # session's origin at whichever human spoke LAST, and a later
                        # notice (a cron result, a subagent completion) would be
                        # delivered into that person's chat regardless of whose turn
                        # produced it. ``bind_origin_mirror`` already declines for
                        # exactly this (link.py), so the sibling write must not be the
                        # hole that reopens it.
                        if channel_namespace_of(session_key) == DM_SCOPE_UNIFIED:
                            return
                        sessions.set_origin_link(session_key, location)
                        bind_origin_mirror(sessions, key=session_key, location=location)

                    try:
                        if Drift.BIND_ON_LOOP in drift:
                            _bind_origin()
                        else:
                            # Offloaded: a FRESH bind (and an in-channel /link,
                            # /unlink, or legacy opt-out migration) has
                            # ``bind_origin_mirror`` write through ``SessionMap``,
                            # which rewrites the whole map synchronously. The steady
                            # state returns early (a read) and costs the hop nothing.
                            await asyncio.to_thread(_bind_origin)
                    except Exception:
                        logger.warning(
                            "%s: origin/mirror bind failed session=%s",
                            channel,
                            session_key,
                            exc_info=True,
                        )
                # Hand the live provider to whatever the channel could not resolve
                # before the session existed. Before the driver runs, so the first
                # turn of a generation behaves like every later one.
                if bind is not None:
                    try:
                        bind(provider)
                    except Exception:
                        logger.warning(
                            "%s: bind_provider failed session=%s",
                            channel,
                            session_key,
                            exc_info=True,
                        )
                # Publish this turn's session identity so managed MCP tools resolve
                # X-Session-Key; one shared writer lives in messaging.identity.
                await publish_turn_identity(sessions, session_key)
                # A compaction drops session-start context. Read-and-clear the
                # one-shot flag so this attempt re-injects that context exactly once;
                # the bracket re-arms it in the finally if the turn never lands.
                needs_reinjection = bracket.take_reinjection()
                # This conversation's own silo comes from the session's RECORDED
                # binding (``memory_store`` above), never from ``agent``: that is a
                # kiro-cli template id, a namespace disjoint from ``cfg.agents``.
                # Off-loop: build_message embeds the episodic query (blocking urllib).
                full_message, _ = await run_in_embed_pool(
                    ctx_builder.build_message,
                    prompt,
                    is_new,
                    session_key,
                    channel_id=asker.conversation_id,
                    agent=agent,
                    memory_store=memory_store,
                    resumed=resumed,
                    needs_reinjection=needs_reinjection,
                    minimal_context=minimal_context,
                    runtime_source=channel,
                    context_provider=provider,
                    user_display_name=asker.display_name,
                )
                tool_gate = build_tool_gate(ctx_builder, session_key=session_key, agent=agent)
                if Drift.GENERIC_DENY_REASON in drift:
                    tool_gate = _without_deny_reason(tool_gate)
                if monitor is not None:
                    wake = monitor

                    def _begin_monitor_turn() -> None:
                        if not wake.still_current():
                            raise _MonitorGenerationChanged
                        sessions.begin_turn(session_key)

                    # A wake needs its OWN pre-stream gate; the ceiling's exemption
                    # for a generated turn is ``turn_ceiling.generated_turn``, set by
                    # the nudge dispatcher, and is not what this branch decides.
                    closing_gate: Callable[[], None] = _begin_monitor_turn
                    driver_extra: dict[str, Any] = {"monitor_completion": monitor.hook}
                else:
                    closing_gate = turn_ceiling.gate(
                        session_key, lambda: sessions.begin_turn(session_key)
                    )
                    driver_extra = {}
                driver = TurnDriver(
                    provider,
                    retry_guard if retry_guard is not None else renderer,
                    approval_mode=approval_mode,
                    decider=decider,
                    auto_approve_session=auto_approve_session,
                    deny_all_tools=deny_all_tools,
                    auto_approve_tool=build_auto_approve(ctx_builder),
                    tool_gate=tool_gate,
                    directive_consumer=directive_consumer,
                    audit_session_key=session_key,
                    audit_agent=agent or "kirocrew",
                    closing_gate=closing_gate,
                    **driver_extra,
                )
                if replaying and retry_guard is not None:
                    # Last look before the replay opens a prompt: a Stop issued at any
                    # point since the turn began -- including inside the reset gap --
                    # means the user does not want this message run, and a ``/new``
                    # means the conversation it belonged to is over. Either way the
                    # completion the guard held is delivered instead, so the channel
                    # finalizes the abandoned reply exactly as a permanent failure
                    # would.
                    stopped = session_stop_generation(sessions, session_key) != stop_gen_at_entry
                    superseded = (
                        session_conversation_generation(sessions, session_key) != conv_gen_at_entry
                    )
                    if stopped or superseded:
                        logger.info(
                            "%s: session=%s %s before the replay -- dropping it",
                            channel,
                            session_key,
                            "was stopped by the user" if stopped else "was superseded by /new",
                        )
                        await retry_guard.release_held()
                        # Leave the turn here, not through the post-turn bookkeeping
                        # below: nothing ran, so there is no success to record and
                        # no exchange to persist -- writing the prompt with an empty
                        # reply would file a turn the user cancelled as a completed
                        # one. The ``finally`` still closes the renderer, releases
                        # the permit and closes the gap.
                        verdict = Verdict.ABANDONED
                        return self._outcome(verdict, monitor, acquired=True)
                    # The replay's own completion supersedes the one the guard held.
                    retry_guard.drop_held()
                accumulated = await driver.run(full_message)

                # Defensive lookup, like every other attribute read on this seam: a
                # driver stand-in may predate this field. A missing reason means "no
                # synthetic completion", never an AttributeError thrown at a real
                # inbound message after the turn already ran.
                if (
                    retry_guard is None
                    or getattr(driver, "last_stop_reason", "") != STOP_REASON_COMPACTION_FAILED
                ):
                    break
                # Synthetic completion: the backend abandoned the turn after a
                # failed auto-compaction and never sent end_turn, so it still
                # counts the prompt as in progress. Reset (mirrors the dashboard
                # runner's needs_session_reset) or this channel's NEXT message
                # collides with "prompt already in progress". Whether the abandoned
                # message is then replayed is the guard's verdict, taken when the
                # driver delivered the completion: a compaction that overflowed the
                # window fails again identically, so replaying it only burns the
                # budget, while a throttled or 5xx'd summarization call has nothing
                # wrong with it and the very next attempt would clear it.
                if retry_guard.held:
                    # Opened BEFORE the reset pops the session: from that pop until
                    # the reacquire above, a Stop finds no session and would go
                    # unrecorded -- the window the pre-replay check exists for --
                    # and a newer message for this key would claim the successor
                    # first and run ahead of the replay; the open gap makes it wait.
                    # Held until the ``finally`` below, past the whole retry
                    # sequence: a waiter admitted after the reacquire would park on
                    # the successor's semaphore, which the next retry's reset would
                    # pop from under it.
                    _set_replay_gap(sessions, session_key, opened=True)
                reset_ok = True
                try:
                    await sessions.reset(session_key)
                except Exception:
                    reset_ok = False
                    logger.warning(
                        "%s: session reset after compaction failure failed session=%s",
                        channel,
                        session_key,
                        exc_info=True,
                    )
                if not retry_guard.held:
                    break
                if not reset_ok:
                    # The runtime this turn ran on is still counted as busy, so a
                    # replay would collide with "prompt already in progress".
                    # Finalize the renderer with the completion the guard held and
                    # keep the give-up behaviour; the ``finally`` closes the gap.
                    await retry_guard.release_held()
                    break
                replaying = True
                logger.info(
                    "%s: transient compaction failure session=%s (attempt %d/%d) -- "
                    "replaying the abandoned message",
                    channel,
                    session_key,
                    retry_guard.retries_used,
                    _COMPACTION_FAILED_RETRIES,
                )
                # Loop back to a fresh ``get_or_create``: the reset discarded the
                # session whose turn permit this call holds, so the reacquire above
                # takes the successor's, which the ``finally`` releases once.

            # Landed is decided by the provider turn alone, the moment run()
            # returns: the prompt (with any re-injected context) is in the
            # conversation iff the completion classifies succeeded. Delivery is
            # judged separately below -- a reply the channel failed to carry is
            # recorded a failure, but the context it carried has already landed,
            # and re-arming would inject it a second time on the next turn.
            landed = bracket.landed(driver)
            stop_reason = (
                (getattr(driver, "last_stop_reason", "") or "")
                if getattr(driver, "completion_observed", False)
                else None
            )
            turn_new = bool(turn_is_new) and not asker.resumed
            if monitor is not None and not monitor.hook.accepted:
                # The wake was refused at its own gate: no turn ran, so nothing is
                # counted, recorded or audited.
                verdict = Verdict.STALE
                return self._outcome(verdict, monitor, acquired=True)
            notice = getattr(driver, "empty_turn_notice", "") or ""
            if Drift.SEAL_UNCLOSED_STREAM in drift and not getattr(
                driver, "completion_observed", True
            ):
                # The stream ended without a terminal, so the driver dispatched no
                # DONE and the renderer has not finalized: nothing has tried to
                # reach the channel yet. Judged now, delivery would read zero
                # attempts and file a success for a turn the user may never hear,
                # and the ``finally``'s close() would then post its bare error
                # placeholder against a transcript row carrying the driver's
                # verdict. Hand the renderer the DONE it never got -- the verdict
                # rides it, so the bubble and the row say one thing -- and judge
                # delivery after it. close() in the finally stays idempotent.
                try:
                    await renderer.dispatch(
                        OutputEvent(kind=DONE, stop_reason="error", notice=notice)
                    )
                except Exception:
                    logger.warning(
                        "%s: finalizing the unclosed turn failed session=%s",
                        channel,
                        session_key,
                        exc_info=True,
                    )
            # A turn that produced text (or an empty-turn notice, that turn's ENTIRE
            # delivery) but delivered NONE of it is not a success: the provider
            # answered, the user did not hear it. Only a renderer that owns that
            # observable reports it, as a literal ``True``; a muted conversation's
            # SilentRenderer never attempts a send and never reports a failure.
            if bool(accumulated.strip() or notice) and (
                getattr(renderer, "delivery_failed", False) is True
            ):
                logger.warning(
                    "%s: the turn for %s produced %s but no message reached the "
                    "channel; recording it as a failure",
                    channel,
                    session_key,
                    "output" if accumulated.strip() else "an empty-turn notice",
                )
                await sessions.record_failure(session_key)
                verdict = Verdict.UNDELIVERED
            else:
                _record_success(sessions, session_key, channel)
                verdict = Verdict.ANSWERED

            await self._after_answer(
                asker,
                TurnRecord("answered", session_key, agent, prompt, accumulated, turn_new, notice),
                provider,
            )
            return self._outcome(
                verdict,
                monitor,
                acquired=True,
                reply_text=accumulated,
                landed=landed,
                is_new=turn_new,
                stop_reason=stop_reason,
            )
        except _MonitorGenerationChanged:
            logger.info(
                "%s monitor dispatch refused after generation changed for %s",
                channel,
                session_key,
            )
            verdict = Verdict.STALE
        except TurnCeilingExceeded as exc:
            # The conversation is at its turn ceiling, so this turn never opened.
            # Ahead of the shutdown branch below and deliberately unlike it in two
            # ways. It is NOT spooled: the spool exists so a message our own restart
            # dropped is answered later, and replaying a message we refused on
            # purpose would answer it after all. And it does not `record_failure`:
            # the session did not misbehave, the conversation reached a bound.
            #
            # The notice IS the point. A refusal the user cannot see is the same
            # silence the per-message echo guards already leave, so the text goes
            # into this channel's own renderer (the mute substitute when muted),
            # which also ends the stream: the `finally` below only tears the
            # renderer down and flushes nothing.
            logger.warning(
                "%s: turn ceiling reached for %s -- conversation paused",
                channel,
                session_key,
            )
            await turn_ceiling.render_refusal(renderer, exc)
            verdict = Verdict.CEILING
        except SessionClosingError:
            # The gateway began shutting down between the claim and the dispatch, so
            # this turn never opened. Terminal for the message, but NOT a fault of
            # the session — which is why it is caught ahead of the generic handler
            # below and deliberately skips `record_failure`: charging a restart to
            # the circuit breaker would count toward tripping a reset on a session
            # that never misbehaved, and `logger.exception` would file a routine
            # shutdown as an error with a full traceback.
            #
            # The `finally` still runs, so the renderer is finalized (the user gets
            # this channel's notice rather than silence) and the lease is released.
            logger.info(
                "%s: aborting dispatch for %s — gateway is shutting down",
                channel,
                session_key,
            )
            verdict = Verdict.SHUTTING_DOWN
            # A wake is generated work whose own loop re-fires after the restart, so
            # spooling it would replay a check the loop is about to run again anyway.
            #
            # Durability, at the ONE point where the payload is still in memory and
            # the turn is provably unopened. Every other outcome of this dispatch is
            # already recorded somewhere, which is why nothing is spooled on those
            # paths and why a replay cannot double-answer. Best-effort by
            # construction: the helper never raises, so a full disk degrades to
            # today's loss rather than becoming the thing that fails shutdown.
            # ``route.text`` and ONLY ``route.text`` -- never the prompt. The two
            # differ wherever a channel transforms the prompt, and the difference is
            # not cosmetic: WhatsApp's rules mode prepends the group's private
            # operating rules to the model prompt, so spooling the turn text would
            # quote those rules back into the group in the restart notice. NOT for a
            # restricted session: an incognito or temporary conversation is a promise
            # that nothing persists, and the spool is a durable file holding the
            # message verbatim.
            if monitor is None and not (
                self._restricted is not None and await self._restricted(session_key)
            ):
                await spool_refused_turn(channel_type=channel, route=asker.route)
        except ToollessTurnUnavailable as exc:
            # Refused before the prompt opened; the sender gets one neutral line so
            # the silence is not read as being ignored, the SEL row already names why.
            # Not charged to the circuit breaker: this is a configuration refusal,
            # not a provider failure, and a group of refused members would otherwise
            # trip the breaker for the session they never got to use.
            logger.warning("%s: %s", channel, exc)
            verdict = Verdict.TOOLLESS_REFUSED
            if asker.addressed and not quiet:
                try:
                    await renderer.on_text_chunk(TOOLLESS_TURN_REFUSAL_NOTE)
                    await renderer.on_done()
                except Exception:
                    logger.warning(
                        "%s: could not display tool-less refusal", channel, exc_info=True
                    )
        except UnknownMemoryStore as exc:
            logger.warning("%s member memory unavailable: %s", channel, exc)
            verdict = Verdict.MEMORY_REFUSED
            if monitor is None:
                try:
                    await renderer.on_text_chunk(redact_local_paths(redact(str(exc)))[0][:1000])
                    await renderer.on_done()
                except Exception:
                    logger.warning("%s: could not display memory refusal", channel, exc_info=True)
        except Exception as exc:
            logger.exception("%s transport_dispatch: error handling message", channel)
            verdict = Verdict.FAILED
            if _acquired:
                # A dying runtime reaches this generic handler as one more exception,
                # so without the attribution question every tenant of one process
                # charges its own breaker for a single process event. The provider
                # handed over is the one THIS attempt acquired, never a fresh lookup.
                await charge_turn_failure(
                    sessions,
                    session_key,
                    exc=exc,
                    provider=_turn_provider,
                    channel_type=channel,
                )
                # The turn raised ahead of the post-turn record, so nothing above
                # recorded it. The reply is what the driver had accumulated when it
                # raised -- text the renderer was already handed and the user already
                # saw. Guarded like every other bookkeeping step: a record failure
                # must not mask the turn's error or reach the finally un-released.
                if self._record is not None and asker.addressed:
                    try:
                        await self._record(
                            TurnRecord(
                                "failed",
                                session_key,
                                agent,
                                prompt,
                                getattr(driver, "partial_text", "") or "",
                                False,
                                error=exc,
                            )
                        )
                    except Exception:
                        logger.warning(
                            "%s: persist of the failed turn failed session=%s",
                            channel,
                            session_key,
                            exc_info=True,
                        )
        finally:
            # An approval window the driver never awaited -- the prompt went out and
            # the turn then ended before the decider -- has no wait of its own to
            # close it, so it would outlive this turn with its nonce still armed.
            if self._approvals.discard is not None:
                self._approvals.discard(session_key)
            # A turn that consumed the post-compaction flag but never landed
            # discarded the prompt carrying the re-injected context, and a backend
            # that compacted the session during the turn dropped it; the bracket
            # arms the flag for either, then settles the skill-body dedup writes.
            # ``driver`` is the latest attempt's: a replay after a COMPACTION_FAILED
            # reset reports on the successor session, not the one the reset replaced.
            bracket.compacted(driver)
            bracket.settle()
            # Always finalize the turn, even if get_or_create raised before the
            # semaphore was held. Only release if we actually acquired it.
            #
            # ``renderer.close()`` is best-effort and must NEVER prevent the release
            # below. A renderer that fails to finalize -- a malformed vendor
            # response, a dropped socket mid-flush -- would otherwise leave the
            # semaphore held with no path to give it back. Because the semaphore is
            # keyed by SESSION, that does not just lose this turn: every later
            # message for that conversation blocks forever, and any queued turn
            # never drains. The channel looks permanently busy until the gateway
            # restarts.
            try:
                await renderer.close()
            except Exception:
                logger.warning(
                    "%s: renderer.close failed session=%s",
                    channel,
                    session_key,
                    exc_info=True,
                )
            if _acquired:
                sessions.release(session_key)
            if recovery:
                # Closed LAST, after the permit is back: a message that waited behind
                # the gap then finds an idle successor instead of a semaphore a later
                # reset could pop from under it. Idempotent, so a turn that never
                # opened one (or whose reset raised) costs a dictionary lookup.
                _set_replay_gap(sessions, session_key, opened=False)
        return self._outcome(verdict, monitor, acquired=_acquired)


async def drive_turn(turn: ChannelTurn, *, sessions: Any, ctx_builder: Any) -> None:
    """Run one authorized inbound message end to end, from a :class:`ChannelTurn`.

    The compatibility entry for the dispatchers that still describe each turn
    field by field: every field maps one-to-one onto :class:`ChannelTurns`, so this
    is the same pipeline with no divergence of its own.
    """
    persist = turn.persist
    notice = turn.notice

    async def _restricted(_session_key: str) -> bool:
        return turn.inbound_restricted

    turns = ChannelTurns(
        turn.channel_type,
        sessions=sessions,
        ctx_builder=ctx_builder,
        dispatcher=None,
        agent=lambda: turn.agent,
        record=(
            exchange_writer(lambda _key, user, reply, is_new, _agent: persist(user, reply, is_new))
            if persist is not None
            else None
        ),
        notice=(
            (lambda _where, key, provider: notice(key, provider)) if notice is not None else None
        ),
        surface=turn.after_persist,
        restricted=_restricted,
    )
    await turns._run(
        Asker(
            turn.session_key,
            turn.conversation_id,
            route=turn.inbound_route,
            start_priority=turn.start_priority,
            audit_caller=turn.audit_caller,
            origin=turn.origin_conversation,
            display_name=turn.user_display_name,
            model=turn.model,
        ),
        turn.user_text,
        _given(turn.renderer),
        approval_mode=turn.approval_mode,
        decider=turn.decider,
        auto_approve_session=turn.auto_approve_session,
        minimal_context=turn.minimal_context,
        deny_all_tools=turn.deny_all_tools,
        directive_consumer=turn.directive_consumer,
        bind=turn.bind_provider,
        is_muted=lambda: conversation_is_muted(sessions, turn),
        quiet=turn.unprompted,
    )


def _given(renderer: Any) -> Callable[[bool], Awaitable[Any]]:
    """A renderer factory answering with an already-built *renderer*."""

    async def _factory(_muted: bool) -> Any:
        return renderer

    return _factory
