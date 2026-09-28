from __future__ import annotations

import pytest

from kiro_crew.dashboard.state import DashboardState


class _FlushTask:
    def __init__(self, events: list[str]) -> None:
        self._events = events

    def cancel(self) -> None:
        self._events.append("flush.cancel")


class _Socket:
    def __init__(self, name: str, events: list[str], *, fail: bool = False) -> None:
        self.name = name
        self._events = events
        self._fail = fail

    async def close(self) -> None:
        self._events.append(f"{self.name}.close")
        if self._fail:
            raise ConnectionResetError(self.name)


@pytest.mark.asyncio
async def test_close_all_ws_cancels_flush_then_closes_and_clears_every_registry() -> None:
    events: list[str] = []
    first = _Socket("first", events, fail=True)
    second = _Socket("second", events)
    state = DashboardState.__new__(DashboardState)
    state._flush_task = _FlushTask(events)  # type: ignore[assignment]
    state._ws_clients = [first, second]  # type: ignore[list-item]
    state._owner_ws_clients = {first}  # type: ignore[assignment]
    state._ws_log_subscribers = {first, second}  # type: ignore[assignment]
    state._ws_subagent_subscribers = {second}  # type: ignore[assignment]

    await state.close_all_ws()

    assert events == ["flush.cancel", "first.close", "second.close"]
    assert state._flush_task is None
    assert state._ws_clients == []
    assert state._owner_ws_clients == set()
    assert state._ws_log_subscribers == set()
    assert state._ws_subagent_subscribers == set()
