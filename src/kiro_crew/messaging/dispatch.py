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
import importlib as _importlib
import logging
import sys as _sys
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from kiro_crew import _facade_mirror
from kiro_crew.acp.types import STOP_REASON_COMPACTION_FAILED
from kiro_crew.context import session_store_for_turn
from kiro_crew.executors import run_in_embed_pool
from kiro_crew.hooks import (
    HOOK_REPLY,
)
from kiro_crew.memory_stores import UnknownMemoryStore
from kiro_crew.messaging import turn_ceiling
from kiro_crew.messaging.dispatch_admission import (
    conversation_is_muted,
    delivery_is_muted,
    inbound_permitted,
)
from kiro_crew.messaging.dispatch_contract import (
    Approvals,
    Asker,
    Audience,
    ChannelTurn,
    Drift,
    MonitorWake,
    RendererSource,
    TurnOutcome,
    TurnRecord,
    Verdict,
    _monitor_result,
    _MonitorGenerationChanged,
    exchange_writer,
)
from kiro_crew.messaging.dispatch_crew_log import (
    open_turn_crew_log,
    predecessor_sid,
    requested_model_sid,
    slot_workspace,
)
from kiro_crew.messaging.dispatch_recovery import (
    _record_success,
    _set_replay_gap,
    await_replay_gap,
    charge_turn_failure,
    session_conversation_generation,
    session_stop_generation,
)
from kiro_crew.messaging.dispatch_tools import (
    TOOLLESS_TURN_AGENT,
    TOOLLESS_TURN_REFUSAL_NOTE,
    ToollessTurnUnavailable,
    _refuse_unless_toolless,
    _toolless_turn_work_dir,
    _without_deny_reason,
    build_auto_approve,
    build_tool_gate,
)
from kiro_crew.messaging.driver import APPROVAL_INTERACTIVE, DirectiveConsumer, TurnDriver
from kiro_crew.messaging.identity import publish_turn_identity
from kiro_crew.messaging.inbound_spool import (
    spool_refused_turn,
)
from kiro_crew.messaging.link import (
    DM_SCOPE_UNIFIED,
    bind_origin_mirror,
    channel_namespace_of,
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
from kiro_crew.security import (
    redact,
    redact_credentials,
    redact_exfiltration_urls,
    redact_local_paths,
)
from kiro_crew.sel import sel

# Imported from the kiro_crew.session_allocation facade rather than through kiro_crew.session:
# this module deliberately types ``sessions`` as ``Any`` to stay off the session
# package's import graph, and session_allocation imports nothing from messaging,
# so this direction cannot cycle.
from kiro_crew.session_allocation import SessionBusyError, SessionClosingError

logger = logging.getLogger("kiro_crew.messaging.dispatch")


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

    async def _consume(kind: str, args: dict[str, Any]) -> bool:
        # Deferred import: the dashboard package imports every channel package
        # at boot, and the channel packages import this module (cycle).
        from kiro_crew.dashboard.session_directive_apply import (
            apply_session_directive_outcome,
        )

        state: Any = getattr(dispatcher, "dashboard_state", None)
        if state is None:
            state = _ChannelDirectiveState(sessions=sessions)
        outcome = await apply_session_directive_outcome(
            state,
            None,
            session_key,
            kind,
            args,
            producer_is_channel=True,
        )
        result = outcome.text
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
        # The driver reads this as the structured terminal-turn signal (see
        # ``TurnDriver.DirectiveConsumer``): a quiet end applied on a channel
        # turn owes the thread no empty-turn notice.
        return outcome.ends_turn

    return _consume


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
        agent = asker.agent or self._agent()
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
                    undelivered_watch_key=session_key,
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
            elif getattr(driver, "unfinished_turn", False) is True:
                # The handle ended a turn the backend never finished; the driver
                # already followed the partial text with the timeout notice. The
                # runtime still reads alive, so book a failure and reset the
                # session (keeping the conversation), as the compaction-failure
                # retry above does, or the next message waits on it again.
                logger.warning(
                    "%s: the turn for %s ended unfinished (%s); resetting the session",
                    channel,
                    session_key,
                    stop_reason,
                )
                # At its threshold the breaker resets the session inside
                # record_failure and answers True. A same-key message can claim a
                # successor while that reset is awaited, so a second reset here
                # would end the successor mid-turn: reset only when it did not.
                breaker_reset = await sessions.record_failure(session_key)
                verdict = Verdict.FAILED
                if breaker_reset is not True:
                    try:
                        await sessions.reset(session_key)
                    except Exception:
                        logger.warning(
                            "%s: session reset after an unfinished turn failed session=%s",
                            channel,
                            session_key,
                            exc_info=True,
                        )
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
            finally:
                # A cancellation landing inside the close is not an ``Exception``
                # (a channel whose turns run on its poll task cancels that task when
                # it closes), so the release cannot wait on the guard above.
                if _acquired:
                    sessions.release(session_key)
                if recovery:
                    # Closed LAST, after the permit is back: a message that waited
                    # behind the gap then finds an idle successor instead of a
                    # semaphore a later reset could pop from under it. Idempotent, so
                    # a turn that never opened one (or whose reset raised) costs a
                    # dictionary lookup.
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


if TYPE_CHECKING:  # every owner-defined name in _EXPORTS, for type checkers and IDEs
    from kiro_crew.messaging.dispatch_admission import (  # noqa: F401
        _CANCEL_ALIASES,
        _InboundCallbackAdmission,
        admit_inbound_callback,
        hold_inbound_callback,
        is_pure_cancel,
    )
    from kiro_crew.messaging.dispatch_contract import (  # noqa: F401
        DISCORD_DRIFT,
        PROCESS_GRANT,
    )
    from kiro_crew.messaging.dispatch_recovery import (  # noqa: F401
        _breaker_threshold,
    )
    from kiro_crew.messaging.dispatch_tools import (  # noqa: F401
        _provider_backend,
        toolless_turns_supported,
        warn_if_toolless_turns_unservable,
    )

if TYPE_CHECKING:  # names it serves from outside the family, from their homes
    from kiro_crew import (  # noqa: F401
        runtime_death,
    )
    from kiro_crew.agent_sdk.backends import (  # noqa: F401
        Routing,
        routing_for,
    )
    from kiro_crew.agent_sdk.drivers.acp_vocab import (  # noqa: F401
        is_runtime_death,
    )
    from kiro_crew.history import (  # noqa: F401
        transcript_stem,
    )
    from kiro_crew.hooks import (  # noqa: F401
        TOOL_AUTO_APPROVE,
        TOOL_DENY,
        event_is_spawn_run,
        hook_gate_kwargs,
    )
    from kiro_crew.messaging.identity import (  # noqa: F401
        channel_inbound_permitted,
    )
    from kiro_crew.messaging.inbound_spool import (  # noqa: F401
        InboundRoute,
        spool_refused_turn_sync,
    )
    from kiro_crew.messaging.link import (  # noqa: F401
        ChannelLink,
        canonical_key,
        is_channel_session_key,
        split_dm_session_key,
    )
    from kiro_crew.monitoring.completion import (  # noqa: F401
        MonitorCompletionHook,
    )
    from kiro_crew.monitoring.models import (  # noqa: F401
        MonitorDispatchResult,
    )
    from kiro_crew.safety_override import (  # noqa: F401
        safety_override,
    )
    from kiro_crew.start_priority import (  # noqa: F401
        StartPriority,
    )

# This module is the facade of the shared channel turn pipeline, and every name is reached
# through it as ``kiro_crew.messaging.dispatch.<name>``: a read is served by the module that
# binds the name, and a write -- ``monkeypatch.setattr``, ``mock.patch`` -- reaches every
# module holding it (:mod:`kiro_crew._facade_mirror`). Owners never import this module. The
# turn loop (``ChannelTurns``, ``drive_turn``), its compaction retry guard, the hook
# auto-reply and the directive consumer stay here. New work goes to the owner of its
# responsibility:
#
#   ``dispatch_contract`` -- what a dispatcher hands the pipeline and what it gets back
#   ``dispatch_tools`` -- which tools a turn may run, and the tool-less turn
#   ``dispatch_admission`` -- callback admission, the governance gate and the mute predicate
#   ``dispatch_crew_log`` -- opening a channel session's crew log
#   ``dispatch_recovery`` -- replaying an abandoned turn and charging a failed one
#: Owner modules, lowest layer first: the first holder of a name answers its reads.
_OWNERS = (
    "kiro_crew.messaging.dispatch_contract",
    "kiro_crew.messaging.dispatch_tools",
    "kiro_crew.messaging.dispatch_admission",
    "kiro_crew.messaging.dispatch_crew_log",
    "kiro_crew.messaging.dispatch_recovery",
)
for _owner in _OWNERS:
    _importlib.import_module(_owner)
#: Name the facade serves but does not bind -> the owner that answers a read of it.
_EXPORTS: dict[str, str] = {}
_EXPORTS.update(_facade_mirror.install(_sys.modules[__name__], _OWNERS, excluded=()).served)
_sys.modules[__name__].__class__ = _facade_mirror.MirroredModule
#: What a star import binds: every public name of the unsplit module's namespace.
__all__ = sorted(n for n in set(globals()) | set(_EXPORTS) if not n.startswith("_"))
