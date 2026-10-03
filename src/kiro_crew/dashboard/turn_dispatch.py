"""Guarded dispatch for dashboard chat turns.

Every chat turn runs under a wall-clock ceiling so a genuinely runaway turn
cannot pin a session forever. Reaching that ceiling must always produce a
VISIBLE outcome: an error card and a row in the session transcript. A dispatch
that only wrapped the turn in ``asyncio.wait_for`` and attached
``state._background_tasks.discard`` — a ``set`` method that ignores its
argument's result — never retrieves the resulting ``TimeoutError``, so the turn
simply stops, leaving nothing but a garbage-collection-time "Task exception was
never retrieved" message and a failure indistinguishable from the agent going
quiet.

The ceiling is an ordinary outcome of long-running work, not a rare edge case: a
babysit loop polling a pull request reaches it routinely — ten review rounds at
roughly five minutes of waiting each, plus the tool time in between.

This module owns the one dispatch path. Keeping the ceiling, the clamp against
the transport's own timeout, and the visible-card guarantee together here is
deliberate: re-deriving them at each call site lets a new site silently
reintroduce the silent death by copying an ``add_done_callback(discard)`` shape.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Coroutine
from contextvars import ContextVar
from typing import Any

from kiro_crew.acp.client import resolve_prompt_timeout
from kiro_crew.config.loader import (
    APPROVAL_TURN_MARGIN_SECS,
    TOOL_APPROVAL_TIMEOUT_MIN,
    KiroCrewConfig,
)
from kiro_crew.constants import CHAT_TURN_TIMEOUT, TOOL_APPROVAL_TIMEOUT
from kiro_crew.providers.base import CancelOutcome

logger = logging.getLogger(__name__)

# Absolute ``loop.time()`` deadline of the turn currently running, published by
# :func:`_bounded_turn` so code INSIDE the turn can size its own waits against
# the budget that is actually left. ``None`` outside a bounded turn (the OpenAI
# compatibility path bounds its turn with a bare ``wait_for``, and unit tests
# call the resolvers directly), in which case callers fall back to the
# ceiling-relative bound.
_TURN_DEADLINE: ContextVar[float | None] = ContextVar("kirocrew_turn_deadline", default=None)

_NativeTimeoutCancel = Callable[["asyncio.Task[Any]"], Awaitable["CancelOutcome | None"]]


class TurnTimeoutError(TimeoutError):
    """A wall-clock turn deadline, including native cancellation evidence."""

    def __init__(
        self,
        timeout_secs: float,
        native_cancel_outcome: "CancelOutcome | None" = None,
    ) -> None:
        super().__init__(f"turn exceeded the {timeout_secs:.0f}s ceiling")
        self.timeout_secs = timeout_secs
        self.native_cancel_outcome = native_cancel_outcome


def _turn_budget_remaining() -> float | None:
    """Seconds left in the running turn, or ``None`` when that is unknowable."""
    deadline = _TURN_DEADLINE.get()
    if deadline is None:
        return None
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop: the deadline is on the same clock as ``loop.time()`` and
        # cannot be compared to anything here.
        return None
    return deadline - loop.time()


def _acp_prompt_ceiling() -> float:
    """The transport's own per-prompt timeout.

    Resolves through :func:`~kiro_crew.acp.client.resolve_prompt_timeout`, which
    follows a configured ceiling ABOVE the 4h default (plus a margin so this
    module's card always fires before the transport cut). The clamp in
    :func:`chat_turn_timeout_secs` therefore no longer fires in normal operation;
    it stays as the fail-safe for a resolver that could not read config.

    A function rather than a bare constant reference so tests can substitute it
    without reaching into ``acp.client``.
    """
    return float(resolve_prompt_timeout())


def chat_turn_timeout_secs() -> float:
    """Resolve the wall-clock ceiling for one chat turn.

    Reads ``agent.chat_turn_timeout_secs``, falling back to
    :data:`CHAT_TURN_TIMEOUT` when config is unavailable (tests, early
    bootstrap) so a config-less context behaves exactly like a default config.

    The value is clamped to the ACP transport's own prompt timeout. Configuring
    a dashboard ceiling above it cannot take effect — the transport times out
    first and the turn dies there instead — so accepting the larger number
    would report a limit the system does not honour. The clamp is logged at
    warning level so the misconfiguration is visible rather than silently
    ignored.
    """
    try:
        configured = float(KiroCrewConfig.load().agent.chat_turn_timeout_secs)
    except Exception:
        logger.debug("chat-turn ceiling config unavailable; using default", exc_info=True)
        return CHAT_TURN_TIMEOUT
    if configured <= 0:
        # The ceiling is a runaway backstop and cannot be disabled; a
        # non-positive value would make wait_for raise immediately.
        return CHAT_TURN_TIMEOUT
    acp_ceiling = _acp_prompt_ceiling()
    if configured > acp_ceiling:
        logger.warning(
            "agent.chat_turn_timeout_secs=%.0fs exceeds the ACP prompt timeout "
            "(%.0fs); clamping. The transport bounds the turn first, so the "
            "larger value cannot take effect.",
            configured,
            acp_ceiling,
        )
        return acp_ceiling
    return configured


def native_cancel_ack_timeout_secs() -> float:
    """Resolve the bounded wait for a native turn-cancellation acknowledgement."""
    try:
        configured = float(KiroCrewConfig.load().agent.soft_stop_budget_secs)
    except Exception:
        logger.debug(
            "native cancel acknowledgement config unavailable; using default", exc_info=True
        )
        return 10.0
    if configured <= 0:
        return 10.0
    return min(60.0, max(0.5, configured))


def _owned_native_timeout_cancel(
    state: Any,
    slot: Any,
    wait_ack_timeout: float,
) -> _NativeTimeoutCancel:
    """Build the exact-owner native cancellation used by a turn deadline."""

    async def _cancel(owner_task: "asyncio.Task[Any]") -> "CancelOutcome | None":
        session_key = getattr(slot, "_active_turn_session_key", "")
        if not isinstance(session_key, str) or not session_key:
            return None
        sessions = getattr(state, "sessions", None)
        cancel_owned = getattr(sessions, "cancel_owned_turn", None)
        if not callable(cancel_owned):
            return None
        outcome = await cancel_owned(
            session_key,
            owner_task,
            wait_ack_timeout=wait_ack_timeout,
        )
        if outcome not in (None, "acked", "timeout", "no_turn", "error"):
            logger.warning("Unknown native cancellation outcome for %s: %r", session_key, outcome)
            return "error"
        return outcome

    return _cancel


def tool_approval_timeout_secs() -> float:
    """Resolve how long a turn waits for a human to answer an approval prompt.

    Reads ``agent.tool_approval_timeout_secs``, falling back to
    :data:`TOOL_APPROVAL_TIMEOUT` when config is unavailable (tests, early
    bootstrap) so a config-less context behaves exactly like a default config.

    The value is bounded twice, because two different things can make it
    outlive the turn it belongs to:

    * against the turn ceiling :func:`chat_turn_timeout_secs` resolves. The
      config loader already applies this to the two config fields; repeating it
      here catches a RESOLVED ceiling lower than the configured one (the ACP
      transport's prompt timeout clamps it).
    * against the budget REMAINING in the turn that is already running. A prompt
      arming late in a long agentic turn has far less than the full ceiling left,
      so a ceiling-relative bound alone still lets the turn die first — the exact
      failure this window exists to prevent. Returns ``0.0`` when less than the
      margin remains: there is no window that can both wait and report, so the
      caller declines immediately instead of pretending to wait.
    """
    try:
        configured = float(KiroCrewConfig.load().agent.tool_approval_timeout_secs)
    except Exception:
        logger.debug("approval-window config unavailable; using default", exc_info=True)
        configured = TOOL_APPROVAL_TIMEOUT
    if configured <= 0:
        # An approval prompt cannot be disabled by zeroing its window — that
        # would make wait_for raise immediately and auto-decline every tool.
        configured = TOOL_APPROVAL_TIMEOUT
    ceiling = chat_turn_timeout_secs()
    budget = max(float(TOOL_APPROVAL_TIMEOUT_MIN), ceiling - APPROVAL_TURN_MARGIN_SECS)
    window = configured
    if window > budget:
        logger.warning(
            "agent.tool_approval_timeout_secs=%.0fs leaves less than %ds under the "
            "resolved %.0fs turn ceiling; capping at %.0fs so the approval deadline "
            "lands inside the turn.",
            configured,
            APPROVAL_TURN_MARGIN_SECS,
            ceiling,
            budget,
        )
        window = budget
    remaining = _turn_budget_remaining()
    if remaining is None:
        return window
    arm_budget = max(0.0, remaining - APPROVAL_TURN_MARGIN_SECS)
    if arm_budget < window:
        # Normal operation on a long turn, not a misconfiguration.
        logger.info(
            "Approval window shortened from %.0fs to %.0fs: %.0fs left in the turn.",
            window,
            arm_budget,
            remaining,
        )
        return arm_budget
    return window


def format_approval_no_budget_card() -> str:
    """User-facing text for a prompt that arrived with no time left to answer it.

    Distinct from :func:`format_approval_timeout_card` because nothing was
    waited on: the turn was already close enough to its ceiling that any wait
    would have been cut by the ceiling and misreported as a turn timeout.
    """
    return (
        "⏱️ A tool needed your approval, but this turn was too close to its time "
        "limit to wait for an answer, so I declined it on your behalf and carried "
        "on from the denial — nothing was rolled back. If you wanted that tool to "
        "run, send the message again: the fresh turn has a full window to ask you "
        "properly."
    )


def format_approval_timeout_card(timeout_secs: float) -> str:
    """User-facing text for an approval prompt nobody answered in time.

    Deliberately distinct from :func:`format_turn_timeout_card`: the approval
    window can outlive the turn, and one shared card would surface an unanswered
    prompt as a generic turn timeout, stating neither the actual cause nor the
    fix, which is resending.
    """
    if timeout_secs >= 3600:
        waited = f"{timeout_secs / 3600:.1f}".rstrip("0").rstrip(".") + " hours"
    else:
        waited = f"{max(1, round(timeout_secs / 60))} minutes"
    return (
        f"⏱️ A tool needed your approval and nothing came back within {waited}, "
        "so I declined it on your behalf and carried on from the denial — nothing "
        "was rolled back. If you wanted that tool to run, send the message again "
        "and approve the prompt when it appears, or turn on YOLO mode first for an "
        "unattended run."
    )


def format_turn_timeout_card(
    timeout_secs: float,
    native_cancel_outcome: "CancelOutcome | None" = None,
) -> str:
    """User-facing text for a turn that hit the ceiling."""
    if timeout_secs >= 3600:
        limit = f"{timeout_secs / 3600:.1f}".rstrip("0").rstrip(".") + "-hour"
    else:
        limit = f"{max(1, round(timeout_secs / 60))}-minute"
    if native_cancel_outcome in ("timeout", "no_turn", "error"):
        return (
            f"⏱️ This turn hit the {limit} limit. The native agent did not "
            "acknowledge the stop request, so its previous tool may still be "
            "finishing. Do not resume this session until it becomes idle. Work "
            "already written to disk is still there — nothing was rolled back."
        )
    return (
        f"⏱️ This turn hit the {limit} limit and was stopped. Work already "
        "written to disk is still there — nothing was rolled back. Send a "
        "message to continue from where it stopped."
    )


def _report_turn_timeout(
    state: Any,
    slot: Any,
    timeout_secs: float,
    native_cancel_outcome: "CancelOutcome | None" = None,
) -> None:
    """Record and surface one wall-clock timeout outcome."""
    logger.warning(
        "Chat turn in slot %s hit the %.0fs ceiling", getattr(slot, "key", "?"), timeout_secs
    )
    try:
        from kiro_crew.metrics.events import TURN_TIMEOUT_CAUSE, emit_counter

        emit_counter(
            TURN_TIMEOUT_CAUSE,
            {
                "path": "dashboard_ceiling",
                "awaiting_permission": bool(getattr(slot, "_last_turn_awaiting_permission", False)),
                "children_announced": bool(getattr(slot, "_last_turn_children_announced", False)),
            },
        )
    except Exception:
        logger.debug("timeout-cause metric emit failed", exc_info=True)
    try:
        # slot.append persists the card AND broadcasts it once; do not also
        # broadcast_ws here or the UI renders a duplicate.
        slot.append(
            "error",
            format_turn_timeout_card(timeout_secs, native_cancel_outcome),
            "msg msg-err",
        )
        state.push_slots_update()
    except Exception:
        logger.debug("Failed to render turn-timeout card", exc_info=True)


def finish_turn_task(
    state: Any,
    slot: Any,
    task: "asyncio.Task[Any]",
    timeout_secs: float,
) -> None:
    """Retrieve *task*'s outcome so a ceiling hit becomes a visible card.

    Replaces the bare ``state._background_tasks.discard`` callback. Still
    discards, but also consumes the exception — which is what turns a silent
    death into something the user can see.
    """
    state._background_tasks.discard(task)
    if task.cancelled():
        # Cooperative stop or shutdown. Already surfaced by _run_chat's own
        # CancelledError handler; a second card would be noise.
        return
    try:
        exc = task.exception()
    except asyncio.CancelledError:  # pragma: no cover - racing cancellation
        return
    if exc is None:
        return
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        _report_turn_timeout(
            state,
            slot,
            timeout_secs,
            exc.native_cancel_outcome if isinstance(exc, TurnTimeoutError) else None,
        )
        return
    # Anything else escaped _run_chat's own handler chain. Log it rather than
    # let it die as an unretrieved task exception.
    logger.error(
        "Chat turn in slot %s failed: %s",
        getattr(slot, "key", "?"),
        exc,
        exc_info=exc,
    )


async def _bounded_turn(
    coro: "Coroutine[Any, Any, Any]",
    timeout_secs: float,
    *,
    _started: "list[None] | None" = None,
    native_cancel: "_NativeTimeoutCancel | None" = None,
    cancel_ack_timeout_secs: float = 10.0,
    background_tasks: "set[asyncio.Task[Any]] | None" = None,
) -> Any:
    """Run *coro* under a wall-clock ceiling, raising even on a suppressed deadline.

    ``asyncio.wait_for`` cannot be used directly here. It cancels the coroutine
    when the deadline fires and then re-raises whatever the coroutine did with
    that cancellation — and ``_run_chat`` **catches** ``CancelledError``: it
    flushes the partial assistant output and returns normally. The cancellation
    is absorbed, ``wait_for`` hands back a value instead of raising, and the
    caller cannot tell a turn that finished from one killed at the ceiling, so
    the card this module promises never appears. (Same on 3.10's ``wait_for``
    and on the 3.12 ``timeouts``-based rewrite: both only convert a
    cancellation that actually propagates.)

    So the deadline is armed here and recorded in ``deadline_fired``, which is
    set ONLY by this function's own timer. That makes "was this turn cut?" an
    observed fact rather than an inference from elapsed wall-clock: a clock
    comparison would mislabel a turn that completed just under the deadline but
    whose continuation was delayed by a congested loop, discarding a real
    result. When a native cancellation callback is supplied, the deadline sends
    that request against the exact lease owner and waits for its bounded outcome
    before cancelling the Python task. That ordering keeps ACP active-turn state
    available long enough for ``session/cancel`` to reach the native runtime.
    """
    # ``spawn_guarded_turn`` uses this yield-free marker to distinguish a
    # wrapper that claimed its input coroutine from one cancelled before its
    # first event-loop step.  In the latter case this function's ``finally``
    # never runs, so the dispatch helper must close the still-unclaimed input.
    if _started is not None:
        _started.append(None)

    loop = asyncio.get_running_loop()
    deadline_fired = False
    deadline_task: "asyncio.Task[CancelOutcome | None] | None" = None
    deadline_outcome: "CancelOutcome | None" = None

    async def _cancel_native_then_unwind() -> "CancelOutcome | None":
        owner_task = task
        if owner_task is None:
            return None
        outcome: "CancelOutcome | None" = None
        try:
            if native_cancel is not None:
                acknowledgement_bound = max(0.1, float(cancel_ack_timeout_secs)) + 0.5
                try:
                    outcome = await asyncio.wait_for(
                        native_cancel(owner_task),
                        timeout=acknowledgement_bound,
                    )
                except asyncio.TimeoutError:
                    outcome = "timeout"
                except Exception:
                    logger.warning("Native turn cancellation failed at deadline", exc_info=True)
                    outcome = "error"
        finally:
            # Only after the native request has settled (or exhausted its hard
            # bound) may the dashboard unwind the stream that carries ACP's
            # active-turn marker.
            if not owner_task.done():
                owner_task.cancel()
        return outcome

    def _on_deadline() -> None:
        nonlocal deadline_fired, deadline_task, deadline_outcome
        if task is None or task.done():
            return
        deadline_fired = True
        try:
            deadline_task = loop.create_task(_cancel_native_then_unwind())
        except RuntimeError:
            # A closing loop cannot run the native request. Preserve the old
            # finite-ceiling guarantee and report the missing acknowledgement.
            deadline_outcome = "error" if native_cancel is not None else None
            task.cancel()
            return
        if background_tasks is not None:
            background_tasks.add(deadline_task)
            deadline_task.add_done_callback(background_tasks.discard)

    async def _settle_deadline_cancel() -> "CancelOutcome | None":
        if deadline_task is None:
            return deadline_outcome
        # A second cancellation belongs to the wrapper, not to the native send
        # already in flight. Keep waiting for the bounded task; it remains in the
        # caller's background-task set even if teardown cancels this wrapper.
        while not deadline_task.done():
            try:
                await asyncio.shield(deadline_task)
            except asyncio.CancelledError:
                if not deadline_fired:
                    raise
        if deadline_task.cancelled():
            return "error"
        try:
            return deadline_task.result()
        except Exception:
            logger.warning("Native turn cancellation task failed", exc_info=True)
            return "error"

    # Published so anything running INSIDE the turn can size its own waits
    # against the budget that is actually left, rather than against the
    # full-length ceiling. Set before the task is created so the task's context
    # copy carries it; reset in the finally so a direct `await _bounded_turn(...)`
    # cannot leak a spent deadline into the caller's context and starve the next
    # turn dispatched there.
    previous_deadline = _TURN_DEADLINE.get()
    _TURN_DEADLINE.set(loop.time() + timeout_secs)
    task: "asyncio.Task[Any] | None" = None
    handle: "asyncio.TimerHandle | None" = None
    _generator_exit = False
    try:
        try:
            task = asyncio.ensure_future(coro)
        except BaseException:
            # Ownership was not transferred to a Task.
            coro.close()
            raise

        # Keep timer creation inside the same ownership transaction.  Although
        # the real event loop almost never rejects ``call_later``, a closing or
        # instrumented loop can: the just-created Task must not outlive that
        # failed dispatch.
        handle = loop.call_later(timeout_secs, _on_deadline)
        try:
            # Shield keeps cancellation of this wrapper from unwinding the ACP
            # stream before the deadline's native-cancel phase can run. External
            # Stop/shutdown still cancels the inner task explicitly below.
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if deadline_fired:
                outcome = await _settle_deadline_cancel()
                raise TurnTimeoutError(timeout_secs, outcome) from None
            # Cancelled by something else (Stop button, shutdown) — propagate,
            # after explicitly ending the owned inner task.
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass
            raise
        if deadline_fired:
            # The native cancel may have made the stream return normally, or
            # _run_chat may have absorbed the local cancellation after it. The
            # deadline remains authoritative in either case.
            outcome = await _settle_deadline_cancel()
            raise TurnTimeoutError(timeout_secs, outcome)
        return result
    except GeneratorExit:
        # This wrapper is being torn down directly -- its own coroutine object
        # is being ``close()``-d, which happens when nothing ever awaited or
        # cancelled it through a live Task and the garbage collector reclaims
        # it instead (an orphaned ``spawn_guarded_turn`` dispatch nobody
        # joined). Unlike a live ``CancelledError`` unwind, there is no
        # guarantee the event loop that would drive ``task`` to completion is
        # even still running -- ``close()`` resumes this frame synchronously
        # from whatever thread the collector runs on, not from a loop
        # callback. A coroutine that suspends again while unwinding a
        # GeneratorExit gets "coroutine ignored GeneratorExit" from the
        # interpreter, so ``_generator_exit`` below tells the ``finally`` to
        # skip the join and only cancel best-effort.
        _generator_exit = True
        raise
    finally:
        if handle is not None:
            handle.cancel()
        # Restore by value instead of retaining a ContextVar token. Test and
        # shutdown harnesses may resume coroutine finalization in a copied
        # Context (notably Windows xdist); reset(token) then raises and can take
        # down the whole worker because tokens are context-bound.
        _TURN_DEADLINE.set(previous_deadline)
        if task is not None and not task.done():
            # The wrapper itself was cancelled, or setup failed after Task
            # creation.  Cancel AND join it: cancellation alone can leave an
            # unstarted coroutine pending until a later GC cycle.
            try:
                task.cancel()
            except RuntimeError:
                # The loop that owned ``task`` is already closed (the
                # GeneratorExit case, or a shutdown race). Nothing left to
                # schedule the cancellation on.
                pass
            if not _generator_exit:
                try:
                    await task
                except BaseException:
                    # Cleanup must preserve the exception already leaving the
                    # wrapper (setup failure or caller cancellation).
                    pass


async def bounded_chat_turn(
    coro: "Coroutine[Any, Any, Any]",
    *,
    state: Any = None,
    slot: Any = None,
) -> Any:
    """Bound *coro* by the configured turn ceiling, resolved off the event loop.

    ``state`` and ``slot`` enable the exact-owner native cancellation phase.
    They remain optional for compatibility with non-session utility callers.
    """
    timeout, cancel_ack_timeout = await asyncio.to_thread(
        lambda: (chat_turn_timeout_secs(), native_cancel_ack_timeout_secs())
    )
    native_cancel = (
        _owned_native_timeout_cancel(state, slot, cancel_ack_timeout)
        if state is not None and slot is not None
        else None
    )
    background_tasks = getattr(state, "_background_tasks", None)
    try:
        return await _bounded_turn(
            coro,
            timeout,
            native_cancel=native_cancel,
            cancel_ack_timeout_secs=cancel_ack_timeout,
            background_tasks=background_tasks if isinstance(background_tasks, set) else None,
        )
    except TurnTimeoutError as exc:
        if state is not None and slot is not None:
            _report_turn_timeout(state, slot, exc.timeout_secs, exc.native_cancel_outcome)
        raise


def spawn_guarded_turn(
    state: Any,
    slot: Any,
    coro: "Coroutine[Any, Any, Any]",
    *,
    timeout_secs: float | None = None,
    cancel_ack_timeout_secs: float | None = None,
) -> "asyncio.Task[Any]":
    """Start *coro* as a ceiling-bounded turn whose failure is always visible.

    Registers the task in ``state._background_tasks`` (so it is not garbage
    collected mid-flight) and attaches :func:`finish_turn_task`, which both
    deregisters it and consumes its exception.

    Callers that also track the task (``slot.task``, a per-session map) should
    keep doing so; this helper deliberately does not own those fields, because
    they carry site-specific stop/steer semantics.
    """
    started: list[None] = []
    bounded = None
    try:
        timeout = chat_turn_timeout_secs() if timeout_secs is None else timeout_secs
        cancel_ack_timeout = (
            native_cancel_ack_timeout_secs()
            if cancel_ack_timeout_secs is None
            else cancel_ack_timeout_secs
        )
        background_tasks = getattr(state, "_background_tasks", None)
        bounded = _bounded_turn(
            coro,
            timeout,
            _started=started,
            native_cancel=_owned_native_timeout_cancel(state, slot, cancel_ack_timeout),
            cancel_ack_timeout_secs=cancel_ack_timeout,
            background_tasks=background_tasks if isinstance(background_tasks, set) else None,
        )
        task = asyncio.create_task(bounded)
    except BaseException:
        # The caller created ``coro`` before entering this helper.  If timeout
        # resolution or task creation fails, no Task owns either coroutine.
        if bounded is not None:
            bounded.close()
        coro.close()
        raise
    state._background_tasks.add(task)

    def _finish(t: "asyncio.Task[Any]") -> None:
        if not started:
            # A Task cancelled before its first loop step closes the bounded
            # wrapper but not the input coroutine stored in its arguments.
            coro.close()
        finish_turn_task(state, slot, t, timeout)

    task.add_done_callback(_finish)
    return task
