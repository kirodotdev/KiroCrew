"""The manager's cancellation: the one intentional-cancel chokepoint, queued-run stops,
cancel recovery, the parent-end teardown wrappers and their fence, and the delegations
into :class:`~kiro_crew.subagent_manager.CancellationCoordinator`.

Each function takes the manager as ``self`` and is bound as the ``SubagentManager``
attribute of the same name in :mod:`kiro_crew.subagent_parts.manager`.

Manager fields read: ``_admission``, ``_cancellation``, ``_published_depths``,
``_shutting_down``.

Manager fields written: none.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kiro_crew.subagent import (
        AbstractSet,
        Any,
        Sequence,
        SubagentInfo,
        SubagentManager,
        asyncio,
        logger,
    )


def _schedule_cancel_recovery(
    self: SubagentManager, info: SubagentInfo, *, reason: str = "unexpected_cancel"
) -> None:
    return self._cancellation._schedule_cancel_recovery_impl(info, reason=reason)


def _cancel_task_intentionally(
    self: SubagentManager,
    task: "asyncio.Task | asyncio.TimerHandle",  # type: ignore[type-arg]
    info: "SubagentInfo | None" = None,
    *,
    reason: str,
) -> None:
    """The single sanctioned chokepoint for INTENTIONALLY cancelling a
    manager-owned subagent task or follow-up watcher.

    Enforces the intentional-cancel contract mechanically instead of by
    docstring: a managed run's terminal marker MUST already be visible
    before the cancel is issued (``info.user_stopped`` / ``info.reaped`` /
    ``info.done`` / ``self._shutting_down``), otherwise ``_run``'s
    CancelledError arm classifies the cancel as unexpected and auto-respawns
    the run — a zombie respawn of work this call site meant to kill. The
    manager-owned follow-up watchers have no run recovery arm; identity
    with their manager field is their marker. A source-scan
    test asserts every raw ``.cancel()`` on these objects routes through here.

    Missing marker → loud error + the recovery budget is consumed
    defensively (``_cancel_retry_used``) so a mis-marked intentional
    cancel can never zombie-respawn; the cancel still proceeds.
    """
    retry_timer = any(
        task is timer
        for timer in (
            getattr(self, "_retained_claim_retry_handle", None),
            *(retry.handle for retry in getattr(self, "_queue_depth_retries", {}).values()),
            getattr(self, "_pressure_recheck_handle", None),
            getattr(self, "_memory_sampler_handle", None),
        )
    )
    followup_watcher = any(
        task is watcher for watcher in getattr(self, "_followup_watchers", {}).values()
    )
    marked = (
        retry_timer
        or followup_watcher
        or self._shutting_down
        or (info is not None and (info.user_stopped or info.reaped or info.done))
    )
    if not marked:
        logger.error(
            "Intentional cancel (reason=%s) issued WITHOUT a terminal "
            "marker — consuming the recovery budget defensively to "
            "prevent a zombie auto-respawn. Fix the call site: set "
            "user_stopped/reaped/done or _shutting_down BEFORE cancelling.",
            reason,
        )
        if info is not None:
            info._cancel_retry_used = True
    task.cancel()


def _unqueue(self: SubagentManager, agent_id: str, **kwargs: Any) -> dict | None:
    return self._cancellation._unqueue_impl(agent_id, **kwargs)


def _report_queued_stop(
    self: SubagentManager,
    params: dict,
    *,
    row_settled: bool = False,
    error: str = "",
    report_owed: bool = False,
) -> "asyncio.Task[bool] | None":
    return self._cancellation._report_queued_stop_impl(
        params, row_settled=row_settled, error=error, report_owed=report_owed
    )


async def cancel(self: SubagentManager, agent_id: str) -> bool:
    return await self._cancellation.cancel_impl(agent_id)


async def cancel_for_parent(self: SubagentManager, parent_session_key: str) -> tuple[int, int]:
    return await self._cancellation.cancel_for_parent_impl(parent_session_key)


def has_live_or_queued_children(self: SubagentManager, parent_session_key: str) -> bool:
    """Whether *parent_session_key* owns a live or queued run, read with no await.

    A side-effect-free query, unlike :meth:`snapshot_teardown_children` which
    arms the delivery gate and tears down follow-ups. Lets a caller decide whether
    to defer a parent's retirement without touching its children's completion path.
    """
    return self._cancellation.has_live_or_queued_children_impl(parent_session_key)


async def has_pending_store_children(self: SubagentManager, parent_session_key: str) -> bool:
    """Whether *parent_session_key* owns a waiting child only the store holds.

    The off-loop companion to :meth:`has_live_or_queued_children`: it reads the
    task store on the writer thread, so the identity-retire deferral can count a
    durable-only or window-held child (which the teardown cancels) without a
    synchronous store read on the event loop. Routed through
    ``taskq_pending_ids_for_async(include_window=True)`` -- the SAME reading the
    teardown's own store sweep uses (window rows kept, admitted rows left out,
    raises when the store is unavailable) -- so the decision and the cancel can
    never disagree about which rows count. A read failure propagates, so the
    caller can defer rather than read an accepted child as absent.
    """
    rows = await self._admission.taskq_pending_ids_for_async(
        parent_session_key, include_window=True
    )
    return bool(rows)


def open_teardown_fence(self: SubagentManager, parent_session_key: str) -> "set[str] | None":
    """Open the teardown fence for *parent_session_key* WITHOUT snapshotting.

    The identity sweep opens the fence while it still holds the registry lock,
    then decides retire-vs-defer at the async teardown. From the moment the
    fence opens, the admission path records every child it accepts for this
    parent into it (``note_teardown_store_accept`` under
    ``_teardown_fence_lock``), so a child admitted between the open and the
    off-loop read is seen by :meth:`has_fenced_children` rather than raced.
    Unlike :meth:`snapshot_teardown_children` this takes NO in-memory snapshot:
    it arms no delivery gate and tears down no follow-ups, so a parent that is
    then spared keeps its children's completion path intact.

    Returns the fence set, so a sweep that DEFERS can release exactly its own
    fence (:meth:`release_teardown_fence`) and never one a concurrent teardown
    opened. ``None`` when there is no store.
    """
    return self._cancellation.open_identity_sweep_fence_inner(parent_session_key)


def has_fenced_children(self: SubagentManager, parent_session_key: str) -> bool:
    """Whether any child was recorded in this parent's OPEN teardown fence.

    Synchronous and cheap (a set read under ``_teardown_fence_lock``): the
    admissions that landed since :meth:`open_teardown_fence`. Combined with
    :meth:`has_live_or_queued_children` (in memory) and
    :meth:`has_pending_store_children` (off-loop, pre-existing rows), it is the
    third source the retire-vs-defer decision reads.
    """
    return self._cancellation.has_fenced_children_impl(parent_session_key)


def release_teardown_fence(
    self: SubagentManager, parent_session_key: str, expected: "AbstractSet[str] | None" = None
) -> None:
    """Discard this parent's teardown fence when the decision was to DEFER.

    A spared parent runs no cancel sweep, so its fence has no consumer; drop it
    so a later real teardown of the same key opens a fresh one. *expected* is
    the fence the sweep opened; the drop happens only when the stored fence IS
    it, so a concurrent ``destroy``'s fence is never dropped out from under its
    own cancel.
    """
    self._cancellation.release_teardown_fence_impl(parent_session_key, expected)


async def cancel_queued(
    self: SubagentManager, agent_id: str, parent_session_key: str, *, stop_origin: str = ""
) -> bool:
    return await self._cancellation.cancel_queued_impl(
        agent_id, parent_session_key, stop_origin=stop_origin
    )


def snapshot_teardown_children(self: SubagentManager, parent_session_key: str) -> tuple[str, ...]:
    """Run ids under *parent_session_key*, taken with no await. Parent-end use.

    Marks them as teardown-cancelled in the same synchronous step. The mark is what
    stops a terminal report from injecting into the retired parent, and a run can
    finish on its own during the provider-teardown awaits that follow — so marking
    later, when the cancel actually runs, is too late for exactly the runs whose
    report is already on its way.

    Also opens the fence that records every row the store accepts for this parent
    from here on, so the cancel can stop the rows accepted before it -- the rows
    held only by the store, which no snapshot can name -- and spare a successor's.
    """
    selected = self._cancellation.snapshot_teardown_children_impl(parent_session_key)
    self._cancellation.note_teardown_snapshot(parent_session_key)
    return selected


def commit_identity_sweep_retire(
    self: SubagentManager, parent_session_key: str, expected: "AbstractSet[str]"
) -> bool:
    """Atomically decide the identity sweep's final commit-vs-defer.

    Returns True (commit) only when *expected* (the sweep's tentative fence)
    recorded no child since it opened; it then becomes this key's COMMITTED
    teardown fence, so the cancel's store sweep inherits any concurrent
    admission. False means a child arrived (or a newer sweep owns the key) and
    the caller must defer and release. Removes the F1 window between the final
    fenced-child check and the committed fence opening.
    """
    return self._cancellation.commit_identity_sweep_retire_inner(parent_session_key, expected)


def snapshot_teardown_children_only(
    self: SubagentManager, parent_session_key: str
) -> tuple[str, ...]:
    """Select the run ids to cancel WITHOUT opening a fresh committed fence.

    The identity sweep's commit path has already promoted its tentative fence
    to the committed one (:meth:`commit_identity_sweep_retire`), so re-opening
    a fresh fence here would discard the promoted set and the concurrent
    admissions it carries. This takes only the selection half.
    """
    return self._cancellation.snapshot_teardown_children_impl(parent_session_key)


async def cancel_for_teardown(
    self: SubagentManager,
    agent_ids: "Sequence[str]",
    *,
    parent_session_key: str,
    verb: str = "",
) -> int:
    """Stop the snapshotted runs without reporting them to a retired parent.

    ``parent_session_key`` is carried so the teardown's one audit line can name the
    conversation whose runs these were; the ids themselves come from the snapshot,
    which is the only reading of them that cannot drift. The store rows accepted
    before that snapshot are stopped after them.
    """
    # The parent is gone, so the depth its slot advertised goes with it; a
    # frame a cancel path publishes after this records the new value again.
    self._published_depths.forget(parent_session_key)
    fence = self._cancellation.take_teardown_snapshot(parent_session_key)
    try:
        return await self._cancellation.cancel_for_teardown_impl(
            agent_ids,
            parent_session_key=parent_session_key,
            verb=verb,
            accepted_since=fence,
        )
    finally:
        # Recording stops only once the sweep's store read is behind it: a row
        # a successor queues during the awaits above must still be spared. A
        # read the store refused keeps it recording for the reaper's retry.
        self._cancellation.release_teardown_snapshot(fence)


async def retry_owed_teardown_sweeps(self: SubagentManager) -> int:
    return await self._cancellation.retry_owed_teardown_sweeps_impl()


async def cancel_all(self: SubagentManager) -> None:
    return await self._cancellation.cancel_all_impl()
