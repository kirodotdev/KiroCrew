"""Shared channel turn pipeline — one copy of the dispatch skeleton.

This module owns the sequence every non-Slack channel dispatcher runs around
:class:`TurnDriver`:

    governance gate
    -> hook auto-reply                   (HOOK_REPLY short-circuits, no session)
    -> renderer.on_turn_start()          (typing indicator before cold start)
    -> sessions.get_or_create + set_channel
    -> publish_turn_identity
    -> ctx_builder.build_message         (off-loop, embeds block)
    -> TurnDriver.run                    (shared redaction + approval ladder)
    -> COMPACTION_FAILED: reset; replay once more if the failure was transient
                                         and nothing was emitted (bounded)
    -> post-turn: record_success, persist, threshold notice, SEL audit
                                         (each guarded independently)
    -> finally: renderer.close() + release (release gated on acquire)

What stays per-channel is what actually differs between them: the wire
protocol, event normalization, ``authorize()`` semantics, rendering, command
vocabulary, and the ack strings. Channels inject those through
:class:`ChannelTurn` rather than subclassing, so a capability this protocol
lacks widens the protocol once instead of forking the pipeline.

Dependency direction is ``<channel> -> messaging`` (never the reverse), so this
module must not import any channel package.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED
from kiro_crew.agent_sdk.drivers.acp_vocab import classify_stop_reason
from kiro_crew.context import session_store_for_turn
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import (
    HOOK_REPLY,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    event_is_spawn_run,
    hook_gate_kwargs,
)
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging.driver import DirectiveConsumer, TurnDriver
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
from kiro_crew.session_allocation import SessionClosingError

logger = logging.getLogger(__name__)


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

    Invariant: this MUST equal the first ``:``-segment of ``session_key`` —
    the surface name is the routing authority for governance and (future)
    control-plane operations. Holds by construction today because every
    adopter builds its key via ``build_dm_session_key(channel_type, ...)``.
    """

    session_key: str
    """The session address. OPAQUE to this pipeline: it is passed through to
    ``sessions.*`` verbatim and never parsed, split, or rebuilt here. Keys are
    constructed channel-side via :func:`kiro_crew.messaging.link.build_dm_session_key`
    (``{surface}:{agent}:{chat_type}:{scope}[:genN]``). Keeping the pipeline
    address-agnostic is what lets the address grammar evolve (deeper scope
    paths, new surfaces) without touching dispatch.
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
    interactive ladder is consulted. Defaults False, so every existing adopter is
    byte-identical."""

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
    Both writes are steady-state READS after the first turn, so this costs a map
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
    the gate strict, which is what keeps ``drive_turn``'s backstop a backstop -- a
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
    """

    def _tool_gate(event: Any) -> str:
        result = ctx_builder.hooks.on_tool_call(
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
    because Discord and Telegram run their OWN copies of the turn loop rather
    than going through :func:`drive_turn`, so they have no ``ChannelTurn`` to
    pass. Every channel that can be disconnected must consult this, or the
    dashboard control is a label with nothing behind it.

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


def consume_reinjection(sessions: Any, session_key: str) -> bool:
    """Read-and-clear the one-shot post-compaction re-injection flag.

    ``session_compaction`` marks it after a successful in-place compaction,
    because compaction drops the session-start context (skills index, member
    section, response preferences). The turn that consumes it passes the value
    to ``build_message`` as ``needs_reinjection`` so that context comes back
    exactly once. Every channel turn loop reads it through this one helper: a
    per-channel copy of the turn loop that skips it re-injects nothing after
    ``/compact``.

    Defensive on the accessor: a session stand-in that predates the flag gets
    the safe ``False``, never an AttributeError on a real inbound message.
    """
    consume = getattr(sessions, "consume_needs_reinjection", None)
    return bool(consume(session_key)) if callable(consume) else False


def stop_reason_landed(stop_reason: str | None) -> bool:
    """Whether the turn that ended with *stop_reason* landed, for re-injection.

    ``None`` means no completion was observed at all -- the stream exhausted or
    was cut without an ``EVENT_COMPLETE`` -- and that is never landed: nothing
    proves the prompt reached the conversation. A string is a completion's
    stop reason, judged as an allowlist through the one stop-reason classifier
    every completion consumer shares: only a ``succeeded`` class (``end_turn``,
    or an empty reason from a provider that never populates the field) proves
    the prompt -- and the re-injected context it carried -- is now part of the
    conversation. Every other terminal is a turn the backend did not complete:
    ``cancelled`` (the backend drops a cancelled turn from its transcript),
    ``stale_recover`` and ``error: tool stall`` (synthetic completions for a
    wedged turn), ``refusal`` and the ``error:`` family. All of those leave the
    consumed flag to be re-armed.
    """
    if stop_reason is None:
        return False
    return classify_stop_reason(stop_reason).is_success


def driver_turn_landed(driver: Any) -> bool:
    """:func:`stop_reason_landed` for a completed ``TurnDriver.run``.

    ``run`` returns normally on every terminal the backend synthesises a
    completion for, a user cancel included, and also when the stream simply
    ends without one, so the driver records both the stop reason and whether a
    completion was observed. Defensive on the attributes, like every other
    read on the driver seam, in the fail-safe direction: a stand-in that
    reports no completion is not landed, so the worst case is one extra
    re-injection rather than a lost one.
    """
    if not getattr(driver, "completion_observed", False):
        return stop_reason_landed(None)
    return stop_reason_landed(getattr(driver, "last_stop_reason", "") or "")


def rearm_reinjection(sessions: Any, session_key: str, *, consumed: bool, landed: bool) -> None:
    """Put the one-shot flag back when this turn consumed it but never landed.

    The flag is cleared BEFORE ``build_message``, so a turn that then dies -- a
    provider error, a driver fault, a cancel -- has discarded the prompt that
    carried the re-injected context, and without this the session runs without
    its skills index (and a member DM without its rules) until the next
    compaction. This is the contract the dashboard runner already keeps in its
    own ``finally`` (``chat_runner``: re-arm when consumed and not landed); the
    channel loops share it so the two paths cannot disagree.

    ``landed`` means the turn was recorded a success. A cancelled turn is NOT
    landed: the backend drops a cancelled turn from its own transcript, so the
    context it carried is gone with it. Call from the turn's ``finally`` so every
    exit path is covered. Never raises: a failure to re-arm is logged and the
    turn's own outcome stands.
    """
    if not consumed or landed:
        return
    mark = getattr(sessions, "mark_needs_reinjection", None)
    if not callable(mark):
        return
    try:
        mark(session_key)
    except Exception:
        logger.debug(
            "re-arming post-compaction re-injection failed session=%s",
            session_key,
            exc_info=True,
        )


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
# throttled or 5xx'd summarization call). Per drive_turn call, so the budget is
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
    """The renderer the driver sees while :func:`drive_turn` may still retry.

    A turn abandoned after a transient compaction failure is replayed INSIDE the
    same ``drive_turn`` call, into the same renderer: the shared pipeline has no
    queue of its own to put the message back on (five of the seven channels
    riding it keep none), and the channel's renderer is one message's output
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


async def drive_turn(turn: ChannelTurn, *, sessions: Any, ctx_builder: Any) -> None:
    """Run one authorized inbound message end to end.

    Everything acquire-dependent runs INSIDE the try so ``finally`` always
    finalizes the turn (``renderer.close``), even when ``get_or_create`` raises
    on a cold-start failure. ``release()`` is gated on ``_acquired`` so a
    semaphore that was never held is never released.
    """
    renderer = turn.renderer
    session_key = turn.session_key
    _acquired = False
    # Post-compaction re-injection bookkeeping for the finally: whether this
    # turn consumed the one-shot flag, and whether it landed (recorded success).
    needs_reinjection = False
    _turn_landed = False
    # Enforced governance backstop. Channels SHOULD gate earlier (before any
    # side effect such as a command ack or a generation bump — see the weixin
    # dispatcher, which checks before parse_command), but the pipeline rechecks
    # so an adopter that forgets cannot execute a policy-denied turn. Denied
    # messages are dropped silently, before the typing indicator and before any
    # session is acquired.
    if not await inbound_permitted(turn.channel_type):
        return
    # Substituted BEFORE on_turn_start so a disconnected conversation never even
    # shows a typing indicator, and before TurnDriver so nothing streams. The
    # local name is what the driver and the finally's close() both use, so the
    # real renderer is left completely untouched -- it opened nothing, so there
    # is nothing of its own to finalize.
    if conversation_is_muted(sessions, turn):
        renderer = SilentRenderer(
            getattr(renderer, "capabilities", None),
            getattr(renderer, "channel_type", "") or turn.channel_type,
        )
    try:
        # ── Hook auto-reply: answer and stop, without acquiring a session ──
        # A ``HOOK_REPLY`` from the context builder's user-defined hooks
        # short-circuits the turn exactly as it does on Slack: the canned reply
        # goes out, the exchange is recorded, and no ACP session is started, so a
        # message a hook already answers costs neither a cold start nor a
        # billable turn. Enforced HERE rather than per channel for the same
        # reason the governance gate is: a channel cannot honour a hook it never
        # calls, and every adopter would otherwise have to re-derive this.
        #
        # Placed after the mute substitution so a disconnected conversation drops
        # the write like any other output, and BEFORE ``on_turn_start`` so no
        # typing indicator is opened for a turn that never runs. Inside the try
        # so the ``finally`` still finalizes the renderer; ``_acquired`` is still
        # False, so nothing is released.
        hook_reply = hook_auto_reply(ctx_builder, turn.user_text)
        if hook_reply is not None:
            if hook_reply:
                await renderer.on_text_chunk(hook_reply)
            # ``on_done`` is what actually delivers on the buffered renderers, so
            # it runs even for an empty reply: the renderer then finalizes a
            # blank answer the same way it does one from the model.
            await renderer.on_done()
            if turn.persist is not None:
                # After the reply is out (a canned answer should not wait on a
                # model turn) but BEFORE the record is written: an older message
                # on this key may be between its reset and its replay, and the
                # transcript must show that turn first, as the reader did.
                await await_replay_gap(sessions, session_key)
                # ``is_new`` is False: no session was created, so there is no
                # new-session bookkeeping (title, dashboard surfacing) owed. What
                # is recorded is the redacted text the user actually saw, so the
                # transcript matches the conversation.
                await asyncio.to_thread(turn.persist, turn.user_text, hook_reply, False)
            return
        # Typing indicator first (before the potentially slow cold start);
        # on_turn_start is idempotent so the driver's later call no-ops.
        await renderer.on_turn_start()
        # ``model`` is passed ONLY when the channel set one, so an adopter that
        # does not offer a model command calls this with exactly the arguments it
        # always did. Widening the call for everyone would make the new field's
        # cost fall on channels that gain nothing from it.
        extra: dict[str, Any] = {"model": turn.model} if turn.model else {}
        # Bounded by the guard's own countdown: it holds a DONE only while it
        # still has a retry to grant, so this loop runs at most
        # ``1 + _COMPACTION_FAILED_RETRIES`` times. The driver renders through the
        # guard, so a retried attempt streams into the SAME still-open renderer
        # (nothing was emitted, so there is nothing to duplicate); every other
        # ``renderer`` use in this function -- the mute substitution above, the
        # ``on_done``/``close`` below -- keeps the real object.
        retry_guard = _TransientCompactionRetryGuard(renderer)
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
            # A linked member session must validate its own memory before a cold
            # provider start. The same identity is then used for this turn's prompt.
            memory_store = await session_store_for_turn(ctx_builder, session_key)
            provider, is_new, resumed = await sessions.get_or_create(
                session_key, agent=turn.agent, channel_id=turn.conversation_id, **extra
            )
            _acquired = True
            if stop_gen_at_entry is None:
                stop_gen_at_entry = session_stop_generation(sessions, session_key)
            if conv_gen_at_entry is None:
                conv_gen_at_entry = session_conversation_generation(sessions, session_key)
            if turn_is_new is None:
                turn_is_new = is_new
            retry_guard.provider = provider
            if is_new:
                await sessions.set_channel(session_key, turn.conversation_id)
            # Bind this conversation as the session's origin AND its own mirror, so
            # unattended notices and dashboard-side turns both reach the user here.
            # After get_or_create, because a cold-start failure leaves no session to
            # bind to; on EVERY turn, because the binding is what a restart, an
            # unlink elsewhere, or a rival claim can take away, and only a
            # self-healing bind cannot leave a live conversation silently unmirrored.
            #
            # Deliberately NOT gated on ``resumed``. Discord skips its bind for a
            # resumed session, but its flag is a mirror-binding LOOKUP ("this turn is
            # answering a dashboard-owned session"), whereas ``resumed`` here means
            # "restored via ACP session/load" — a cold-start recovery of this very
            # conversation, which is exactly the case a self-healing bind exists for.
            # Skipping on it would leave every post-restart session unmirrored.
            #
            # Guarded as a pair. An unbound conversation is a degraded turn — the
            # user still gets their answer here, they just lose the dashboard mirror
            # — whereas a raise on this line drops a turn they are waiting on.
            # ``bind_origin_mirror`` promises not to raise, but that promise covers
            # the ownership conflict it names, not a session accessor failing, and
            # this is the widest call site in the codebase: every channel on the
            # shared pipeline routes through it.
            if turn.origin_conversation is not None:
                # Captured non-None for the closure: the ``is not None`` narrowing does
                # not reach into the nested function (it could be called after the
                # attribute changed), and a local binding is what makes it a
                # ``ChannelLink`` there.
                location = turn.origin_conversation
                try:
                    # Offloaded, like ``turn.persist`` above: a FRESH bind (and an
                    # in-channel /link, /unlink, or legacy opt-out migration) has
                    # ``bind_origin_mirror`` write through ``SessionMap``, which
                    # rewrites the whole map synchronously -- blocking I/O that must
                    # not run on the shared gateway loop. The steady state returns
                    # early (a read) and costs the thread hop nothing.
                    def _bind_origin() -> None:
                        # Both calls skip a ``unified:`` key, and for one reason:
                        # ``dm_scope="unified"`` collapses every allowed user's DM into
                        # a single bucket, so "the conversation this session is read in"
                        # has no single answer. Recording one would point the session's
                        # origin at whichever human spoke LAST, and a later notice (a
                        # cron result, a subagent completion) would be delivered into
                        # that person's chat regardless of whose turn produced it.
                        # ``bind_origin_mirror`` already declines for exactly this
                        # (link.py), so the sibling write must not be the hole that
                        # reopens it.
                        if channel_namespace_of(session_key) == DM_SCOPE_UNIFIED:
                            return
                        sessions.set_origin_link(session_key, location)
                        bind_origin_mirror(sessions, key=session_key, location=location)

                    await asyncio.to_thread(_bind_origin)
                except Exception:
                    logger.warning(
                        "%s: origin/mirror bind failed session=%s",
                        turn.channel_type,
                        session_key,
                        exc_info=True,
                    )
            # Hand the live provider to whatever the channel could not resolve before
            # the session existed. Before the driver runs, so the first turn of a
            # generation behaves like every later one.
            if turn.bind_provider is not None:
                try:
                    turn.bind_provider(provider)
                except Exception:
                    logger.warning(
                        "%s: bind_provider failed session=%s",
                        turn.channel_type,
                        session_key,
                        exc_info=True,
                    )
            # Publish this turn's session identity so managed MCP tools resolve
            # X-Session-Key; one shared writer lives in messaging.identity.
            await publish_turn_identity(sessions, session_key)
            # This conversation's own silo, from the session's RECORDED binding and
            # never from ``turn.agent``: that field carries a kiro-cli template id, a
            # namespace disjoint from ``cfg.agents``, so a store derived from it
            # resolves to ``default`` for exactly the crew that configured otherwise.
            # Resolved on the shared seam rather than per adopter for the same reason
            # ``minimal_context`` is: every channel on this pipeline has the same
            # exposure, and one that forgot would silently read the operator's memory.
            # The member tier was prepared before provider acquisition; unavailable
            # private memory refuses the turn instead of substituting global memory.
            # A compaction drops session-start context. Read-and-clear the one-shot
            # flag so this turn re-injects that context exactly once. The finally
            # re-arms it if this turn never lands.
            needs_reinjection = consume_reinjection(sessions, session_key)

            # Off-loop: build_message embeds the episodic query (blocking urllib).
            full_message, _ = await run_in_embed_pool(
                ctx_builder.build_message,
                turn.user_text,
                is_new,
                session_key,
                channel_id=turn.conversation_id,
                agent=turn.agent,
                memory_store=memory_store,
                resumed=resumed,
                needs_reinjection=needs_reinjection,
                minimal_context=turn.minimal_context,
                runtime_source=turn.channel_type,
                context_provider=provider,
            )

            driver = TurnDriver(
                provider,
                retry_guard,
                approval_mode=turn.approval_mode,
                decider=turn.decider,
                auto_approve_session=turn.auto_approve_session,
                deny_all_tools=turn.deny_all_tools,
                auto_approve_tool=build_auto_approve(ctx_builder),
                tool_gate=build_tool_gate(ctx_builder, session_key=session_key, agent=turn.agent),
                directive_consumer=turn.directive_consumer,
                audit_session_key=session_key,
                audit_agent=turn.agent or "kirocrew",
                closing_gate=lambda: sessions.begin_turn(session_key),
            )
            if replaying:
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
                        turn.channel_type,
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
                    return
                # The replay's own completion supersedes the one the guard held.
                retry_guard.drop_held()
            accumulated = await driver.run(full_message)

            # Defensive lookup, like every other attribute read on this seam: the
            # driver is resolved through the module attribute, so a caller (or a
            # test) may supply a stand-in that predates this field. A missing
            # reason means "no synthetic completion", never an AttributeError
            # thrown at a real inbound message after the turn already ran.
            if getattr(driver, "last_stop_reason", "") != STOP_REASON_COMPACTION_FAILED:
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
                    turn.channel_type,
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
                turn.channel_type,
                session_key,
                retry_guard.retries_used,
                _COMPACTION_FAILED_RETRIES,
            )
            # Loop back to a fresh ``get_or_create``: the reset discarded the
            # session whose turn permit this call holds, so the reacquire below
            # takes the successor's, which the ``finally`` releases once.

        # ── Post-turn bookkeeping. Each step is guarded independently so a
        # failure here cannot fall through to the except and re-record a turn
        # that actually succeeded. ──
        try:
            sessions.record_success(session_key)
        except Exception:
            logger.warning(
                "%s: record_success failed session=%s",
                turn.channel_type,
                session_key,
                exc_info=True,
            )
        # The prompt (with any re-injected context) reached the model and the
        # turn completed, so the finally must NOT restore the one-shot flag --
        # unless the user cancelled it, which discards that prompt.
        _turn_landed = driver_turn_landed(driver)
        if turn.persist is not None:
            try:
                await asyncio.to_thread(
                    turn.persist, turn.user_text, accumulated, bool(turn_is_new)
                )
            except Exception:
                logger.warning(
                    "%s: persist_turn failed session=%s",
                    turn.channel_type,
                    session_key,
                    exc_info=True,
                )
        if turn_is_new and turn.after_persist is not None:
            try:
                await turn.after_persist()
            except Exception:
                logger.warning(
                    "%s: post-persist callback failed session=%s",
                    turn.channel_type,
                    session_key,
                    exc_info=True,
                )
        if turn.notice is not None:
            try:
                await turn.notice(session_key, provider)
            except Exception:
                logger.warning(
                    "%s: maybe_notice failed session=%s",
                    turn.channel_type,
                    session_key,
                    exc_info=True,
                )
        try:
            sel().log_api_access(
                caller=turn.audit_caller or f"{turn.channel_type}:unknown",
                operation="transport_dispatch.handle",
                outcome="success",
                source=turn.channel_type,
                resources=f"session={session_key}",
            )
        except Exception:
            logger.debug("%s: success audit failed", turn.channel_type, exc_info=True)
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
            turn.channel_type,
            session_key,
        )
        # Durability, at the ONE point where the payload is still in memory and
        # the turn is provably unopened. Every other outcome of this
        # dispatch — a completed turn, a turn that ran and failed — is already
        # recorded somewhere, which is why nothing is spooled on those paths and
        # why a replay cannot double-answer. Best-effort by construction: the
        # helper never raises, so a full disk degrades to today's loss rather than
        # becoming the thing that fails shutdown.
        # ``route.text`` and ONLY ``route.text`` -- never ``turn.user_text``. The
        # two differ wherever a channel transforms the prompt, and the difference
        # is not cosmetic: WhatsApp's rules mode prepends the group's private
        # operating rules to the model prompt, so spooling the turn text would
        # quote those rules back into the group in the restart notice. A route
        # whose text is empty is a media-only entry (or nothing), not a cue to
        # reach for the prompt.
        if not turn.inbound_restricted:
            await spool_refused_turn(channel_type=turn.channel_type, route=turn.inbound_route)
    except UnknownMemoryStore as exc:
        logger.warning("%s member memory unavailable: %s", turn.channel_type, exc)
        try:
            await renderer.on_text_chunk(redact_local_paths(redact(str(exc)))[0][:1000])
            await renderer.on_done()
        except Exception:
            logger.warning("%s: could not display memory refusal", turn.channel_type, exc_info=True)
    except Exception:
        logger.exception("%s transport_dispatch: error handling message", turn.channel_type)
        if _acquired:
            await sessions.record_failure(session_key)
    finally:
        # A turn that consumed the post-compaction flag but never landed
        # discarded the prompt carrying the re-injected context; put the flag
        # back so the next turn re-injects it. First, because nothing below
        # depends on it and it must run on every exit path.
        rearm_reinjection(sessions, session_key, consumed=needs_reinjection, landed=_turn_landed)
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
        #
        # Discord already guards this in its own dispatcher, which is how the
        # hazard was found; the guard belongs here so every channel on the
        # shared pipeline inherits it instead of re-deriving it.
        try:
            await renderer.close()
        except Exception:
            logger.warning(
                "%s: renderer.close failed session=%s",
                turn.channel_type,
                session_key,
                exc_info=True,
            )
        if _acquired:
            sessions.release(session_key)
        # Closed LAST, after the permit is back: a message that waited behind the
        # gap then finds an idle successor instead of a semaphore a later reset
        # could pop from under it. Idempotent, so a turn that never opened one
        # (or whose reset raised) costs a dictionary lookup.
        _set_replay_gap(sessions, session_key, opened=False)
