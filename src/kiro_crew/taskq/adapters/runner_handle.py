"""The handle of a claimed runner row: :class:`Admitted`.

``RunnerAdmission.admit`` and ``claim_only`` hand one out once a row is claimed and its
``starting`` write committed, and the runner drives the row through it: ``running`` /
``progress`` / ``renew``, the terminal ``settle`` (``done`` / ``fail`` / ``cancel``), the
``recovering`` write and its ``reclaim``, and the lane-slot yield pair. Every write is fenced
by the claim's generation. The handle reaches its admission's store, lane and durable
terminal-write retry through ``self.admission``.

``kiro_crew.taskq.adapters.runner`` stays the import path of the handle. This module imports
that facade for type checkers only, and it logs under the facade's logger.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..model import ADMITTED, CANCELLED, DONE, FAILED, RECOVERING, RUNNING, SIDE_EFFECT_UNKNOWN
from ..store import TaskStore, TaskStoreUnavailable

if TYPE_CHECKING:
    from .runner import RunnerAdmission

logger = logging.getLogger("kiro_crew.taskq.adapters.runner")


@dataclass
class Admitted:
    """A claimed row the runner is executing. Every write is generation-fenced."""

    admission: "RunnerAdmission"
    task_id: str
    kind: str
    lane: str
    generation: int
    session_key: str = ""
    side_effect_class: str = SIDE_EFFECT_UNKNOWN
    safe_retry: bool = False
    _slot_held: bool = field(default=True, repr=False)
    _settled: bool = field(default=False, repr=False)
    #: The state of the ROW, so a handle never reports one the store did not
    #: take. A claim leaves the row ``admitted``; ``starting`` is what the
    #: constructing call has to WRITE before it hands the handle out.
    _state: str = field(default=ADMITTED, repr=False)

    @property
    def state(self) -> str:
        return self._state

    @property
    def slot_held(self) -> bool:
        return self._slot_held

    def _store(self) -> TaskStore | None:
        return self.admission.store

    def _transition_db(self, state: str, **kw: Any) -> bool:
        """Database phase of a state write. Safe on any thread."""
        store = self._store()
        if store is None:
            return True
        try:
            return store.transition(self.task_id, state, generation=self.generation, **kw)
        except TaskStoreUnavailable:
            logger.debug("taskq runner: %s -> %s write failed", self.task_id, state, exc_info=True)
            return False

    def _write(self, state: str, **kw: Any) -> bool:
        """``self._state`` follows the ROW, so it is published from the write's
        result and never ahead of it."""
        ok = self._transition_db(state, **kw)
        if ok:
            self._state = state
        return ok

    async def write_async(self, state: str, **kw: Any) -> bool:
        """:meth:`_write` for an event-loop caller: the transition on the store's
        writer thread, the in-memory publish back on the loop from its result."""
        store = self._store()
        if store is None:
            self._state = state
            return True
        ok = bool(await store.run(self._transition_db, state, **kw))
        if ok:
            self._state = state
        return ok

    def running(self, progress: dict[str, Any] | None = None) -> bool:
        """``starting -> running`` (also the re-entry after a recovery claim)."""
        if self._state == RUNNING:
            return True
        return self._write(RUNNING, progress=progress)

    async def running_async(self, progress: dict[str, Any] | None = None) -> bool:
        """:meth:`running` for an event-loop caller."""
        if self._state == RUNNING:
            return True
        return await self.write_async(RUNNING, progress=progress)

    def progress(self, marker: dict[str, Any]) -> bool:
        store = self._store()
        if store is None:
            return True
        try:
            return store.record_progress(self.task_id, self.generation, marker)
        except TaskStoreUnavailable:
            return False

    def renew(self) -> bool:
        """Extend the lease; False means another incarnation owns the row now."""
        store = self._store()
        if store is None:
            return True
        try:
            return store.renew_lease(self.task_id, self.generation)
        except TaskStoreUnavailable:
            return False

    def release_slot(self) -> None:
        """Give the lane slot back (idempotent). The row keeps its state."""
        if self._slot_held:
            self._slot_held = False
            self.admission.lane.release(self.task_id)

    async def reacquire_slot(self) -> None:
        """Take the slot back after a yield.

        No ``ancestors``, unlike :meth:`RunnerAdmission.admit`: this row already
        RAN, so refusing it here would fail work in flight, where refusing an
        admission only declines to start it.
        """
        if not self._slot_held:
            await self.admission.lane.acquire(self.task_id)
            self._slot_held = True

    def settle(
        self, state: str, *, result_ref: str | None = None, error: str | None = None
    ) -> bool:
        """Terminal write FIRST, then slot release and forget. A second call
        is a no-op.

        The handle is marked settled only once the terminal write committed,
        or once the admission's durable retry owns it (the store was
        unavailable: the write is replayed by the admission's next ``admit``,
        and by ``tick`` where one runs, until it commits). A
        write fenced by a newer generation means another owner already ended
        the row -- nothing to retry, the handle just lets go. Returns True
        only for a committed write.
        """
        if self._settled:
            return False
        return self._settle_apply(state, self._finish_db(state, result_ref, error))

    async def settle_async(
        self, state: str, *, result_ref: str | None = None, error: str | None = None
    ) -> bool:
        """:meth:`settle` for an event-loop caller: the terminal write on the
        store's writer thread, the slot release and the forget back on the loop.

        NOT for an ``except CancelledError`` arm -- an ``await`` there can be
        interrupted before the write is submitted, and a dropped terminal write
        leaves the row active for the next boot's reconciler to re-dispatch.
        """
        if self._settled:
            return False
        store = self._store()
        if store is None:
            return self._settle_apply(state, True)
        ok = bool(await store.run(self._finish_db, state, result_ref, error))
        return self._settle_apply(state, ok)

    def _finish_db(self, state: str, result_ref: str | None, error: str | None) -> bool:
        """Database phase of the terminal write. Safe on any thread.

        A store that cannot take it hands the write to the admission's durable
        retry (:meth:`RunnerAdmission.defer_terminal_write` names its replayers)
        rather than losing it. That hand-off writes
        one ``_pending_finish`` item from whichever thread ran this phase; the
        future's completion is what orders it before :meth:`_settle_apply` reads
        the dict back on the loop.
        """
        store = self._store()
        if store is None:
            return True
        try:
            return store.finish(
                self.task_id,
                state,
                generation=self.generation,
                result_ref=result_ref,
                error=error,
            )
        except TaskStoreUnavailable:
            logger.warning(
                "taskq runner: terminal write for %s deferred (store unavailable)",
                self.task_id,
            )
            self.admission.defer_terminal_write(
                self.task_id, self.generation, state, result_ref=result_ref, error=error
            )
            return False

    def _settle_apply(self, state: str, ok: bool) -> bool:
        self._settled = True
        # Captured BEFORE release_slot() clears it: a lane completion counts only
        # for a handle that metered a lane slot. A claim_only container row holds
        # no slot (_slot_held=False from construction) and executes nothing
        # itself -- its steps are what the lane meters -- so it must not credit a
        # completion no slot produced.
        was_lane_metered = self._slot_held
        self.release_slot()
        if ok:
            self._state = state
            if state == DONE and was_lane_metered:
                # The lane's completion signal for the adaptive controller: a
                # grant that reached a committed ``done``. Recorded here, the
                # single settle choke point, so fail/cancel/recovery never count.
                self.admission.lane.note_done()
        if self.task_id not in self.admission._pending_finish:
            self.admission.forget(self.task_id)
        return ok

    def done(self, result_ref: str | None = None) -> bool:
        return self.settle(DONE, result_ref=result_ref)

    async def done_async(self, result_ref: str | None = None) -> bool:
        return await self.settle_async(DONE, result_ref=result_ref)

    def fail(self, error: str) -> bool:
        return self.settle(FAILED, error=error)

    async def fail_async(self, error: str) -> bool:
        return await self.settle_async(FAILED, error=error)

    def cancel(self, reason: str = "") -> bool:
        return self.settle(CANCELLED, error=reason or None)

    async def cancel_async(self, reason: str = "") -> bool:
        return await self.settle_async(CANCELLED, error=reason or None)

    def _recovering_kw(self, reason: str, delay_secs: float) -> dict[str, Any]:
        """The recovery write's arguments; the slot release is the caller's."""
        return {
            "next_run_at": self.admission.now() + max(0.0, float(delay_secs)),
            "detail": {"reason": str(reason)[:200], "delay_secs": round(float(delay_secs), 3)},
        }

    def recovering(self, *, reason: str, delay_secs: float) -> bool:
        """``running -> recovering`` with ``next_run_at = now + delay``; slot released.

        The row becomes claimable again after the delay; :meth:`reclaim` takes
        it back under a NEW generation, so a late callback from the interrupted
        turn is fenced out as ``stale_result``.
        """
        self.release_slot()
        return self._write(RECOVERING, **self._recovering_kw(reason, delay_secs))

    async def recovering_async(self, *, reason: str, delay_secs: float) -> bool:
        """:meth:`recovering` for an event-loop caller: the slot release stays on
        the loop ahead of the write, the transition runs on the writer thread."""
        self.release_slot()
        return await self.write_async(RECOVERING, **self._recovering_kw(reason, delay_secs))

    async def reclaim(self) -> bool:
        """Re-admit a ``recovering`` row: a lane slot, then a fresh claim."""
        handle = await self.admission.admit(self.task_id, kind=self.kind, lane=self.lane)
        self.generation = handle.generation
        self._slot_held = True
        self._state = handle.state
        return True
