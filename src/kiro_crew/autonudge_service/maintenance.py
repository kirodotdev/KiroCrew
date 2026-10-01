"""The per-data-home transaction lock, the maintenance quiesce and the maintenance view.

Service startup, the public ``add()`` / ``update()`` / ``remove()`` transactions and
administrative maintenance all serialize on one lock per event loop and data home
(:func:`_maintenance_lock`); the task that holds it for a mutation is recorded
(:func:`_claim_mutation_lock`) so an unserialized body can refuse to run without it.
A per-loop quiesce signal lets a mutation queued behind a maintenance transaction give
way instead of waiting on a timer that maintenance itself is waiting for.

The two registries these functions keep, ``_MAINTENANCE_LOCKS`` and
``_MUTATION_LOCK_OWNERS``, stay defined in :mod:`kiro_crew.autonudge` and are read
through it, because the gateway boot smoke tracks that module's globals by name.

Its functions that take the service as ``self`` are
:class:`~kiro_crew.autonudge.AutoNudgeService` methods: each is bound on the class by
name and runs against the service's state through ``self``, and a call to any other
service method goes through ``self`` too, so a patch on the instance reaches it. The
plain helpers beside them are imported directly by the owners that use them.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, AsyncIterator

from kiro_crew.autonudge_service.model import NudgeLoop
from kiro_crew.config.loader import data_home

if TYPE_CHECKING:
    from kiro_crew.autonudge import AutoNudgeService


def _maintenance_lock(base_dir: Path) -> asyncio.Lock:
    """Per-event-loop lock serializing store maintenance with service startup."""
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    loop = asyncio.get_running_loop()
    path_key = os.path.normcase(os.path.abspath(str(base_dir)))
    return seams._MAINTENANCE_LOCKS.setdefault((loop, path_key), asyncio.Lock())


def _claim_mutation_lock(lock: asyncio.Lock) -> None:
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    # Explicit checks, not asserts: asserts vanish under ``python -O``.
    owner = asyncio.current_task()
    if owner is None or not lock.locked():
        raise RuntimeError("mutation lock must be held by the caller")
    if lock in seams._MUTATION_LOCK_OWNERS:
        raise RuntimeError("mutation lock already has an owner")
    seams._MUTATION_LOCK_OWNERS[lock] = owner


def _assert_mutation_lock_owned(lock: asyncio.Lock) -> None:
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    if not (lock.locked() and seams._MUTATION_LOCK_OWNERS.get(lock) is asyncio.current_task()):
        raise RuntimeError("mutation lock must be held by the caller")


def _unclaim_mutation_lock(lock: asyncio.Lock) -> None:
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    _assert_mutation_lock_owned(lock)
    del seams._MUTATION_LOCK_OWNERS[lock]


def _release_mutation_lock(lock: asyncio.Lock) -> None:
    _unclaim_mutation_lock(lock)
    lock.release()


async def _cancel_and_drain_tasks(*tasks: asyncio.Task[Any]) -> bool:
    """Cancel child tasks without letting repeated cancellation abort cleanup."""
    for task in tasks:
        task.cancel()
    drain = asyncio.ensure_future(asyncio.gather(*tasks, return_exceptions=True))
    interrupted = False
    while not drain.done():
        try:
            await asyncio.shield(drain)
        except asyncio.CancelledError:
            interrupted = True
    drain.result()
    return interrupted


class _AutoNudgeMaintenanceView:
    """Store operations that are safe inside ``maintenance_service``'s lock."""

    def __init__(self, service: AutoNudgeService) -> None:
        self._service = service
        self._quiescing: set[str] = set()

    def _release(self) -> None:
        for loop_id in self._quiescing:
            self._service._end_maintenance_quiesce(loop_id)
        self._quiescing.clear()

    def list_all(self) -> list[NudgeLoop]:
        return self._service.list_all()

    def get_by_slot(self, slot_key: str) -> NudgeLoop | None:
        return self._service.get_by_slot(slot_key)

    async def deactivate_and_wait(self, loop_id: str, *, stopped_reason: str | None = None) -> bool:
        self._quiescing.add(loop_id)
        quiesced = await self._service._deactivate_and_wait_unserialized(
            loop_id, stopped_reason=stopped_reason
        )
        if quiesced:
            return True
        else:
            self._service._end_maintenance_quiesce(loop_id)
            self._quiescing.discard(loop_id)
            return False

    async def remove(self, loop_id: str) -> None:
        lock = _maintenance_lock(self._service._base_dir)
        await self._service._remove_unserialized(loop_id, mutation_lock=lock)
        self._service._end_maintenance_quiesce(loop_id)
        self._quiescing.discard(loop_id)


@asynccontextmanager
async def maintenance_service(
    cls: type[AutoNudgeService], base_dir: Path | None = None
) -> AsyncIterator["_AutoNudgeMaintenanceView"]:
    """Yield one authoritative store view, serialized with startup and peers."""
    from kiro_crew import autonudge as seams  # read at call time: the facade imports us

    selected_dir = base_dir or data_home()
    lock = _maintenance_lock(selected_dir)
    async with lock:
        _claim_mutation_lock(lock)
        try:
            live = seams._INSTANCE
            if live is not None and live._base_dir == selected_dir:
                view = _AutoNudgeMaintenanceView(live)
                try:
                    yield view
                finally:
                    view._release()
                return
            offline = await cls.load_for_maintenance(base_dir=selected_dir)
            view = _AutoNudgeMaintenanceView(offline)
            try:
                yield view
            finally:
                view._release()
        finally:
            _unclaim_mutation_lock(lock)


def _begin_maintenance_quiesce(self: AutoNudgeService, loop_id: str) -> None:
    self._maintenance_quiescing.add(loop_id)
    self._maintenance_quiesce_events.setdefault(loop_id, asyncio.Event()).set()


def _end_maintenance_quiesce(self: AutoNudgeService, loop_id: str) -> None:
    self._maintenance_quiescing.discard(loop_id)
    self._maintenance_quiesce_events.pop(loop_id, None)


async def _acquire_mutation_lock(self: AutoNudgeService, loop_id: str) -> asyncio.Lock | None:
    """Acquire the store mutex unless cleanup claims this loop first."""
    if loop_id in self._maintenance_quiescing:
        return None
    lock = _maintenance_lock(self._base_dir)
    event = self._maintenance_quiesce_events.setdefault(loop_id, asyncio.Event())
    acquire_task = asyncio.create_task(lock.acquire())
    quiesce_task = asyncio.create_task(event.wait())
    try:
        done, _pending = await asyncio.wait(
            {acquire_task, quiesce_task}, return_when=asyncio.FIRST_COMPLETED
        )
    except BaseException:
        await _cancel_and_drain_tasks(acquire_task, quiesce_task)
        if acquire_task.done() and not acquire_task.cancelled():
            lock.release()
        raise
    if acquire_task in done:
        interrupted = await _cancel_and_drain_tasks(quiesce_task)
        if interrupted:
            lock.release()
            raise asyncio.CancelledError()
        if loop_id not in self._maintenance_quiescing:
            _claim_mutation_lock(lock)
            return lock
        lock.release()
        return None
    interrupted = await _cancel_and_drain_tasks(acquire_task)
    if acquire_task.done() and not acquire_task.cancelled():
        lock.release()
    if interrupted:
        raise asyncio.CancelledError()
    return None


async def deactivate_and_wait(self: AutoNudgeService, loop_id: str) -> bool:
    """Persistently pause a loop and wait for its current timer to quiesce.

    ``update(active=False)`` deliberately does not cancel a timer already
    inside its fire callback because channel turns run inline there.  A
    cleanup caller has a different need: it must know that a dashboard fire
    cannot materialize a slot after the caller takes its snapshot.
    The loop remains durably present and inactive until the caller removes
    it, so a timeout or process exit leaves a restart-visible recovery
    marker instead of losing the orphan's only identity.
    """
    timer_before = self._timers.get(loop_id)
    loop = await self.update(loop_id, active=False)
    if loop is None:
        return False
    # A turn-complete notification can replace the timer while update()
    # waits to acquire and persist the inactive state. Once update returns,
    # active=False prevents any further replacement, so both tasks close
    # the final slot-publication window.
    timer_after = self._timers.get(loop_id)
    current = asyncio.current_task()
    for timer in {timer_before, timer_after}:
        if timer is not None and timer is not current and not timer.done():
            await asyncio.shield(timer)
    return True


async def _deactivate_and_wait_unserialized(
    self: AutoNudgeService, loop_id: str, *, stopped_reason: str | None = None
) -> bool:
    """Quiesce a loop while the caller owns the maintenance transaction."""
    self._begin_maintenance_quiesce(loop_id)
    timer_before = self._timers.get(loop_id)
    inner = asyncio.create_task(
        self._update_unserialized(loop_id, active=False, stopped_reason=stopped_reason)
    )
    try:
        loop = await asyncio.shield(inner)
    except asyncio.CancelledError:
        # maintenance_service() must not release its transaction while the
        # executor-backed write can still commit a stale snapshot.
        while not inner.done():
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError:
                continue
        inner.result()
        raise
    if loop is None:
        return False
    timer_after = self._timers.get(loop_id)
    current = asyncio.current_task()
    for timer in {timer_before, timer_after}:
        if timer is not None and timer is not current and not timer.done():
            await asyncio.shield(timer)
    return True
