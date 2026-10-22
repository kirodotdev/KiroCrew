"""The runtime's spawn entry point.

``spawn`` under the cold-start admission, the one re-derive retry after a stale spawn
spec, and the revive of a runtime whose start failed before that retry.

Each function here whose first parameter is ``self`` is an ``AcpRuntime`` method: the class
binds it by name, so it reads and writes the runtime's own state on the instance, and a patch
of the method on the class or on an instance reaches every caller.

Instance fields written: ``_dead``, ``_death_expected``, ``_death_label``,
``_death_reason``, ``_death_summary``, ``_death_tail``, ``_process_tree_confirmed_dead``,
``_saw_auth_failure``, ``_saw_delegate_launch_failure``, ``_saw_sandbox_init_failure``, ``_scratch_dir``,
``_stall_turn_sessions``, ``_stderr_lines``, ``_stdin_stall_death``.

Instance fields read: ``_acp_backend``, ``_answer_tasks``, ``_initialized``, ``_process``,
``_session_queues``.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Callable

from kiro_crew import runtime_death
from kiro_crew.acp import runtime_start
from kiro_crew.acp.session_handle import AcpRuntimeError
from kiro_crew.start_priority import START_QUEUE_ADMISSION, StartPriority, notify_start_queue

if TYPE_CHECKING:
    from kiro_crew.acp.runtime import AcpRuntime

#: The runtime's logger: this code logged under that name before the split.
logger = logging.getLogger("kiro_crew.acp.runtime")


async def spawn(
    self: AcpRuntime,
    *,
    start_priority: StartPriority = StartPriority.BACKGROUND,
    on_gate_queued: Callable[..., None] | None = None,
    on_gate_acquired: Callable[..., None] | None = None,
) -> None:
    """Start the ACP runtime behind the gateway-wide cold-start admission gate.

    ``start_priority`` orders this spawn in the admission queue (rule:
    ``kiro_crew.start_priority``). ``on_gate_queued(queue)`` /
    ``on_gate_acquired(queue_wait_ms, queue)`` bracket the admission wait, with
    ``queue`` = :data:`START_QUEUE_ADMISSION`, as they bracket the
    ``session/new`` gate in :meth:`create_session`.
    """
    if self._process is not None:
        raise AcpRuntimeError("Runtime already spawned")
    self._process_tree_confirmed_dead = False

    admission = runtime_start._cold_start_admission()
    notify_start_queue(logger, on_gate_queued, START_QUEUE_ADMISSION)
    wait_ms = await admission.acquire(start_priority)
    started = time.monotonic()
    outcome = "error"
    try:
        # Inside the try: the permit is released below whatever the callback does.
        notify_start_queue(logger, on_gate_acquired, wait_ms, START_QUEUE_ADMISSION)
        logger.info(
            "acp_cold_start stage=queue_wait outcome=admitted priority=%s wait_ms=%.1f "
            "active_starts=%d queued_starts=%d %s",
            start_priority.value,
            wait_ms,
            admission.active,
            admission.queued,
            admission.semaphore.describe(),
        )
        await self._spawn_admitted_rederiving_once()
        outcome = "ready"
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    finally:
        process = self._process
        if process is None:
            process_state = "absent"
        elif process.returncode is None:
            process_state = "running"
        else:
            process_state = "exited"
        logger.info(
            "acp_cold_start stage=spawn outcome=%s duration_ms=%.1f backend=%s "
            "active_starts=%d queued_starts=%d process_state=%s",
            outcome,
            (time.monotonic() - started) * 1000.0,
            self._acp_backend or "kiro",
            admission.active,
            admission.queued,
            process_state,
        )
        admission.release(start_priority)


async def _spawn_admitted_rederiving_once(self: AcpRuntime) -> None:
    """``_spawn_admitted``, retried ONCE when the post-handshake bracket fires.

    The bracket around a derived spec's load fails CLOSED: a write to the default
    spec landing between the pre-spawn gate and the ``initialize`` response kills
    the child, because the spec it loaded may be a generation nobody verified. That
    is the right answer for a revocation. It is the WRONG surface for a benign write
    -- the dashboard's MCP sync, an app registration, the periodic rebuild -- whose
    only effect on the worker is that its mirror needs re-deriving, and which
    happens to land inside a spawn's few-hundred-millisecond window. Those are
    ordinary events, and surfacing each as a failed dispatch would make the fleet
    flaky exactly when an operator is changing things.

    So: one retry, and only for THAT refusal. The second attempt runs the pre-spawn
    gate again, which re-derives the mirror from the default as it now stands, and
    spawns a fresh child on it. If the default is still moving the second bracket
    fires too and the refusal propagates -- a file that will not hold still across
    two spawns is not a benign write. Never a loop: the exit condition is another
    process leaving the file alone.

    Narrow on purpose. ``DerivedSpecStale`` from ``_initialize`` is the post-check;
    the pre-spawn gate's own refusal (a mirror that CANNOT be re-derived) arrives as
    ``AcpRuntimeError`` and is not retried, because a second attempt would fail the
    same way for the same reason.

    The first attempt's reap is a ``kill()``, which marks this runtime dead, so the
    retry runs only after :meth:`_revive_after_failed_start` has cleared that death.
    When it cannot -- the reap did not confirm the first child gone, or a reply
    task the first child started would not stop -- the stale spec is reported as
    the failure, unretried: a second child beside a first one this handle still
    holds is exactly the leak the failed-start guard exists to prevent.
    """
    from kiro_crew.agent import DerivedSpecStale

    try:
        await self._spawn_admitted()
    except DerivedSpecStale as first:
        if not await self._revive_after_failed_start():
            logger.warning(
                "acp_cold_start stage=rederive outcome=not_retried backend=%s reason=%s: "
                "the failed start left a child or a reply task this runtime could not "
                "confirm stopped",
                self._acp_backend or "kiro",
                first,
            )
            raise AcpRuntimeError(str(first)) from first
        logger.info(
            "acp_cold_start stage=rederive outcome=retry backend=%s reason=%s",
            self._acp_backend or "kiro",
            first,
        )
        try:
            await self._spawn_admitted()
        except DerivedSpecStale as second:
            raise AcpRuntimeError(str(second)) from second


async def _revive_after_failed_start(self: AcpRuntime) -> bool:
    """Clear the death a failed start's reap left, so this handle can start again.

    The failed-start guard reaps the child with ``kill()``, and ``kill()`` marks
    the runtime dead; every request path refuses a dead runtime, so a second
    start on this handle would have its own ``initialize`` refused with "runtime
    is dead" and never reach the check it is retrying. A start that failed
    before ``initialize`` completed served no session, so its death has no
    tenant left to tell, and clearing it is what lets the same handle start
    again.

    Clears what the dead child left behind that a later reading would attribute
    to the next one: the death state and record, its stderr ring, the auth and
    sandbox latches armed from it, and its scratch directory, which the next
    launch would otherwise adopt as the session's tree. Fields the next spawn
    assigns before it reads them (pid, start identity, capabilities) are left
    for it to assign.

    The child's reply tasks are cancelled and awaited first. ``kill()`` does
    not stop them, and the runtime being dead is the only thing that keeps one
    of them -- a host-auth answer waiting on a token refresh -- from writing its
    reply into the next child's stdin under an id that child never sent.

    Returns False, changing nothing about the death, unless the reap dropped the
    process handle AND confirmed the root and its tracked descendants gone. A
    declined kill, a Windows tree drain still pending, or a root that outlived
    its signals all leave that unconfirmed, and spawning a second child beside
    the first is the leak the guard prevents. Also False once a session is
    registered, which a start that never initialized cannot have, and when a
    reply task outlives its cancellation.
    """
    if (
        not self._dead
        or self._process is not None
        or not self._process_tree_confirmed_dead
        or self._initialized
        or self._session_queues
    ):
        return False
    replies = [task for task in self._answer_tasks if not task.done()]
    for task in replies:
        task.cancel()
    if replies:
        _, still_running = await asyncio.wait(replies, timeout=self._REVIVE_ANSWER_DRAIN_TIMEOUT)
        if still_running:
            return False
    self._scratch_dir = None
    self._dead = False
    self._process_tree_confirmed_dead = False
    self._death_summary = None
    self._death_reason = ""
    self._death_label = ""
    self._death_tail = ""
    self._death_expected = False
    self._stdin_stall_death = False
    self._stall_turn_sessions = frozenset()
    self._stderr_lines.clear()
    self._saw_auth_failure = False
    self._saw_sandbox_init_failure = False
    self._saw_delegate_launch_failure = False
    runtime_death.retract(self)
    return True
