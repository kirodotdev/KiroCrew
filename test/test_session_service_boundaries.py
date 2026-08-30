"""Characterization tests for SessionManager's extraction boundaries.

These tests deliberately exercise the facade while observing effects owned by
different future services.  They pin ordering and persistence semantics before
the implementation is split across those services.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.config import KiroCrewConfig
from kiro_crew.session import FirstTurnState, SessionManager, _Session


@pytest.fixture
def cfg() -> KiroCrewConfig:
    config = KiroCrewConfig()
    config.session.timeout_secs = 2
    return config


def _provider(*, on_shutdown: Callable[[], None] | None = None) -> MagicMock:
    provider = MagicMock()
    provider.cwd = "C:/workspace"
    provider.is_process_alive = lambda: True
    provider.is_alive = lambda: True
    provider.has_active_turn = lambda: False
    provider.has_unfinished_turn = lambda: False
    provider.context_usage_pct = lambda: 0.0

    async def shutdown() -> None:
        if on_shutdown is not None:
            on_shutdown()

    provider.shutdown = AsyncMock(side_effect=shutdown)
    return provider


@pytest.mark.asyncio
async def test_close_all_keeps_cross_boundary_teardown_order(cfg: KiroCrewConfig) -> None:
    events: list[str] = []
    manager = SessionManager(cfg, provider_factory=lambda **_: _provider())

    async def drain(*, timeout: float | None = None) -> int:
        assert manager._closing is True
        events.append("drain")
        return 0

    manager.drain_active_turns = drain  # type: ignore[method-assign]

    worker_started = asyncio.Event()
    never = asyncio.Event()

    async def worker() -> None:
        worker_started.set()
        try:
            await never.wait()
        except asyncio.CancelledError:
            events.append("owned-task-cancelled")
            raise

    owned_task = asyncio.create_task(worker())
    await worker_started.wait()
    manager._background_tasks.add(owned_task)

    async def kill_bg(*, expected: bool) -> None:
        assert expected is True
        events.append("background-runtime-killed")

    manager._bg_runtime = SimpleNamespace(kill=kill_bg)
    manager._subagent_runtimes["parent"] = SimpleNamespace()

    async def release_runtime(key: str) -> None:
        assert key == "parent"
        events.append("subagent-runtime-released")
        manager._subagent_runtimes.pop(key, None)

    manager.release_subagent_runtime = release_runtime  # type: ignore[method-assign]

    warm = _provider(on_shutdown=lambda: events.append("warm-provider-shutdown"))
    active = _provider(on_shutdown=lambda: events.append("active-provider-shutdown"))
    manager._warm_pool.put_nowait((warm, 1.0))
    manager._sessions["active"] = _Session(
        provider=active,
        first_turn=FirstTurnState.NOTHING_ARMED,
    )

    async def close_map() -> None:
        events.append("session-map-closed")

    manager._session_map.aclose = AsyncMock(side_effect=close_map)  # type: ignore[method-assign]

    await manager.close_all()

    assert events[:5] == [
        "drain",
        "owned-task-cancelled",
        "background-runtime-killed",
        "subagent-runtime-released",
        "session-map-closed",
    ]
    assert set(events[5:]) == {"warm-provider-shutdown", "active-provider-shutdown"}
    assert owned_task.done()
    assert manager._background_tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("operation", "clear_sid_calls", "delete_calls", "release_calls"),
    [
        (lambda manager: manager.reset("missing", clear_conversation=True), 0, 0, 0),
        (lambda manager: manager.remove("missing"), 0, 0, 0),
        (lambda manager: manager.destroy("missing"), 0, 1, 1),
        (lambda manager: manager.discard_conversation("missing"), 1, 0, 1),
    ],
    ids=("reset", "remove", "destroy", "discard"),
)
async def test_missing_key_teardown_persistence_matrix(
    cfg: KiroCrewConfig,
    operation: Callable[[SessionManager], Awaitable[object]],
    clear_sid_calls: int,
    delete_calls: int,
    release_calls: int,
) -> None:
    manager = SessionManager(cfg, provider_factory=lambda **_: _provider())
    manager._session_map.clear_sid = MagicMock()  # type: ignore[method-assign]
    manager._session_map.delete = MagicMock()  # type: ignore[method-assign]
    manager.release_subagent_runtime = AsyncMock()  # type: ignore[method-assign]

    await operation(manager)

    assert manager._session_map.clear_sid.call_count == clear_sid_calls
    assert manager._session_map.delete.call_count == delete_calls
    assert manager.release_subagent_runtime.await_count == release_calls


@pytest.mark.asyncio
async def test_hard_stop_orders_abort_reset_respawn_and_hook(cfg: KiroCrewConfig) -> None:
    events: list[str] = []
    manager = SessionManager(cfg, provider_factory=lambda **_: _provider())
    session = _Session(
        provider=_provider(),
        first_turn=FirstTurnState.NOTHING_ARMED,
    )
    manager._sessions["key"] = session

    manager.clear_queue = MagicMock(side_effect=lambda _key: events.append("queue-cleared"))
    manager._send_abort_for_session = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda *_: events.append("abort-sent")
    )
    manager.reset = AsyncMock(  # type: ignore[method-assign]
        side_effect=lambda _key: events.append("session-reset")
    )

    async def eager_respawn(_key: str) -> None:
        raise AssertionError("the scheduler double must not execute the coroutine")

    manager._eager_respawn = eager_respawn  # type: ignore[method-assign]

    scheduled = MagicMock()

    def create_task(coro):  # type: ignore[no-untyped-def]
        events.append("respawn-scheduled")
        coro.close()
        return scheduled

    async def on_hard() -> None:
        events.append("hard-hook")

    with patch("kiro_crew.session.asyncio.create_task", side_effect=create_task):
        outcome = await manager.stop_turn("key", force=True, on_hard=on_hard)

    assert outcome == "hard"
    assert events == [
        "queue-cleared",
        "abort-sent",
        "session-reset",
        "respawn-scheduled",
        "hard-hook",
    ]
    scheduled.add_done_callback.assert_called_once()
    manager._background_tasks.clear()


@pytest.mark.asyncio
async def test_soft_stop_marks_cancelled_context_before_hook(cfg: KiroCrewConfig) -> None:
    manager = SessionManager(cfg, provider_factory=lambda **_: _provider())
    provider = _provider()
    provider.cancel = AsyncMock(return_value="acked")
    session = _Session(
        provider=provider,
        first_turn=FirstTurnState.NOTHING_ARMED,
    )
    manager._sessions["key"] = session
    manager.reset = AsyncMock()  # type: ignore[method-assign]

    async def on_soft() -> None:
        assert session.prev_turn_cancelled is True

    outcome = await manager.stop_turn("key", on_soft=on_soft)

    assert outcome == "soft"
    manager.reset.assert_not_awaited()


@pytest.mark.asyncio
async def test_identity_retirement_preserves_map_and_drops_transient_state(
    cfg: KiroCrewConfig,
) -> None:
    manager = SessionManager(cfg, provider_factory=lambda **_: _provider())
    provider = _provider()
    manager._sessions["key"] = _Session(
        provider=provider,
        first_turn=FirstTurnState.NOTHING_ARMED,
    )
    manager._compact_cooldown_until["key"] = 10.0
    manager._compact_pending_verdict["key"] = 95.0
    manager._suppress_replay.add("key")
    manager._origin_links["key"] = MagicMock()
    manager._session_map.clear_sid = MagicMock()  # type: ignore[method-assign]
    manager._session_map.delete = MagicMock()  # type: ignore[method-assign]
    manager.release_subagent_runtime = AsyncMock()  # type: ignore[method-assign]
    manager._retire_kiro_warm_pool = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._retire_kiro_subagent_runtimes = AsyncMock(return_value=True)  # type: ignore[method-assign]
    manager._retire_kiro_bg_runtime = AsyncMock(return_value=True)  # type: ignore[method-assign]

    with patch("kiro_crew.session._provider_uses_kiro_identity_store", return_value=True):
        retired, complete = await manager.retire_kiro_identity_sessions()

    assert (retired, complete) == (["key"], True)
    assert "key" not in manager._sessions
    assert "key" not in manager._compact_cooldown_until
    assert "key" in manager._compact_pending_verdict
    assert "key" not in manager._suppress_replay
    assert "key" not in manager._origin_links
    manager._session_map.clear_sid.assert_not_called()
    manager._session_map.delete.assert_not_called()
    provider.shutdown.assert_awaited_once()
