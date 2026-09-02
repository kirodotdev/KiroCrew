"""Terminal arbitration and task lifetime ownership for subagent runs."""

from __future__ import annotations

import asyncio
from typing import Any, Callable, Coroutine, Generic, Protocol, TypeVar


class LifecycleInfo(Protocol):
    """Mutable lifecycle flags used without importing the manager module."""

    id: str
    done: bool
    reaped: bool
    _reap_started: bool
    _recovering: bool
    _finalized: bool


InfoT = TypeVar("InfoT", bound=LifecycleInfo)


class SubagentLifecycle(Generic[InfoT]):
    """Own one-shot terminal claims, report tasks, and teardown gates."""

    def __init__(self) -> None:
        self._report_tasks: set[asyncio.Task[None]] = set()
        self._report_owners: dict[asyncio.Task[None], InfoT] = {}
        self._teardown_gates: dict[str, asyncio.Event] = {}

    @property
    def report_tasks(self) -> set[asyncio.Task[None]]:
        return self._report_tasks

    @property
    def report_owners(self) -> dict[asyncio.Task[None], InfoT]:
        return self._report_owners

    @property
    def teardown_gates(self) -> dict[str, asyncio.Event]:
        return self._teardown_gates

    @staticmethod
    def begin_reap(info: InfoT) -> None:
        info._reap_started = True

    @staticmethod
    def mark_reaped(info: InfoT) -> None:
        info.reaped = True

    @staticmethod
    def claim_record(info: InfoT) -> bool:
        if info.done:
            return False
        info.done = True
        return True

    @staticmethod
    def claim_report(info: InfoT, *, supersede_recovery: bool = False) -> bool:
        if info._recovering and not supersede_recovery:
            return False
        if info._finalized:
            return False
        if info._recovering:
            info._recovering = False
        info._finalized = True
        return True

    def spawn_report(
        self,
        info: InfoT,
        report_factory: Callable[[], Coroutine[Any, Any, None]],
    ) -> asyncio.Task[None]:
        task: asyncio.Task[None] = asyncio.create_task(report_factory())
        self._report_tasks.add(task)
        self._report_owners[task] = info

        def forget(completed: asyncio.Task[None]) -> None:
            self._report_tasks.discard(completed)
            self._report_owners.pop(completed, None)

        task.add_done_callback(forget)
        return task

    @staticmethod
    async def await_report(task: asyncio.Task[None]) -> None:
        await asyncio.shield(task)

    def pending_reports(self) -> list[asyncio.Task[None]]:
        return [task for task in self._report_tasks if not task.done()]

    def owner_for(self, task: asyncio.Task[None]) -> InfoT | None:
        return self._report_owners.get(task)

    def open_teardown(self, run_id: str) -> asyncio.Event:
        gate = asyncio.Event()
        self._teardown_gates[run_id] = gate
        return gate

    def gate_for(self, run_id: str) -> asyncio.Event | None:
        return self._teardown_gates.get(run_id)

    def close_teardown(self, run_id: str, gate: asyncio.Event) -> None:
        gate.set()
        if self._teardown_gates.get(run_id) is gate:
            self._teardown_gates.pop(run_id, None)
