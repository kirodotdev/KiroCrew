"""Ending a parent session's sub-agent runs.

Every verb that ends a conversation snapshots the runs the key owns in the same registry
lock hold as its pop, and cancels them after the provider teardown, shielded and bounded
by ``_CHILD_CANCEL_TIMEOUT_SECS``, before the companion runtime is released. The teardown
fence and the owes-child-work reads let a retirement spare a parent whose children still
run or still owe it a report. A new check on what ending a parent must stop or spare goes
here.

Instance fields written: none.

Instance fields read: ``_deps``, ``_owner``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, AbstractSet

if TYPE_CHECKING:
    from kiro_crew.session_lifecycle import SessionLifecycleService, _ChildTeardownHandler


#: Budget for cancelling one parent's sub-agent runs at a parent end. A parent
#: end is on a user-facing path (a closed tab, a switched model), so an
#: unresponsive child must not hold it open; the companion-runtime release that
#: follows is the backstop for whatever the cancel does not reach in time.
_CHILD_CANCEL_TIMEOUT_SECS = 20.0


def set_child_teardown_handler(
    self: SessionLifecycleService, handler: _ChildTeardownHandler | None
) -> None:
    """Register the two-half sub-agent teardown hook used at every parent end."""
    if self._child_teardown is not None and handler is not None:
        self._deps.logger.warning(
            "Child teardown handler already registered; replacing existing handler"
        )
    self._child_teardown = handler


async def end_children_for(self: SessionLifecycleService, key: str) -> None:
    """End *key*'s sub-agent runs without touching its process.

    The conversation-ended half of a parent end, on its own. Every other verb here
    reaches it as part of tearing a session down, but a caller can replace the
    conversation on a LIVE process -- the workflow pool does exactly that when it hands
    a warm worker to the next task through ``provider.new_conversation()``. The process
    survives and the conversation does not, so the children of the conversation that
    ended have nowhere to report: without this they inject into whatever the reused
    worker is doing next.

    Same two-phase shape as the teardown verbs, for the same reason: the snapshot is
    taken under the registry lock so it cannot name the runs of a conversation that
    starts after it, and the cancel runs outside the lock because it awaits.

    No provider shutdown, no map mutation, no runtime release: this verb makes exactly
    one claim, that the conversation is over. In particular it does NOT advance the
    key's ownership generation -- that counter belongs to session allocation, the
    process here leaves the session in place, and advancing it from a verb that
    retires nothing would report a replacement to every reader of it.
    """
    owner = self._owner
    async with owner._lock:
        teardown_children = self._snapshot_parent_children(key)
    # Names ITSELF in the audit, exactly as the six teardown verbs do, rather than
    # forwarding a verb a caller supplies. One consumer passed one value, so the
    # parameter carried no information the method name does not -- and it bought the
    # verb-naming ratchet an exemption, which is machinery in place of a rule. The
    # caller's own identity is not lost: the line's ``key`` carries it.
    await self._cancel_parent_children(key, teardown_children, verb="end_children_for")


def _open_teardown_fence(self: SessionLifecycleService, key: str) -> "AbstractSet[str] | None":
    """Open the teardown fence for *key* while the registry lock is held.

    Arms the admission path to record every child it accepts for this parent
    from here on (``note_teardown_store_accept``), so the async retire-vs-defer
    decision below sees a child that arrives between this point and its
    off-loop store read instead of racing it. Takes no in-memory snapshot, so a
    parent that is then spared keeps its children's completions.

    Returns the fence handle so the async decision can release exactly this
    fence if it defers, never a newer one a concurrent teardown opened.
    """
    handler = self._child_teardown
    if handler is None or not key:
        return None
    return handler.open_teardown_fence(key)


async def _parent_has_sparable_child(self: SessionLifecycleService, key: str) -> bool:
    """Whether *key* still owns a child the retirement must spare, read at the
    async teardown under the fence opened by :meth:`_open_teardown_fence`.

    True when a child is live or queued in memory, held only by the task store
    (a durable-only or window-held row), OR admitted into the open fence since
    it opened -- i.e. any child the retirement teardown would otherwise cancel.
    A store read that FAILS raises, and the caller defers (fail-safe): a parent
    with an accepted child read as childless would be retired and its child
    cancelled.
    """
    handler = self._child_teardown
    if handler is None or not key:
        return False
    if handler.has_live_or_queued_children(key):
        return True
    if handler.has_fenced_children(key):
        return True
    # Off-loop (writer thread): pre-existing durable rows the fence did not
    # record because they were accepted before it opened. Raises on failure.
    return await handler.has_pending_store_children(key)


async def _parent_owes_child_work(self: SessionLifecycleService, key: str) -> bool:
    """Whether retiring *key* would drop sub-agent work it is still owed, read
    off the loop under the fence opened by :meth:`_open_teardown_fence`.

    Three readings, each covering a span the others miss: a registered report
    still delivering after its run's task ended (``has_unfinished_report_for``),
    the manager's pending-work signal (``has_pending_work_for_async``: a
    finished child whose report is not registered yet, a live follow-up
    watcher, a queued row), and :meth:`_parent_has_sparable_child` (a child the
    teardown would cancel). The pending-work signal stops seeing a run once its
    task ends, which is why the report reading is asked separately. A failed
    read raises, and the caller keeps the parent.
    """
    handler = self._child_teardown
    if handler is None or not key:
        return False
    return (
        handler.has_unfinished_report_for(key)
        or await handler.has_pending_work_for_async(key)
        or await self._parent_has_sparable_child(key)
    )


def _parent_owes_child_work_now(self: SessionLifecycleService, key: str) -> bool:
    """The in-memory half of :meth:`_parent_owes_child_work`, asked again at the
    commit under the registry lock.

    No await, so the answer still holds when the commit pops the session: work
    that appeared while the off-loop read was suspended keeps the parent. The
    store half is the fence's to catch, through ``commit_identity_sweep_retire``.
    """
    handler = self._child_teardown
    if handler is None or not key:
        return False
    return (
        handler.has_live_or_queued_children(key)
        or handler.has_in_memory_pending_work_for(key)
        or handler.has_unfinished_report_for(key)
    )


def _snapshot_parent_children(self: SessionLifecycleService, key: str) -> tuple[str, ...]:
    """The runs *key* owns, read synchronously so the answer cannot drift.

    Called while this module still holds ``owner._lock``, in the same block as
    the pop. That is the only place the answer is certain: every await after it
    is a window in which a cold start can register a successor under the same
    key, and a snapshot taken after one would include the successor's runs.
    Comments throughout this file already treat that window as reachable.
    """
    handler = self._child_teardown
    if handler is None or not key:
        return ()
    try:
        return tuple(handler.snapshot_teardown_children(key))
    except Exception:
        self._deps.logger.exception("Parent end %s: snapshotting sub-agents failed", key)
        return ()


async def _cancel_parent_children(
    self: SessionLifecycleService,
    key: str,
    agent_ids: "Sequence[str]",
    *,
    verb: str,
) -> None:
    """Stop the snapshotted runs, ahead of reaping the runtime they share.

    *key* is passed alongside the snapshot rather than instead of it, so the
    teardown's audit line can name the conversation the ids belonged to.

    Paired with :meth:`SessionManager.release_subagent_runtime` at every site
    that calls it, because that call IS this module's parent-end boundary and
    the two halves of ending a parent belong together.

    The pairing is what makes the rule backend-independent. Releasing the
    companion runtime kills the process a session-sharing child lives ON, so
    a parent end already ends the children of a harness that multiplexes —
    as a side effect of reaping the process, not as a decision. A harness
    that runs one process per child has no entry in ``_subagent_runtimes``,
    so the release reaches nothing and its children outlive the conversation
    that asked for them, each holding its own agent process and that
    process's MCP fleet until its own run timeout expires. Asking the manager
    to cancel makes the same thing happen for every harness, by intent, and
    it happens FIRST so a child is stopped through its own teardown rather
    than by having its runtime pulled out from under a live turn.

    Bounded and best-effort, matching :meth:`_fire_recycle_callback`: a
    parent end must not hang or fail on an unresponsive child, and the
    release below is the backstop for anything the cancel does not reach.

    ``close_all`` is deliberately NOT a caller. Gateway shutdown cancels
    every run at once through ``SubagentManager.cancel_all``, which also
    drains follow-up watchers and announces undelivered messages — work a
    per-key cancel does not do.
    """
    handler = self._child_teardown
    if handler is None or (not agent_ids and not key):
        return

    # The timeout bounds the PARENT'S WAIT, not the reap. A bare ``wait_for`` cancels
    # the coroutine it is waiting on, and this coroutine kills child processes: a
    # write-capable child whose reset runs long would have its ``_force_reap``
    # cancelled part-way, after the marks were written and before the kills landed,
    # leaving it executing tools against a conversation that has ended. So the reap
    # runs as its own task, shielded, and a timeout leaves it running to completion.
    #
    # The task is registered in ``_background_tasks`` for the ordinary reason: a task
    # with no strong reference can be garbage-collected mid-flight, and this one has
    # nothing else holding it once the wait gives up.
    task = asyncio.ensure_future(
        handler.cancel_for_teardown(
            agent_ids,
            parent_session_key=key,
            # Names WHICH verb ended the conversation: the teardown's audit lines carry
            # it, and so does the stop origin of a live run this teardown is first to
            # stop. Several verbs reach this one helper, and "a parent end cancelled
            # these runs" is not answerable from outside without it.
            verb=verb,
        )
    )
    self._owner._background_tasks.add(task)
    task.add_done_callback(self._owner._background_tasks.discard)
    try:
        await asyncio.wait_for(asyncio.shield(task), timeout=_CHILD_CANCEL_TIMEOUT_SECS)
    except asyncio.TimeoutError:
        self._deps.logger.warning(
            "Parent end: cancelling sub-agents %s exceeded %.0fs; it continues in the "
            "background and the runtime release still runs",
            list(agent_ids),
            _CHILD_CANCEL_TIMEOUT_SECS,
        )
    except Exception:
        self._deps.logger.exception("Parent end: cancelling sub-agents %s failed", list(agent_ids))
