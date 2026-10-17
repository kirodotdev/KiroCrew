"""Cancelling persisted rows that have not started.

One row, inline or run whole on the writer thread; every row of a bulk stop as one writer-thread
job; and the claimed-not-started probe a parent-end teardown asks before it reaps a run.

A function here whose first parameter is ``self`` is a ``_TaskqBridgeMixin`` method: the class
binds it under the same name in ``admission/taskq_bridge.py``, so it reaches the manager through
``self._manager`` and its sibling members through ``self``, and a class-level patch of a member
reaches every call made through ``self`` or the class.

This module uses no manager field directly: it reaches the store through
``taskq_store``, except that :func:`taskq_post_cancel_queued` reads ``_retained_claims``.
Writes: none. Reads: ``_retained_claims``.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging as _logging
from typing import TYPE_CHECKING, Any, Collection, Sequence

if TYPE_CHECKING:
    # Typing only: the facade imports this module at run time.
    from .taskq_bridge import _TaskqBridgeMixin

_glue_logger = _logging.getLogger("kiro_crew.subagent_manager.admission")


# How many times a single row's state is re-read before the answer is given up on, and the
# pause between tries. Same shape and reasoning as the durable sweep's own bound: an error
# reading one row is normally writer-thread contention, not an outage.
_STATE_READ_ATTEMPTS = 3
_STATE_READ_BACKOFF_SECS = 0.2


async def taskq_cancel_queued_async(
    self: _TaskqBridgeMixin, agent_id: str, *, allow_admitted: bool = True, retained: bool = False
) -> dict[str, Any] | None:
    """:meth:`taskq_cancel_queued` run whole on the writer thread.

    The sync twin is reached from ``_unqueue`` on paths already off the loop. A
    parent-end teardown is not one of them: it runs as a coroutine inside the
    session lifecycle, so ``store.get``/``store.cancel`` there stall the gateway
    loop for as long as the task store is contended — the
    ``no-sync-store-call-from-a-coroutine`` rule.

    DELEGATION, not a second copy. ``store.run`` takes any callable and runs it on
    the writer thread, so the twin's own body runs there unchanged: one hop instead
    of two, and the race-safety argument in its docstring — the state test and the
    cancel sharing one ``only_from`` under the generation the read returned — is the
    same code rather than the same intent restated. Restating it is what would let
    the two drift, and a drift here reads as "cancelled row with a running spawn
    under it" on exactly one of the two paths.
    """
    store = self.taskq_store()
    if store is None:
        return None
    return await store.run(
        self.taskq_cancel_queued, agent_id, allow_admitted=allow_admitted, retained=retained
    )


async def taskq_row_is_claimed_unstarted_async(self: _TaskqBridgeMixin, agent_id: str) -> bool:
    """True only when *agent_id*'s row is CLAIMED and not started (``admitted``).

    This is the one state a parent-end teardown must not reap through the live path:
    the row's claimer sits between its claim and its registration, so stopping it here
    is the act the store's own state gate just refused, reached through another door.
    Every other answer -- started, missing, unreadable -- takes the reap.

    A row that cannot be read answers ``False`` for "claimed", because an unknown is
    not a claim. Folding the two together spared a LIVE run whenever the store was
    briefly unreadable, leaving it working against a conversation that has ended --
    the failure this path exists to prevent. The narrow exemption needs positive
    evidence, so only the state that names it earns it.

    The read is retried on the same bound the durable sweep uses: an error here is
    normally writer-thread contention rather than a real outage.
    """
    # The same deferred import the rest of the bridge uses, rather than
    # a module-scope one: this file is reached from the gateway boot path, and the
    # convention there is that a flag-gated subsystem's IMPORT is deferred too. Spelled
    # as the package alias so this line has the shape its siblings do.
    from kiro_crew import taskq as _taskq_model

    store = self.taskq_store()
    if store is None:
        return False
    for attempt in range(_STATE_READ_ATTEMPTS):
        try:
            rec = await store.run(store.get, agent_id)
        except Exception:
            if attempt + 1 < _STATE_READ_ATTEMPTS:
                await _asyncio.sleep(_STATE_READ_BACKOFF_SECS)
                continue
            _glue_logger.warning(
                "taskq: could not read the state of %s in %d attempts; treating it as "
                "not claimed so a live run is not spared by a store outage",
                agent_id,
                _STATE_READ_ATTEMPTS,
                exc_info=True,
            )
            return False
        return rec is not None and rec.state == _taskq_model.ADMITTED
    return False


def taskq_cancel_queued(
    self: _TaskqBridgeMixin, agent_id: str, *, allow_admitted: bool = True, retained: bool = False
) -> dict[str, Any] | None:
    """Cancel a persisted row that has not started; returns its params for the report.

    *retained* says *agent_id* is a claim held in ``_retained_claims``, read by
    the caller on the loop: such a claim has no run and no claimer in flight,
    so with ``allow_admitted`` its ``starting`` row is this process's unstarted
    work exactly like a retained ``admitted`` one, and is cancelled too.

    ``allow_admitted=False`` refuses a row that has been CLAIMED but not started.
    Stop-all leaves it True: the user pressed Stop, and a claimed-not-started row is
    work they asked to end; the claimer's ``admitted -> starting`` compare-and-set
    (``claim_and_start``) is refused for a row cancelled here, and a row it has
    already moved is ``starting``, which this path leaves to the live reap. A
    parent-end teardown passes False: a row in that
    window may be carrying a decision a person made (a spawn approval is the visible
    case), which a teardown has no standing to revoke on their behalf. Refusing
    leaves the row to the incarnation that owns it.

    Race-safe against the drain because the STATE TEST AND THE CANCEL SHARE
    ONE TRANSACTION: ``unstarted`` is both the predicate this code judges the
    row it read by and the ``only_from`` the store re-tests inside the cancel,
    so a row a drain claimed and started in between answers None here and the
    caller falls through to the live reap -- never a ``cancelled`` row with a
    running spawn under it. A row claimed but not started (``admitted``) is
    still this path's: the spawn's own start write is fenced on the
    generation this cancel bumps, so it is refused and stops there.

    The cancel is deliberately NOT fenced on the read's generation. A claim, a
    requeue or a wake can bump the generation while the row stays unstarted
    (``queued`` -> ``admitted``, a wait woken into ``retry_wait``); a generation
    fence refused that cancel as ``stale_result`` and dropped the stop, leaving a
    row nothing executes and nothing cancels -- a ``retry_wait`` row is outside
    the boot reconciler's ``ACTIVE`` set, and the parent's queued count keeps it
    attached. ``only_from`` alone is the guarantee the fence was there
    for: whatever generation the row is at, it is cancelled only while unstarted.
    """
    from kiro_crew import taskq as _taskq

    store = self.taskq_store()
    if store is None:
        return None
    try:
        rec = store.get(agent_id)
        if rec is None or rec.terminal:
            return None
        unstarted = _taskq.CLAIMABLE
        if allow_admitted:
            unstarted = unstarted | frozenset({_taskq.ADMITTED})
            if retained:
                unstarted = unstarted | frozenset({_taskq.STARTING})
        if rec.state not in unstarted:
            return None
        previous = store.cancel(agent_id, reason="user_stop", only_from=unstarted)
    except _taskq.TaskStoreUnavailable:
        _glue_logger.warning("taskq: cancel of %s failed", agent_id, exc_info=True)
        return None
    if previous is None:
        return None
    params = dict(rec.params)
    params["_preassigned_id"] = agent_id
    return params


def taskq_cancel_queued_each(
    self: _TaskqBridgeMixin, agent_ids: Sequence[str], retained_ids: Collection[str] = ()
) -> dict[str, dict[str, Any] | None | Exception]:
    """:meth:`taskq_cancel_queued` for every id, as ONE call: a bulk stop's store phase.

    Each id keeps its own answer: the params of a row that was cancelled,
    None for one that was not, or the exception its cancel raised. Caught per
    id, so one row's error neither cancels nor skips the rest -- the caller
    reports the rows that were cancelled and then raises the error.
    *retained_ids* are the ids held in ``_retained_claims``, snapshotted on
    the loop by :meth:`taskq_post_cancel_queued`.
    """
    outcomes: dict[str, dict[str, Any] | None | Exception] = {}
    for agent_id in agent_ids:
        try:
            outcomes[agent_id] = self.taskq_cancel_queued(
                agent_id, retained=agent_id in retained_ids
            )
        except Exception as exc:  # noqa: BLE001 - handed back per row, raised by the caller
            outcomes[agent_id] = exc
    return outcomes


def taskq_post_cancel_queued(
    self: _TaskqBridgeMixin, agent_ids: Sequence[str]
) -> "dict[str, dict[str, Any] | None | Exception] | _asyncio.Future[Any]":
    """Queue :meth:`taskq_cancel_queued_each` on the writer thread NOW.

    Returns the future of that one job, or the answers themselves when the
    store is called inline -- no running loop, or the pump on the loop --
    which is :meth:`_post_store_write`'s rule, and for its reason: queued
    here, the cancels land before any refill read or claim queued after this
    call returns, so the caller may drop the rows' window entries before it
    awaits the answer. Inline, the pump's own store calls are inline too and
    are not ordered behind a writer-thread job, so the cancels have to have
    landed before this returns. Which ids are retained claims is read here,
    on the caller's thread, and handed to the job.
    """
    store = self.taskq_store()
    if store is None or not agent_ids:
        return {}
    held = self._manager._retained_claims
    retained_ids = frozenset(agent_id for agent_id in agent_ids if agent_id in held)
    try:
        _asyncio.get_running_loop()
    except RuntimeError:
        return self.taskq_cancel_queued_each(agent_ids, retained_ids)
    if not type(self).pump_off_loop:
        return self.taskq_cancel_queued_each(agent_ids, retained_ids)
    return store.post(self.taskq_cancel_queued_each, list(agent_ids), retained_ids)
