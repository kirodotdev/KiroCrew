"""What a dispatcher hands the shared channel turn pipeline, and what it gets back.

The value types of :mod:`kiro_crew.messaging.dispatch`: :class:`ChannelTurn` (a turn
described field by field, for :func:`~kiro_crew.messaging.dispatch.drive_turn`), the
:class:`~kiro_crew.messaging.dispatch.ChannelTurns` adapters and inputs (:class:`Drift`,
:class:`Audience`, :class:`Asker`, :class:`Approvals`, :class:`TurnRecord` with its
:func:`exchange_writer`, :class:`MonitorWake`, ``RendererSource``) and its answer
(:class:`Verdict`, :class:`TurnOutcome`, and the monitor-wake result table). Callers import
them from :mod:`kiro_crew.messaging.dispatch`; a new field or verdict goes here.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from enum import Enum
from typing import Any, Awaitable, Callable, Literal, Optional

from kiro_crew.messaging.driver import DirectiveConsumer
from kiro_crew.messaging.inbound_spool import InboundRoute
from kiro_crew.messaging.link import ChannelLink
from kiro_crew.messaging.renderer import Renderer
from kiro_crew.monitoring.completion import MonitorCompletionHook
from kiro_crew.monitoring.models import MonitorDispatchResult
from kiro_crew.safety_override import safety_override
from kiro_crew.start_priority import StartPriority


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
    ``model`` reaches a session only at creation. ``agent`` is this
    conversation's own ``/agent`` pick; ``None`` runs the pipeline's agent.
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
    agent: str | None = None

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


# Named in tracebacks, log lines and reprs, so they keep the module callers import them from.
Drift.__module__ = "kiro_crew.messaging.dispatch"
Audience.__module__ = "kiro_crew.messaging.dispatch"
Verdict.__module__ = "kiro_crew.messaging.dispatch"
_MonitorGenerationChanged.__module__ = "kiro_crew.messaging.dispatch"
