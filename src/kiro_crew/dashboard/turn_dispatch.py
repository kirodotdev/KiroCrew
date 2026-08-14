"""Guarded dispatch for dashboard chat turns.

Every chat turn runs under a wall-clock ceiling so a genuinely runaway turn
cannot pin a session forever. That ceiling was correct; the way it fired was
not. Each dispatch site wrapped the turn in ``asyncio.wait_for`` and attached
exactly one done-callback — ``state._background_tasks.discard``, a ``set``
method that ignores its argument's result. Nothing ever awaited the task or
called ``.exception()``, so when the ceiling fired the resulting
``TimeoutError`` was **never retrieved**: the turn simply stopped. No error
card, no row in the session transcript, and no log line the user would think
to look for — only a garbage-collection-time "Task exception was never
retrieved" message, emitted whenever the collector happened to run.

That made the failure indistinguishable from the agent going quiet. A babysit
loop polling a pull request reaches the ceiling routinely — ten review rounds
at roughly five minutes of waiting each, plus the tool time in between — so
this was an ordinary outcome of long-running work, not a rare edge case, and
its symptom was "the agent abandoned my PR without saying anything".

This module owns the one dispatch path. Keeping the ceiling, the clamp against
the transport's own timeout, and the visible-card guarantee together here is
deliberate: they were previously re-derived at each of several call sites, so a
new site could silently reintroduce the silent death by copying the old
``add_done_callback(discard)`` shape.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from contextvars import ContextVar
from typing import Any

from kiro_crew.acp.client import resolve_prompt_timeout
from kiro_crew.config.loader import (
    APPROVAL_TURN_MARGIN_SECS,
    TOOL_APPROVAL_TIMEOUT_MIN,
    KiroCrewConfig,
)
from kiro_crew.constants import CHAT_TURN_TIMEOUT, TOOL_APPROVAL_TIMEOUT

logger = logging.getLogger(__name__)

# Absolute ``loop.time()`` deadline of the turn currently running, published by
# :func:`_bounded_turn` so code INSIDE the turn can size its own waits against
# the budget that is actually left. ``None`` outside a bounded turn (the OpenAI
# compatibility path bounds its turn with a bare ``wait_for``, and unit tests
# call the resolvers directly), in which case callers fall back to the
# ceiling-relative bound.
_TURN_DEADLINE: ContextVar[float | None] = ContextVar("kirocrew_turn_deadline", default=None)


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
    follows a configured ceiling ABOVE the 2h default (plus a margin so this
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

    Deliberately distinct from :func:`format_turn_timeout_card`: the two used to
    be indistinguishable to the user because the approval window outlived the
    turn, so an unanswered prompt always surfaced as a generic turn timeout and
    the actual cause — and the fix, resending — was never stated.
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


def format_turn_timeout_card(timeout_secs: float) -> str:
    """User-facing text for a turn that hit the ceiling."""
    if timeout_secs >= 3600:
        limit = f"{timeout_secs / 3600:.1f}".rstrip("0").rstrip(".") + "-hour"
    else:
        limit = f"{max(1, round(timeout_secs / 60))}-minute"
    return (
        f"⏱️ This turn hit the {limit} limit and was stopped. Work already "
        "written to disk is still there — nothing was rolled back. Send a "
        "message to continue from where it stopped."
    )


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
        logger.warning(
            "Chat turn in slot %s hit the %.0fs ceiling", getattr(slot, "key", "?"), timeout_secs
        )
        try:
            # slot.append persists the card AND broadcasts it once; do not also
            # broadcast_ws here or the UI renders a duplicate.
            slot.append("error", format_turn_timeout_card(timeout_secs), "msg msg-err")
            state.push_slots_update()
        except Exception:
            logger.debug("Failed to render turn-timeout card", exc_info=True)
        return
    # Anything else escaped _run_chat's own handler chain. Log it rather than
    # let it die as an unretrieved task exception.
    logger.error(
        "Chat turn in slot %s failed: %s",
        getattr(slot, "key", "?"),
        exc,
        exc_info=exc,
    )


async def _bounded_turn(coro: "Coroutine[Any, Any, Any]", timeout_secs: float) -> Any:
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
    result. ``_run_chat``'s cancellation semantics are untouched — they are
    shared with the user's Stop button.
    """
    loop = asyncio.get_running_loop()
    deadline_fired = False

    def _on_deadline() -> None:
        nonlocal deadline_fired
        if not task.done():
            deadline_fired = True
            task.cancel()

    # Published so anything running INSIDE the turn can size its own waits
    # against the budget that is actually left, rather than against the
    # full-length ceiling. Set before the task is created so the task's context
    # copy carries it; reset in the finally so a direct `await _bounded_turn(...)`
    # cannot leak a spent deadline into the caller's context and starve the next
    # turn dispatched there.
    deadline_token = _TURN_DEADLINE.set(loop.time() + timeout_secs)
    task: "asyncio.Task[Any]" = asyncio.ensure_future(coro)
    handle = loop.call_later(timeout_secs, _on_deadline)
    try:
        try:
            result = await task
        except asyncio.CancelledError:
            if deadline_fired:
                # Our ceiling cut it and the turn let the cancellation through.
                raise TimeoutError(
                    f"turn exceeded the {timeout_secs:.0f}s ceiling"
                ) from None
            # Cancelled by something else (Stop button, shutdown) — propagate.
            raise
        if deadline_fired:
            # The turn swallowed our cancellation and returned normally. This is
            # the production path: without this the ceiling is silently absorbed.
            raise TimeoutError(f"turn exceeded the {timeout_secs:.0f}s ceiling")
        return result
    finally:
        handle.cancel()
        _TURN_DEADLINE.reset(deadline_token)
        if not task.done():
            # The wrapper itself was cancelled; don't orphan the turn.
            task.cancel()


async def bounded_chat_turn(coro: "Coroutine[Any, Any, Any]") -> Any:
    """Bound *coro* by the configured turn ceiling, resolved OFF the event loop.

    For task-creation sites that cannot ``await`` (sync callbacks, dispatch
    helpers): ``asyncio.create_task(bounded_chat_turn(_run_chat(...)))`` moves
    the :func:`chat_turn_timeout_secs` resolution — a ``KiroCrewConfig.load()``
    whose cache-miss path reads and re-validates the config file — into the
    task itself via ``asyncio.to_thread``, so an edited config never stalls
    the loop shared by every session.
    """
    timeout = await asyncio.to_thread(chat_turn_timeout_secs)
    return await asyncio.wait_for(coro, timeout=timeout)


def spawn_guarded_turn(
    state: Any,
    slot: Any,
    coro: "Coroutine[Any, Any, Any]",
    *,
    timeout_secs: float | None = None,
) -> "asyncio.Task[Any]":
    """Start *coro* as a ceiling-bounded turn whose failure is always visible.

    Registers the task in ``state._background_tasks`` (so it is not garbage
    collected mid-flight) and attaches :func:`finish_turn_task`, which both
    deregisters it and consumes its exception.

    Callers that also track the task (``slot.task``, a per-session map) should
    keep doing so; this helper deliberately does not own those fields, because
    they carry site-specific stop/steer semantics.
    """
    timeout = chat_turn_timeout_secs() if timeout_secs is None else timeout_secs
    task = asyncio.create_task(_bounded_turn(coro, timeout))
    state._background_tasks.add(task)
    task.add_done_callback(lambda t: finish_turn_task(state, slot, t, timeout))
    return task
