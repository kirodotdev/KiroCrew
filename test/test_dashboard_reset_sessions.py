"""Tests for _reset_all_sessions bounded shutdown behavior.

Exercises the ⚡ Apply & Restart path in ``_reset_all_sessions``: healthy
providers shutdown cleanly, hung providers are force-killed after the
timeout budget, and WebSocket ``sessions_restarting`` events fire in the
correct order.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from aiohttp import web

from kiro_crew.dashboard.handlers import _reset_all_sessions


class _FakeSession:
    """Minimal stand-in for ``_Session`` — only carries a ``provider``."""

    def __init__(self, provider: object) -> None:
        self.provider = provider


class _FakeSessionManager:
    """Minimal ``SessionManager`` stub for ``_reset_all_sessions``.

    Exposes the internals that the handler pokes at: ``_lock``, ``_sessions``,
    ``count``, ``_pool_started``, and ``start_pool``. ``start_pool`` records
    whether it was called so tests can assert ordering.
    """

    def __init__(self, providers: list[object]) -> None:
        self._lock = asyncio.Lock()
        self._sessions: dict[str, _FakeSession] = {
            f"key-{i}": _FakeSession(p) for i, p in enumerate(providers)
        }
        self._pool_started = True
        self.start_pool_called = False
        self.start_pool_called_at: float | None = None

    @property
    def count(self) -> int:
        return len(self._sessions)

    async def reload_provider_factory(self) -> None:
        pass

    async def drain_all_providers(self) -> list:
        async with self._lock:
            providers = [s.provider for s in self._sessions.values()]
            self._sessions.clear()
            return providers

    async def drain_warm_pool(self) -> list:
        return []

    async def start_pool(self, *, blocking: bool = True) -> None:
        self.start_pool_called = True
        self.start_pool_called_at = asyncio.get_event_loop().time()


def _make_request(sessions: _FakeSessionManager) -> tuple[web.Request, MagicMock]:
    """Build a minimal ``web.Request`` with ``state`` in app context."""
    broadcasts: list[tuple[str, dict]] = []
    state = MagicMock()
    state.sessions = sessions
    state.broadcast_ws = lambda event, payload: broadcasts.append((event, payload))
    state.push_refresh = MagicMock()
    state.push_slots_update = MagicMock()
    state._background_tasks = set()

    request = MagicMock(spec=web.Request)
    request.app = {"state": state}
    state._broadcasts = broadcasts
    return request, state


class TestResetAllSessionsShutdown:
    """Bounded shutdown behavior for the dashboard restart path."""

    @pytest.mark.asyncio
    async def test_awaits_healthy_shutdowns_without_force_kill(self) -> None:
        """When ``shutdown()`` returns cleanly, ``_sync_kill_provider`` is never called."""
        p1 = MagicMock()
        p1.shutdown = MagicMock(return_value=asyncio.sleep(0))  # fast, clean
        p2 = MagicMock()
        p2.shutdown = MagicMock(return_value=asyncio.sleep(0))
        sessions = _FakeSessionManager([p1, p2])
        request, state = _make_request(sessions)

        with patch("kiro_crew.dashboard.handlers._sync_kill_provider") as mock_kill:
            count = await _reset_all_sessions(request)
            # Wait for the background task to finish so we can observe side effects.
            for task in list(state._background_tasks):
                await task

        assert count == 2
        p1.shutdown.assert_called_once()
        p2.shutdown.assert_called_once()
        mock_kill.assert_not_called()
        assert sessions.start_pool_called is True
        # Both lifecycle events should have fired in order.
        events = [e for e, _ in state._broadcasts if e == "sessions_restarting"]
        assert events == ["sessions_restarting", "sessions_restarting"]
        statuses = [p["status"] for e, p in state._broadcasts if e == "sessions_restarting"]
        assert statuses == ["restarting", "ready"]

    @pytest.mark.asyncio
    async def test_force_kills_hung_provider_after_timeout(self, monkeypatch) -> None:
        """When ``shutdown()`` hangs past the budget, ``_sync_kill_provider`` runs."""
        # Shorten timeout so the test doesn't take 5s. The production default
        # is 5.0 but tests only need to verify the timeout->kill path fires.
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._SHUTDOWN_TIMEOUT_SECS", 0.05
        )

        async def _never_returns() -> None:
            await asyncio.sleep(60)

        hung = MagicMock()
        hung.shutdown = MagicMock(side_effect=lambda: _never_returns())
        healthy = MagicMock()
        healthy.shutdown = MagicMock(return_value=asyncio.sleep(0))
        sessions = _FakeSessionManager([hung, healthy])
        request, state = _make_request(sessions)

        with patch("kiro_crew.dashboard.handlers._sync_kill_provider") as mock_kill:
            await _reset_all_sessions(request)
            for task in list(state._background_tasks):
                await task

        # The hung provider should have been force-killed; the healthy one should not.
        mock_kill.assert_called_once_with(hung)
        assert sessions.start_pool_called is True

    @pytest.mark.asyncio
    async def test_force_kill_fallback_exception_is_swallowed(self, monkeypatch) -> None:
        """If ``_sync_kill_provider`` itself raises, ``_safe_shutdown`` must still
        complete so ``asyncio.gather`` doesn't abort other providers' shutdowns
        and ``start_pool`` runs. Regression guard for AutoSDE comment on CR-268228120.
        """
        monkeypatch.setattr(
            "kiro_crew.dashboard.handlers._SHUTDOWN_TIMEOUT_SECS", 0.05
        )

        async def _never_returns() -> None:
            await asyncio.sleep(60)

        hung = MagicMock()
        hung.shutdown = MagicMock(side_effect=lambda: _never_returns())
        sessions = _FakeSessionManager([hung])
        request, state = _make_request(sessions)

        # Force _sync_kill_provider to raise — simulates PermissionError/OSError
        def _raising_kill(_p: object) -> None:
            raise PermissionError("simulated kill failure")

        with patch(
            "kiro_crew.dashboard.handlers._sync_kill_provider", side_effect=_raising_kill
        ) as mock_kill:
            await _reset_all_sessions(request)
            for task in list(state._background_tasks):
                await task

        # Fallback kill was attempted but raised — must not abort _safe_shutdown.
        mock_kill.assert_called_once_with(hung)
        # start_pool must still have run despite the fallback kill exception.
        assert sessions.start_pool_called is True

    @pytest.mark.asyncio
    async def test_start_pool_runs_after_shutdown_settles(self) -> None:
        """``start_pool`` must only run after all shutdowns complete."""
        completion_order: list[str] = []

        async def _tracked_shutdown(name: str) -> None:
            await asyncio.sleep(0.01)
            completion_order.append(f"shutdown:{name}")

        p1 = MagicMock()
        p1.shutdown = MagicMock(side_effect=lambda: _tracked_shutdown("p1"))
        p2 = MagicMock()
        p2.shutdown = MagicMock(side_effect=lambda: _tracked_shutdown("p2"))
        sessions = _FakeSessionManager([p1, p2])

        # Wrap start_pool to record when it ran.
        real_start_pool = sessions.start_pool

        async def _tracked_start_pool(*, blocking: bool = True) -> None:
            completion_order.append("start_pool")
            await real_start_pool(blocking=blocking)

        sessions.start_pool = _tracked_start_pool  # type: ignore[assignment]

        request, state = _make_request(sessions)
        await _reset_all_sessions(request)
        for task in list(state._background_tasks):
            await task

        # Both shutdowns must complete before start_pool.
        assert completion_order[-1] == "start_pool"
        assert set(completion_order[:-1]) == {"shutdown:p1", "shutdown:p2"}
