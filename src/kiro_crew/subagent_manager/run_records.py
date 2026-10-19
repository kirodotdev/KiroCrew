"""A run's record off the event loop: ``state.json``, ``result.txt`` and the cleanup identity.

``_write_state_off_loop_impl``, ``_write_finished_result_off_loop_impl`` and
``_start_result_file_impl`` run a run's off-loop ``state.json`` merges, the start of each
attempt's ``result.txt`` and its ending's rewrite in a worker thread (later streamed chunks
are appended on the loop) and drain each through ``_drain_state_writer_impl``, which holds
the run's conversation through ``_hold_for_detached_writer_impl`` for a worker it stops
awaiting. ``_cap_unclaimed_result_impl`` caps ``result.txt`` for every ending but a claim.
``_remember_identity_off_loop`` and ``_await_identity_write`` persist the protected cleanup
identity, and ``_forget_finished_live_state`` drops a settled run's transient payload. New
work on how a run's record reaches the disk goes here.

A function here whose first parameter is ``self`` is a ``RunEventCoordinator`` member: the
class binds it under the same name in ``subagent_manager/run.py``. The ``*_impl`` ones are
rebound onto ``kiro_crew.subagent``'s globals by ``bind_component_globals``, so the names
they read unqualified are that module's, imported here for typing only; the others run on
this module's own globals.

The manager fields this module uses. Writes: ``_abandoned_state_writers``. Reads:
``_conversations``, ``_report_owners``, ``_run_events``, ``_tasks``, ``_teardown_gates``,
``_write_finished_result_off_loop``.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import time

    from ..subagent import (
        _STATE_DRAIN_TIMEOUT,
        Any,
        SubagentInfo,
        logger,
        update_state,
        write_finished_result,
        write_result_chunk,
    )
    from .run import RunEventCoordinator


def _forget_finished_live_state(self: RunEventCoordinator, info: SubagentInfo) -> None:
    """Drop a settled run's transient payload unless it owns retained continuation."""
    from ..subagent_persistence import forget_live_run_state

    manager = self._manager
    teardown = manager._teardown_gates.get(info.id)
    if (
        not info.done
        or info._recovering
        or info.id in manager._tasks
        or info.id in manager._abandoned_state_writers
        or (teardown is not None and not teardown.is_set())
        or any(
            owner.id == info.id and not task.done()
            for task, owner in manager._report_owners.items()
        )
    ):
        return
    if f"subagent:{info.id}" in manager._conversations:
        return
    forget_live_run_state(info.id)


async def _write_state_off_loop_impl(
    self: RunEventCoordinator, info: SubagentInfo, what: str, **fields: object
) -> bool:
    """Merge *fields* into this run's ``state.json``, off the event loop.

    Every ``state.json`` writer inside a run goes off the loop and is
    drained through :meth:`_drain_state_writer_impl`, entering here or, for
    the run's ending, through :meth:`_write_finished_result_off_loop_impl`,
    for two reasons that are both load-bearing.

    OFF-LOOP, because ``update_state`` ends in a synchronous fsync and a
    slow FS must not freeze the gateway/heartbeat. Running in a pool
    thread also means the write TAKES ``update_state``'s per-agent lock,
    which off-loop callers hold and on-loop callers deliberately skip.

    DRAINED ON CANCELLATION, because cancelling a ``to_thread`` await
    detaches the worker without stopping it, and ``update_state`` rewrites
    the WHOLE file from the snapshot it already read — so a detached worker
    rolls back every field written after that read, not merely the fields it
    names. That is how a zombie erases the ``pid`` / ``session_id`` a
    cancel-respawn recovery run writes on the loop, or the retention
    ``keep`` that promote / release write on the loop.
    Cancellation is therefore held open until the worker finishes — but
    BOUNDED: ``cancel_all()`` gathers run tasks with no timeout, so an
    unbounded drain on a wedged FS would hold gateway shutdown forever, and
    this module's convention is that bounded shutdown plus recoverable state
    beats unbounded shutdown (same posture as ``_REPORT_DRAIN_TIMEOUT``).
    ``asyncio.wait`` never cancels its members, so repeated cancels of the
    awaiting task keep the worker future intact while the drain loop waits
    out the same deadline. For the whole window in which the worker may still
    write -- from the cancellation until the worker settles, however the drain
    exits -- the run HOLDS its conversation
    (``SubagentManager._abandoned_state_writers``), so the two on-loop
    ``keep`` writes are deferred past the worker instead of being rolled back
    by it. The window starts at the cancellation rather than at the drain
    deadline because on Python 3.10 a second outer cancel can finalize the run
    mid-drain.

    Returns ``update_state``'s own report: True when the merge was written,
    False when it was SKIPPED because the state was unreadable — a caller
    with a durability contract (the pre-spawn provenance write)
    retries on False. Re-raises ``asyncio.CancelledError`` after draining,
    so such a retry loop ends on cancellation instead of adding a second
    writer for the same fields.
    """
    writer = asyncio.ensure_future(asyncio.to_thread(update_state, info.id, **fields))
    return await self._drain_state_writer_impl(info, what, writer)


async def _write_finished_result_off_loop_impl(
    self: RunEventCoordinator,
    info: SubagentInfo,
    text: str | None,
    *,
    hold_conversation: bool = False,
) -> bool:
    """Leave this run's ``result.txt`` as its ending reads it, and say so.

    ``write_finished_result`` as one worker, drained exactly like a state
    write (it ends in one): *text* is the whole answer of a claimed
    completed ending, ``None`` any other ending. ``update_state`` is handed
    over from this namespace, the seam the run's other state writes go
    through.

    Bounded by ``_STATE_DRAIN_TIMEOUT`` even when nothing cancels it. A
    claimed ending is ``done`` to every stop, so a Stop, a reap or a parent
    end cannot cut this wait short, and ``_run``'s ``finally`` caps after
    the run's cancellation was consumed; unbounded, a wedged FS would hold
    the run, its lane slot and its parent until the run deadline, and
    ``cancel_all()``'s gather forever. Past the bound the worker finishes
    detached, under the same hold a cancelled drain takes, and False is
    returned: the flag lands late or not at all, the safe direction.

    *hold_conversation* takes that hold for the whole write instead, for a
    run that is already ``done``: ``_conversation_busy`` does not count a
    ``done`` run, so a release landing mid-write would write ``keep`` on
    the loop for this worker's whole-file rewrite to roll back.
    """
    writer = asyncio.ensure_future(
        asyncio.to_thread(write_finished_result, info.id, text, update_state)
    )
    if hold_conversation:
        self._hold_for_detached_writer_impl(info, "result complete", writer)
    return await self._drain_state_writer_impl(
        info, "result complete", writer, bound=_STATE_DRAIN_TIMEOUT
    )


async def _start_result_file_impl(self: RunEventCoordinator, info: SubagentInfo, text: str) -> bool:
    """Start this attempt's ``result.txt`` with *text*, off the event loop.

    *text* is everything the attempt has streamed: until a write succeeds
    each chunk retries the fresh write with all of it, so a first write
    the disk refused leaves no hole at the file's head. That write grows
    with the answer, so it runs in a worker, drained like a state write,
    and the chunk loop awaits it, so its rename lands before ``_run``'s
    cap and before a respawn's own first write. The exception is a drain
    that gives up at ``_STATE_DRAIN_TIMEOUT`` on cancellation: the worker
    is left to finish detached and its rename can then land after the
    cap. That drain consumes the one-shot recovery, so no respawn follows
    it. True once the file is started.
    """
    writer = asyncio.ensure_future(asyncio.to_thread(write_result_chunk, info.id, text, fresh=True))
    return await self._drain_state_writer_impl(info, "result start", writer)


async def _drain_state_writer_impl(
    self: RunEventCoordinator,
    info: SubagentInfo,
    what: str,
    writer: "asyncio.Future[Any]",
    *,
    bound: float | None = None,
) -> bool:
    """Await a state-writing worker, drained and bounded on cancellation.

    Called by :meth:`_write_state_off_loop_impl`, whose docstring carries
    the rationale, :meth:`_start_result_file_impl`, and
    :meth:`_write_finished_result_off_loop_impl`, the one caller that
    passes *bound*: a limit on the UNCANCELLED wait too, past which the
    worker is left to finish detached and False is returned. The
    manager's ``_drain_state_writer`` forwards here unchanged.
    """
    try:
        if bound is None:
            return bool(await asyncio.shield(writer))
        # ``asyncio.wait`` never cancels its member, so the worker is
        # intact whether this returns on time, on the bound or by a cancel.
        await asyncio.wait({writer}, timeout=bound)
        if writer.done():
            return bool(writer.result())
        logger.warning(
            "%s write for %s did not finish in %.0fs — leaving it to finish detached",
            what,
            info.id,
            bound,
        )
        self._hold_for_detached_writer_impl(info, what, writer)
        return False
    except asyncio.CancelledError:
        self._hold_for_detached_writer_impl(info, what, writer)
        # Latch for _run's recovery gate: on Python 3.10, wait_for's
        # _cancel_and_wait awaits a bare future that a SECOND outer cancel
        # can interrupt, delivering _run's CancelledError handler while this
        # drain is still in flight — before expiry suppression lands. The
        # latch lets the gate see the live drain and skip scheduling a
        # recovery writer the worker could race. 3.11+ delivers the outer
        # cancel only after this child task completes, so there the latch is
        # always observed False.
        info._state_drain_active = True
        try:
            deadline = time.monotonic() + _STATE_DRAIN_TIMEOUT
            while not writer.done():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "%s write for %s did not drain in %.0fs on cancellation — "
                        "abandoning worker (its stale whole-file rewrite may roll "
                        "back a recovery run's state)",
                        what,
                        info.id,
                        _STATE_DRAIN_TIMEOUT,
                    )
                    # The abandoned worker is a live stale writer whose
                    # rewrite is whole-file, so it can roll back the pid /
                    # session_id a cancel-respawn recovery run writes ON the
                    # loop (no per-agent lock there) — and a lost pid means
                    # an orphan the reaper cannot reach. Consume the
                    # one-shot recovery so this cancellation finalizes
                    # instead of respawning: losing one best-effort
                    # auto-continue on an FS already wedged past the
                    # deadline is strictly cheaper than resurrecting stale
                    # state. Uniform across sites — the blast radius is set
                    # by the whole-file rewrite, not by which fields this
                    # particular caller named. The conversation hold above
                    # outlives this branch and is released by _settled.
                    info._cancel_retry_used = True
                    break
                try:
                    await asyncio.wait({writer}, timeout=remaining)
                except asyncio.CancelledError:
                    pass  # repeated cancel: keep draining to the deadline
        finally:
            info._state_drain_active = False
        raise


def _hold_for_detached_writer_impl(
    self: RunEventCoordinator, info: SubagentInfo, what: str, writer: "asyncio.Future[Any]"
) -> None:
    """Hold this run's conversation until *writer* has landed.

    Taken for a worker no one awaits any more, and up front for the final
    result write of a run that is already ``done``. The hold counts
    writers, not runs: a run can have an earlier worker still detached
    when its final cap starts another, and the hold lasts until the LAST
    of them lands. Registering the same worker twice holds it once.
    """
    # Hold this run's conversation for the WHOLE window in which the
    # worker may still write, which starts here and not at the drain
    # deadline: on Python 3.10 a second outer cancel can deliver _run's
    # finalization mid-drain, so the run can go `done` while the writer
    # is live, and a continuation reaching a released gate would then
    # write `keep` for that writer's stale whole-file rewrite to erase.
    # `keep` is written on the loop and takes no per-agent lock,
    # so ordering is the only thing protecting it. The worker's own
    # done-callback releases the hold, so it lasts exactly as long as the
    # worker does -- milliseconds on a healthy FS. Recorded on the
    # MANAGER, not on `info`: `evict_completed_agents` prunes completed
    # runs out of `_agents`, and an eviction must not release the hold.
    held = self._manager._abandoned_state_writers.setdefault(info.id, set())
    if writer in held:
        return
    held.add(writer)

    def _settled(
        fut: "asyncio.Future[Any]",
        _mgr: Any = self._manager,
        _aid: str = info.id,
        _what: str = what,
    ) -> None:
        # The worker has landed (drained or abandoned). The conversation
        # is safe to promote or release again once no other worker of
        # this run is still writing.
        writers = _mgr._abandoned_state_writers.get(_aid)
        if writers is not None:
            writers.discard(fut)
            if not writers:
                del _mgr._abandoned_state_writers[_aid]
        _mgr._run_events._forget_finished_live_state(info)
        # It may also have raised; retrieve it so it never surfaces as an
        # asynchronous "exception was never retrieved" warning.
        if not fut.cancelled() and fut.exception() is not None:
            logger.debug(
                "Best-effort %s write failed for %s while detached",
                _what,
                _aid,
                exc_info=fut.exception(),
            )

    writer.add_done_callback(_settled)


async def _cap_unclaimed_result_impl(self: RunEventCoordinator, info: SubagentInfo) -> None:
    """Cap ``result.txt`` and record no whole answer, for every ending but a claim.

    Called once, from ``_run``'s ``finally``, so the cap covers the endings
    that never reach ``_run_inner``'s tail as well: the turn-limit and
    escalation bails, the deadline, a cancel, a raised error and a reap.
    Skipped for a claimed ending, which wrote its whole answer, and for a
    cancel-recovery respawn, whose next attempt starts ``result.txt``
    afresh: a cap racing it would write this attempt's bytes over that
    one's. Bounded like every finished-result write, and it never raises,
    so the report below it is still spawned: a cancellation landing here is
    drained first, and ``_run`` re-raises none. The run is ``done`` by now,
    so the write holds its conversation until the worker lands.
    """
    if info._ending_claimed or info._recovering:
        return
    try:
        await self._manager._write_finished_result_off_loop(info, None, hold_conversation=True)
    except asyncio.CancelledError:
        logger.debug("result cap for %s cut short by a cancel", info.id)
    except Exception:
        logger.warning("Failed to record result_complete for %s", info.id, exc_info=True)


async def _remember_identity_off_loop(
    self: RunEventCoordinator,
    info: SubagentInfo,
    *,
    session_id: str,
    provider: str,
    cwd: str = "",
    keep: bool | None = None,
    conversation_key: str = "",
) -> None:
    """Persist protected cleanup identity off-loop and drain cancellation.

    The live generation is already published synchronously, but restart
    durability depends on this protected write. ``to_thread`` workers survive
    cancellation, so shield the worker and delay re-raising until it settles;
    otherwise terminal teardown can finish and restart before the authority
    record exists.
    """
    writer = asyncio.ensure_future(
        asyncio.to_thread(
            self._remember_identity,
            info.id,
            session_id=session_id,
            provider=provider,
            cwd=cwd,
            keep=keep,
            conversation_key=conversation_key,
        )
    )
    await self._await_identity_write(info, writer)


async def _await_identity_write(
    self: RunEventCoordinator, info: SubagentInfo, writer: asyncio.Future[None]
) -> None:
    """Keep a protected writer owned by the run through cancellation."""
    try:
        await asyncio.shield(writer)
    except asyncio.CancelledError:
        info._state_drain_active = True
        try:
            while not writer.done():
                try:
                    await asyncio.wait({writer})
                except asyncio.CancelledError:
                    pass
            if not writer.cancelled():
                try:
                    writer.result()
                except Exception:
                    pass
        finally:
            info._state_drain_active = False
        raise
