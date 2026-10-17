"""The manager's terminal reports: the finalize claim, the terminal report and its
await, the slot release, forced reaps and session kills, the tombstone writer, panel
dismissal settling, the injection-failure notice, the wave and batch digest, and the
delegations into :class:`~kiro_crew.subagent_manager.TerminalCoordinator` and
:class:`~kiro_crew.subagent_manager.WaveDigestCoordinator`.

Each function takes the manager as ``self`` and is bound as the ``SubagentManager``
attribute of the same name in :mod:`kiro_crew.subagent_parts.manager`.

Manager fields read: ``_admission``, ``_agents``, ``_report_owners``, ``_tasks``,
``_terminal``, ``_waves``.

Manager fields written: none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import time

    from kiro_crew.subagent import (
        DISMISSAL_FAILED,
        MAX_ERROR_DETAIL_LEN,
        Any,
        Literal,
        ProcessHandle,
        SubagentDelivery,
        SubagentInfo,
        SubagentManager,
        _redact,
        asyncio,
        logger,
        read_state,
        record_panel_dismissal_outcome,
        tombstone_recovery_action,
        write_tombstone,
    )


def _claim_finalize(
    self: SubagentManager,
    info: SubagentInfo,
    *,
    supersede_recovery: bool = False,
    row_settled: bool = False,
) -> bool:
    claimed = self._terminal._claim_finalize_impl(info, supersede_recovery=supersede_recovery)
    if claimed:
        # The one reporter of the outcome also writes it to the task store,
        # fenced by the generation the run was dispatched under -- unless
        # the reporter's own store call already wrote it (``row_settled``).
        self._admission.taskq_settle(info, row_settled=row_settled)
    return claimed


async def _report_terminal(
    self: SubagentManager,
    info: SubagentInfo,
    *,
    source: str,
    injection_timeout_reason: str,
    mark_delivered_on_success: bool,
    settle_digest: bool = False,
    teardown_done: "asyncio.Event | None" = None,
    gate: "asyncio.Future[bool] | None" = None,
) -> bool:
    return await self._terminal._report_terminal_impl(
        info,
        source=source,
        injection_timeout_reason=injection_timeout_reason,
        mark_delivered_on_success=mark_delivered_on_success,
        settle_digest=settle_digest,
        teardown_done=teardown_done,
        gate=gate,
    )


async def _report_terminal_guarded(
    self: SubagentManager,
    info: SubagentInfo,
    *,
    source: str,
    injection_timeout_reason: str,
    mark_delivered_on_success: bool,
    settle_digest: bool = False,
    teardown_done: "asyncio.Event | None" = None,
) -> bool:
    return await self._terminal._report_terminal_guarded_impl(
        info,
        source=source,
        injection_timeout_reason=injection_timeout_reason,
        mark_delivered_on_success=mark_delivered_on_success,
        settle_digest=settle_digest,
        teardown_done=teardown_done,
    )


async def _run_terminal_report(
    self: SubagentManager,
    info: SubagentInfo,
    *,
    source: str,
    injection_timeout_reason: str,
    mark_delivered_on_success: bool,
    settle_digest: bool = False,
    teardown_done: "asyncio.Event | None" = None,
) -> bool:
    return await self._terminal._run_terminal_report_impl(
        info,
        source=source,
        injection_timeout_reason=injection_timeout_reason,
        mark_delivered_on_success=mark_delivered_on_success,
        settle_digest=settle_digest,
        teardown_done=teardown_done,
    )


def _spawn_terminal_report(
    self: SubagentManager,
    info: SubagentInfo,
    *,
    source: str,
    injection_timeout_reason: str,
    mark_delivered_on_success: bool,
    settle_digest: bool = False,
    teardown_done: "asyncio.Event | None" = None,
    gate: "asyncio.Future[bool] | None" = None,
) -> "asyncio.Task[bool]":
    return self._terminal._spawn_terminal_report_impl(
        info,
        source=source,
        injection_timeout_reason=injection_timeout_reason,
        mark_delivered_on_success=mark_delivered_on_success,
        settle_digest=settle_digest,
        teardown_done=teardown_done,
        gate=gate,
    )


async def _await_report(task: "asyncio.Task[bool]") -> bool:
    """Block until a spawned terminal report completes, shielded.

    On normal completion this blocks until the report is delivered
    (sequencing unchanged). If the awaiting caller is cancelled, the shield
    keeps the report running to completion on its own task while the caller
    still receives ``CancelledError`` — teardown semantics are unchanged and
    the outcome is never stranded.
    """
    return await asyncio.shield(task)


async def settle_before_delete(
    self: SubagentManager,
    agent_id: str,
) -> Literal["delivered", "pending"]:
    """Settle completion debt and remove a finished run atomically."""
    info = self._agents.get(agent_id)
    if info is None:
        return "delivered"
    if info._ending_claimed and not info.done:
        # Claimed its completed ending and still writing its result (a
        # bounded wait): ``cancel()`` declined it as done, but its report
        # does not exist yet, so there is nothing to settle before the pop.
        return "pending"
    active_reports = tuple(
        task
        for task, report_info in self._report_owners.items()
        if report_info is info or report_info.id == agent_id
    )
    if active_reports:
        await asyncio.gather(
            *(asyncio.shield(task) for task in active_reports),
            return_exceptions=True,
        )
    # The pop is only half of a dismissal, and the panel's durable half reads
    # neither the manager nor the folder: it folds the session's crew log. So
    # the dismissal is recorded in BOTH places here, in one coroutine, so the
    # halves cannot come apart -- a route that wrote only one of them would
    # clear the card from live state and leave the fold still offering it.
    #
    # Written BEFORE the pop, because the pop is the PUBLISH. With the write
    # second, an unwritable store still popped the run and still answered
    # "delivered", which the DELETE route reports as success -- and the card
    # came back on the next reconnect with nothing to explain it. A failed
    # write returns the retryable result this coroutine already uses above,
    # leaving the run in the manager so the operator can dismiss it again.
    #
    # The folder write is OFF the loop, because it is a synchronous file write
    # and this is a coroutine; its two falsy cases are NOT the same, since a
    # run with no folder has nothing durable to resurrect its card, so its
    # dismissal stands. The log write enqueues, so it needs no thread; its unit
    # search does read files, so that part goes to a thread.
    #
    # The unit is found by SEARCHING the slot's logs for the child's row, not
    # from the emitter's pin: this method waits for the terminal report, whose
    # ``finally`` releases that pin, so by here every finished child has none.
    # Nor from the slot's current session id, which is the unit the slot is
    # landing work in now and not necessarily the one a child dispatched before
    # a reset went to. A child no unit holds is not a failure -- there is no
    # folded card to clear -- so it does not hold the pop.
    #
    # The log append is WAITED on, on the same terms the dismiss route's other
    # arm uses, because the queue returns as soon as the entry is handed over
    # and the entry can still be refused at the buffer's memory ceiling. A run
    # whose folder is later reclaimed has only this record, so a pop on an
    # uncommitted append is the same published-too-early failure the folder
    # write above is ordered to avoid.
    from kiro_crew.crew_log import emit as crew_log_emit
    from kiro_crew.crew_log.resolve import UnitSearchFailed, unit_holding_child
    from kiro_crew.dashboard.chat_utils import subagent_event_slot

    if crew_log_emit.enabled():
        try:
            unit = await asyncio.to_thread(
                unit_holding_child,
                subagent_event_slot(info.parent_session_key),
                agent_id,
            )
        except UnitSearchFailed:
            # The store would not say whether a unit holds this child, which is
            # not the same as none holding it. Popping here would report a
            # dismissal that was never even looked for, and the card returns
            # once the store recovers.
            logger.debug(
                "crew log: the unit search for %s failed, so the dismissal is "
                "left retryable rather than published",
                agent_id,
                exc_info=True,
            )
            return "pending"
        if unit and not await crew_log_emit.awaiting_commit(
            lambda on_settled: crew_log_emit.on_subagent_dismissed(
                unit, agent_id=agent_id, on_settled=on_settled
            ),
            what=f"the panel dismissal for {agent_id}",
        ):
            return "pending"
    outcome = await asyncio.to_thread(record_panel_dismissal_outcome, agent_id)
    if outcome == DISMISSAL_FAILED:
        return "pending"
    self._agents.pop(agent_id, None)
    self._tasks.pop(agent_id, None)
    return "delivered"


def _release_slot(self: SubagentManager, info: SubagentInfo) -> bool:
    return self._terminal._release_slot_impl(info)


async def _force_reap(
    self: SubagentManager, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
) -> None:
    return await self._terminal._force_reap_impl(agent_id, info, elapsed, reason=reason)


async def _reap_once(
    self: SubagentManager, agent_id: str, info: SubagentInfo, elapsed: float, *, reason: str = ""
) -> None:
    return await self._terminal._reap_once_impl(agent_id, info, elapsed, reason=reason)


async def _sigkill_session(
    self: SubagentManager,
    session_key: str,
    handle: ProcessHandle | None,
    *,
    popped: "list[tuple[Any, ProcessHandle]] | None" = None,
) -> str | None:
    return await self._terminal._sigkill_session_impl(session_key, handle, popped=popped)


async def _sigkill_sessions(
    self: SubagentManager,
    session_key: str,
    handles: list[ProcessHandle],
    *,
    popped: "list[tuple[Any, ProcessHandle]] | None" = None,
) -> str | None:
    return await self._terminal._sigkill_sessions_impl(session_key, handles, popped=popped)


def _sessions_under(self: SubagentManager, session_key: str) -> list[tuple[Any, ProcessHandle]]:
    return self._terminal._sessions_under_impl(session_key)


def _retain_process_handles(
    self: SubagentManager,
    agent_id: str,
    session_key: str,
    pairs: list[tuple[Any, ProcessHandle]] | None = None,
) -> list[ProcessHandle]:
    return self._terminal._retain_process_handles_impl(agent_id, session_key, pairs)


def notify_injection_failed(
    self: SubagentManager, info: SubagentInfo, reason: str = "delivery timed out"
) -> None:
    return self._terminal.notify_injection_failed_impl(info, reason)


def batch_members_pending(self: SubagentManager, batch_id: str) -> bool:
    return self._waves.batch_members_pending_impl(batch_id)


def batch_reports_in_flight(self: SubagentManager, batch_id: str) -> bool:
    return self._waves.batch_reports_in_flight_impl(batch_id)


def arm_report_in_flight(self: SubagentManager, info: SubagentInfo) -> None:
    return self._waves.arm_report_in_flight_impl(info)


def consume_report_hold(self: SubagentManager, batch_id: str, agent_id: str) -> None:
    return self._waves.consume_report_hold_impl(batch_id, agent_id)


async def batch_members_pending_async(self: SubagentManager, batch_id: str) -> bool:
    return await self._waves.batch_members_pending_async_impl(batch_id)


def wave_has_live_nested_spawns(self: SubagentManager, batch_id: str) -> bool:
    return self._waves.wave_has_live_nested_spawns_impl(batch_id)


def finalize_batch(self: SubagentManager, batch_id: str) -> None:
    return self._waves.finalize_batch_impl(batch_id)


def record_lost_submission(
    self: SubagentManager,
    batch_id: str,
    batch_total: int,
    reason: str,
    parent_session_key: str = "",
) -> None:
    return self._waves.record_lost_submission_impl(
        batch_id, batch_total, reason, parent_session_key
    )


def _sweep_stuck_waves(self: SubagentManager, now: float) -> None:
    return self._waves._sweep_stuck_waves_impl(now)


async def _sweep_stuck_waves_async(self: SubagentManager, now: float) -> None:
    return await self._waves._sweep_stuck_waves_async_impl(now)


def _sweep_digest_holds(self: SubagentManager, now: float) -> None:
    return self._waves._sweep_digest_holds_impl(now)


async def _sweep_digest_holds_async(self: SubagentManager, now: float) -> None:
    return await self._waves._sweep_digest_holds_async_impl(now)


def force_digest_flush(
    self: SubagentManager,
    batch_id: str,
    parent_session_key: str,
    batch_total: int,
    held_secs: float,
) -> None:
    return self._waves.force_digest_flush_impl(batch_id, parent_session_key, batch_total, held_secs)


async def _announce_digest_flush(self: SubagentManager, info: SubagentInfo) -> None:
    return await self._waves._announce_digest_flush_impl(info)


async def settle_queued_delivery(self: SubagentManager, deliveries: list[SubagentDelivery]) -> None:
    return await self._waves.settle_queued_delivery_impl(deliveries)


async def _settle_digest_holds(self: SubagentManager, info: SubagentInfo) -> None:
    return await self._waves._settle_digest_holds_impl(info)


def _write_tombstone(info: SubagentInfo, cause: str, *, leaked_process: bool = False) -> None:
    """Best-effort tombstone write for abnormal exits.

    A run that is not persistent gets no tombstone. ``write_tombstone``
    refuses one whose live-run state or recorded mode says so, but a run
    that ended before its folder was seeded -- a declined spawn prompt, a
    stop or a reap while it waited for admission into startup -- has
    neither, so the run's own mode decides here.

    ``leaked_process`` records that the kill left the run's process
    standing. The tombstone still ends the run (orphan reconciliation will
    not report it again); the flag is what the next start reads to end the
    survivor without a second notice (``list_leaked_processes``).
    """
    if info.memory_mode != "persistent":
        return
    # Finalize the run's elapsed time ONCE, here, before any abnormal-exit
    # tombstone is written. Every abnormal arm (timeout, cancel, reap,
    # error) writes the tombstone before ``_run``'s finally / the reaper
    # assigns ``info.elapsed`` (subagent_manager/run.py, terminal.py), so
    # sampling the wall clock inline at write time produced a DIFFERENT
    # value than the terminal ``subagent_done`` event later carried, and a
    # replayed ``spawn_status`` (which reads this tombstone) disagreed with
    # the terminal event. Setting ``info.elapsed`` once and persisting that
    # single field makes both writers read the same value; the later
    # finalizers preserve an already-set value rather than re-sampling.
    if info.elapsed <= 0:
        info.elapsed = time.time() - info.started
    try:

        write_tombstone(
            info.id,
            cause=cause,
            recovery_action=tombstone_recovery_action(info.id, read_state(info.id) or {}),
            pid=info._pid,
            turns=info.turns,
            last_tool=info.last_tool,
            outcome=info.outcome,
            elapsed=info.elapsed,
            credits=info.credits,
            # ``cause`` is a coarse bucket ("error", "timeout"), which is
            # not enough to act on. ``info.error`` is in-memory only and
            # dies with the gateway, so without this the specific reason is
            # recoverable from nothing but the log.
            detail=(_redact(info.error)[:MAX_ERROR_DETAIL_LEN] if info.error else ""),
            **({"leaked_process": True} if leaked_process else {}),
        )
    except Exception:
        logger.debug("Failed to write tombstone for %s", info.id, exc_info=True)
