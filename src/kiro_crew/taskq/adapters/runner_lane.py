"""The runner lane: one FIFO execution gate bounded by the live effective cap.

:class:`RunnerLane` meters both runner entries -- TaskRunner steps and workflow agent
calls -- under one bound the adaptive controller moves, grants slots oldest waiter first,
and refuses a wait that only the asking row's own ancestors could end
(``RunnerLaneSelfBlocked``, in ``runner_vocab``).

``kiro_crew.taskq.adapters.runner`` stays the import path of the lane. This module never
imports that facade, and it logs under the facade's logger.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Any, Callable, Iterable

from .runner_vocab import MODE_AIMD, MODE_FIXED, RunnerLaneSelfBlocked

logger = logging.getLogger("kiro_crew.taskq.adapters.runner")


class RunnerLane:
    """FIFO execution gate whose bound is the LIVE effective cap.

    ``cap`` is the ceiling source -- a callable read on every decision (the
    manager's ``max_concurrent``, which the adaptive controller already
    clamps), or a constant. Reading it live is not a WAKE: a waiter parked
    while the bound was ``0`` holds no slot, so no :meth:`release` of its own
    will ever come. :meth:`pump` is the raise EDGE, and the cap's owner rings
    it (``SubagentManager.set_cap_raise_listener``). ``set_effective_cap`` is a
    lane-LOCAL override that also pumps: ``None`` removes it, ``0`` pauses new
    grants (in-flight work finishes; nothing is cancelled).
    ``mode`` (callable or constant) selects ``fixed``, which pins the bound
    at ``pinned`` (or the ceiling) whatever the controller says -- the one
    config flip that turns the adaptive lane back into a plain semaphore.

    No asyncio primitive is created at import or construction: every waiter
    is a future minted on the loop that called :meth:`acquire`.

    The lane also knows WHICH rows hold its slots (:attr:`holders`), which is
    what lets :meth:`acquire` refuse a self-blocking wait instead of parking on
    it -- see :class:`RunnerLaneSelfBlocked`. A caller that passes no ``task_id``
    holds an UNTRACKED slot, so it can never be named as somebody's blocker; the
    refusal therefore requires every held slot to be accounted for, because an
    untracked holder is one nothing proves is an ancestor.
    """

    def __init__(
        self,
        cap: Callable[[], int] | int = 4,
        *,
        mode: Callable[[], str] | str = MODE_AIMD,
        pinned: int | None = None,
        name: str = "runner-lane",
    ) -> None:
        if callable(cap):
            self._cap: Callable[[], int] = cap
        else:
            fixed_cap = int(cap)
            self._cap = lambda: fixed_cap
        if callable(mode):
            self._mode: Callable[[], str] = mode
        else:
            fixed_mode = str(mode)
            self._mode = lambda: fixed_mode
        self._pinned = None if pinned is None else max(0, int(pinned))
        self._override: int | None = None
        self._running = 0
        self._waiters: deque[tuple[asyncio.Future[None], str, frozenset[str]]] = deque()
        self._name = name
        self._granted = 0
        #: Rows that reached a committed ``done`` terminal write, the lane's
        #: COMPLETIONS. A grant is not one -- ``_granted`` counts admissions,
        #: including the ones that fail, cancel or recover -- so the adaptive
        #: controller earns an increase on this counter, never on grants.
        self._settled_ok = 0
        #: Row id -> slots it holds. Only non-empty ids are tracked, and the map
        #: is cleared whenever the lane empties, so a slot released without its
        #: id leaves no phantom holder behind: a phantom would refuse a
        #: descendant nothing is actually blocking.
        self._holders: dict[str, int] = {}

    # -- bound ---------------------------------------------------------------

    @property
    def ceiling(self) -> int:
        try:
            return max(0, int(self._cap()))
        except Exception:  # noqa: BLE001 - a broken cap source pins at one
            logger.debug("%s: cap source failed; assuming 1", self._name, exc_info=True)
            return 1

    @property
    def mode(self) -> str:
        try:
            value = str(self._mode() or MODE_AIMD)
        except Exception:  # noqa: BLE001 - a broken mode source reads as aimd
            value = MODE_AIMD
        return MODE_FIXED if value == MODE_FIXED else MODE_AIMD

    @property
    def effective(self) -> int:
        """The bound in force now: pinned under ``fixed``, else min(ceiling, override)."""
        ceiling = self.ceiling
        if self.mode == MODE_FIXED:
            return ceiling if self._pinned is None else self._pinned
        if self._override is None:
            return ceiling
        return max(0, min(ceiling, self._override))

    def set_effective_cap(self, cap: int | None) -> int:
        """Lane-local override of the bound; returns the one now in force."""
        self._override = None if cap is None else max(0, int(cap))
        self.pump()
        return self.effective

    # -- occupancy -----------------------------------------------------------

    @property
    def running(self) -> int:
        return self._running

    @property
    def waiting(self) -> int:
        return len(self._waiters)

    @property
    def settled_ok(self) -> int:
        """Cumulative rows that reached a committed ``done`` terminal write."""
        return self._settled_ok

    def note_done(self) -> None:
        """Record one committed ``done`` terminal write. Called by
        :meth:`Admitted._settle_apply`, the single in-memory settle choke point,
        so a fail, cancel or recovery -- none of which pass ``done`` with a
        committed write -- is never counted a completion. A write the store
        could not take and the admission later replays
        (:meth:`RunnerAdmission.flush_pending_finish`) is not counted here: its
        slot is already freed and its window long past, so crediting it late
        would attribute a completion to the wrong window -- the adaptive track
        tolerates the missing evidence rather than mis-timing it."""
        self._settled_ok += 1

    @property
    def holders(self) -> frozenset[str]:
        """Ids of the rows holding a slot right now."""
        return frozenset(self._holders)

    @property
    def saturated(self) -> bool:
        """Whether the next :meth:`acquire` would have to park."""
        return bool(self._waiters) or self._running >= self.effective

    def stats(self) -> dict[str, Any]:
        return {
            "name": self._name,
            "running": self._running,
            "waiting": len(self._waiters),
            "effective": self.effective,
            "ceiling": self.ceiling,
            "mode": self.mode,
            "granted": self._granted,
            "settled_ok": self._settled_ok,
        }

    def _take(self, task_id: str) -> None:
        """Book one slot to *task_id* (untracked when the id is empty)."""
        self._running += 1
        self._granted += 1
        if task_id:
            self._holders[task_id] = self._holders.get(task_id, 0) + 1

    def self_blocking_holders(self, ancestors: Iterable[str]) -> frozenset[str]:
        """The holders of EVERY slot when they are all this row's ancestors, else
        empty.

        Non-empty is the exact statement "no release can come from outside this
        row's own ancestry": the lane is full and each slot belongs to an
        ancestor. One non-ancestor holder answers empty, because that holder
        will free a slot the FIFO queue then hands on. A bound of ``0`` also
        answers empty: that is a deliberate PAUSE which grants nothing to
        anyone and which :meth:`pump` ends, never a block to refuse.

        An UNACCOUNTED slot answers empty too, and that is the fail-safe
        direction: a slot taken with no ``task_id`` is held by nobody this lane
        can name, so it cannot be shown to be an ancestor's, and refusing on a
        set that does not cover every holder would refuse a wait some untracked
        holder is about to end.
        """
        wanted = frozenset(ancestors)
        if not wanted or not self._holders or self.effective <= 0:
            return frozenset()
        if self._running < self.effective:
            return frozenset()
        if sum(self._holders.values()) < self._running:
            return frozenset()
        holders = frozenset(self._holders)
        return holders if holders <= wanted else frozenset()

    def _self_blocked(self, task_id: str, holders: frozenset[str]) -> RunnerLaneSelfBlocked:
        return RunnerLaneSelfBlocked(
            f"{task_id or 'a row'} would wait for a {self._name} slot held only by its own "
            f"ancestor {', '.join(sorted(holders))}; an ancestor yields its slot "
            "(Admitted.release_slot) before its descendant is admitted"
        )

    def pump(self) -> int:
        """Grant slots to the oldest waiters while the bound allows; returns grants.

        Also the point where a waiter that became self-blocking AFTER it parked
        is refused: the bound can fall (an adaptive squeeze) so that a release
        frees nothing, which would leave that waiter on its own ancestry with no
        further edge to wake it.

        Judging only the head is not enough, because a waiter ahead of it can be
        one the same ancestry blocks: an ordinary root queued in front of a
        self-blocked descendant is served only by the ancestor releasing, so
        neither ever wakes and the queue stalls behind a head that is not itself
        refusable. So every parked waiter is judged while the lane is at its
        bound. Refusing one cannot disturb FIFO for the rest -- it leaves the
        queue, it does not overtake -- and the scan runs only on the saturated
        edge.
        """
        granted = 0
        while self._waiters:
            if self._running < self.effective:
                fut, task_id, _anc = self._waiters.popleft()
                if fut.done():
                    continue
                self._take(task_id)
                fut.set_result(None)
                granted += 1
                continue
            refused = False
            for entry in list(self._waiters):
                fut, task_id, ancestors = entry
                if fut.done():
                    self._drop_waiter(entry)
                    refused = True
                    continue
                holders = self.self_blocking_holders(ancestors)
                if not holders:
                    continue
                self._drop_waiter(entry)
                fut.set_exception(self._self_blocked(task_id, holders))
                refused = True
            if not refused:
                break
        return granted

    def _drop_waiter(self, entry: Any) -> None:
        """Remove *entry* from the FIFO wherever it sits."""
        try:
            self._waiters.remove(entry)
        except ValueError:
            pass

    async def acquire(self, task_id: str = "", *, ancestors: Iterable[str] = ()) -> None:
        """Wait for a slot. FIFO; a cancelled waiter leaves the queue cleanly.

        *ancestors* are the ids this row descends from. A wait that only one of
        them could ever end is REFUSED (:class:`RunnerLaneSelfBlocked`) rather
        than parked. Pass none and the park is unbounded, which is what every
        ROOT entry does: a root has no ancestor to be blocked by, so a
        legitimately long queue wait is never turned into a refusal.
        """
        if not self._waiters and self._running < self.effective:
            self._take(task_id)
            return
        wanted = frozenset(ancestors)
        holders = self.self_blocking_holders(wanted)
        if holders:
            raise self._self_blocked(task_id, holders)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[None] = loop.create_future()
        self._waiters.append((fut, task_id, wanted))
        try:
            await fut
        except asyncio.CancelledError:
            # The task's cancel reaches the future first: a waiter that was
            # still parked sees ``fut.cancelled()`` and holds no slot, so only
            # its queue entry goes (``_running`` is untouched). A future that
            # already holds a grant was resolved before the cancel landed:
            # that slot is handed on. Mirrors ``asyncio.Semaphore.acquire``.
            if fut.cancelled():
                self._waiters = deque(w for w in self._waiters if w[0] is not fut)
            else:
                self.release(task_id)
            raise

    def release(self, task_id: str = "") -> None:
        """Give one slot back. Pass the id the matching :meth:`acquire` used, or
        the lane keeps counting that row as a holder until it empties."""
        if self._running > 0:
            self._running -= 1
        held = self._holders.get(task_id, 0) if task_id else 0
        if held > 1:
            self._holders[task_id] = held - 1
        elif held:
            del self._holders[task_id]
        if self._running == 0:
            self._holders.clear()
        self.pump()
