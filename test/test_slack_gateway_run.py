"""The gateway's ``run()`` and the servers it starts.

Covers ``run_gateway``, ``run()``'s boot and shutdown with the run marker, the
dashboard and API-only server wiring, the background-session and dashboard-URL
branch, and the event loop staying responsive during slow startup work.
"""

from __future__ import annotations

import asyncio
import contextlib
import gc
import logging
import threading
import time
import weakref
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from slack_gateway_helpers import _restore_update_check_cache  # noqa: F401
from slack_gateway_helpers import (
    _LOST_RUN_SECS,
    _fake_async_proc,
    _make_orchestrator,
    _mock_context_builder,
    _mock_dashboard_state,
    _mock_sessions,
)

from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.slack.gateway import GatewayOrchestrator
from kiro_crew.testing.wait import await_until, finished

_GATEWAY_LOGGER = "kiro_crew.slack.gateway"

# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_dashboard and _init_mcp_discovery
# ═══════════════════════════════════════════════════════════════════════════


class TestInitDashboard:
    """Dashboard initialization."""

    @pytest.mark.asyncio
    async def test_init_dashboard_creates_state(self):
        orch = _make_orchestrator(test_mode=True)
        orch.sessions = _mock_sessions()
        orch.cron_svc = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.ctx_builder = _mock_context_builder()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.task_runner = MagicMock()
        orch.slack = None
        ds = _mock_dashboard_state()
        runner = MagicMock()
        with patch(
            "kiro_crew.slack.gateway.start_dashboard",
            new_callable=AsyncMock,
            return_value=(runner, ds),
        ) as start:
            await orch._init_dashboard()
        assert orch.dashboard_state is ds
        assert orch._dashboard_runner is runner
        assert start.await_args.kwargs["assume_kiro_ready"] is True

    @pytest.mark.parametrize(
        "servers, expected",
        [
            pytest.param([], "No MCP servers configured", id="test_init_mcp_discovery_logs"),
            pytest.param(
                [SimpleNamespace(name="alpha"), SimpleNamespace(name="beta")],
                "Configured MCP servers: alpha, beta",
                id="named_servers",
            ),
        ],
    )
    def test_init_mcp_discovery_logs(self, caplog, servers, expected):
        orch = _make_orchestrator()
        with (
            patch("kiro_crew.mcp_discovery.list_servers", return_value=servers) as listing,
            caplog.at_level(logging.INFO, logger=_GATEWAY_LOGGER),
        ):
            orch._init_mcp_discovery()
        listing.assert_called_once_with()
        assert expected in [r.getMessage() for r in caplog.records if r.name == _GATEWAY_LOGGER]

    def test_init_mcp_discovery_handles_error(self, caplog):
        orch = _make_orchestrator()
        with (
            patch(
                "kiro_crew.mcp_discovery.list_servers", side_effect=RuntimeError("fail")
            ) as listing,
            caplog.at_level(logging.DEBUG, logger=_GATEWAY_LOGGER),
        ):
            orch._init_mcp_discovery()
        listing.assert_called_once_with()
        records = [r for r in caplog.records if r.name == _GATEWAY_LOGGER]
        failed = [r for r in records if r.getMessage() == "MCP server listing failed"]
        assert len(failed) == 1
        assert isinstance(failed[0].exc_info[1], RuntimeError)


# ═══════════════════════════════════════════════════════════════════════════
# Tests: run_gateway entry point
# ═══════════════════════════════════════════════════════════════════════════


class TestRunGateway:
    """Top-level run_gateway function."""

    @pytest.mark.asyncio
    async def test_run_gateway_creates_orchestrator(self):
        from kiro_crew.slack.gateway import run_gateway

        cfg = KiroCrewConfig()
        with patch.object(cfg, "load_credentials", return_value={}):
            # The aggregate-cgroup-ceiling apply shells out to systemctl —
            # a host-service mutation the rootdir guard refuses; stub it.
            with patch("kiro_crew.slack.gateway.ensure_agents_slice_limits", return_value=True):
                with patch.object(GatewayOrchestrator, "run", autospec=True) as mock_run:
                    await run_gateway(cfg, no_dashboard=True, no_crons=True)
        mock_run.assert_awaited_once()
        orch = mock_run.await_args.args[0]
        assert isinstance(orch, GatewayOrchestrator)
        assert orch._no_dashboard is True
        assert orch._no_crons is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: run method (partial — covers init sequence)
# ═══════════════════════════════════════════════════════════════════════════


class TestRunMethod:
    """Gateway run method."""

    @pytest.mark.asyncio
    async def test_run_raises_on_shutdown(self):
        """run() exits when shutdown_event is set."""
        import kiro_crew

        orch = _make_orchestrator()

        orch._init_services = AsyncMock()
        # _init_services is mocked so vector_memory never gets created — mock
        # the default-on embeddings wiring too (it dereferences vector_memory).
        orch._start_embeddings = AsyncMock()
        # run() spawns _auto_migrate_memory as a fire-and-forget task; with
        # _init_services mocked it would raise AttributeError on
        # vector_memory and surface as an unretrieved-task error at GC time.
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        kiro_crew.shutdown_event.set()
        # loop.add_signal_handler -> set_wakeup_fd fails on xdist worker threads.
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.slack.events.init_socket_mode"):
                    with patch("kiro_crew.slack.interactions.init"):
                        with patch("kiro_crew.slack.events.SeenCache"):
                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                with patch(
                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                    new_callable=AsyncMock,
                                ):
                                    with patch("os._exit"):
                                        with patch(
                                            "kiro_crew.platform_compat.raise_nofile_soft_limit"
                                        ):
                                            # flake-ok: pre-existing unbounded run that the pre-set shutdown event ends, moved verbatim; tracked for a de-flake pass
                                            await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()

        orch._init_services.assert_called_once()
        orch._init_cron.assert_awaited_once()
        orch._shutdown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_run_no_dashboard_uses_api_server(self, tmp_path, monkeypatch):
        """--no-dashboard waits for and then clears its run marker."""
        import kiro_crew
        from kiro_crew.instances import run_marker

        orch = _make_orchestrator(no_dashboard=True)
        orch._dashboard_port = 5476

        monkeypatch.setattr(run_marker, "config_dir", lambda: tmp_path)
        run_marker.write_marker(orch._dashboard_port)
        original_write_marker = run_marker.write_marker
        events = []

        def slow_write_marker(port):
            events.append("write-start")
            # flake-ok: pre-existing sleep inside the slow marker-write stub, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- stub that simulates a slow write, moved verbatim; tracked for a de-flake pass
            time.sleep(0.05)
            original_write_marker(port)
            events.append("write-end")

        original_clear_marker = run_marker.clear_marker

        def recording_clear_marker(port):
            events.append("clear")
            original_clear_marker(port)

        monkeypatch.setattr(run_marker, "write_marker", slow_write_marker)
        monkeypatch.setattr(run_marker, "clear_marker", recording_clear_marker)
        marker = run_marker.marker_path(orch._dashboard_port)
        pid_marker = run_marker.pid_path(orch._dashboard_port)
        assert marker.exists()
        assert pid_marker.exists()

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        # run() spawns _auto_migrate_memory as a fire-and-forget task; with
        # _init_services mocked it would raise AttributeError on
        # vector_memory and surface as an unretrieved-task error at GC time.
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        kiro_crew.shutdown_event.set()
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.slack.events.init_socket_mode"):
                    with patch("kiro_crew.slack.interactions.init"):
                        with patch("kiro_crew.slack.events.SeenCache"):
                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                with patch(
                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                    new_callable=AsyncMock,
                                ):
                                    with patch("os._exit"):
                                        with patch(
                                            "kiro_crew.platform_compat.raise_nofile_soft_limit"
                                        ):
                                            # flake-ok: pre-existing unbounded run that the pre-set shutdown event ends, moved verbatim; tracked for a de-flake pass
                                            await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()

        orch._init_dashboard.assert_not_awaited()
        orch._init_api_server.assert_awaited_once()
        assert events == ["write-start", "write-end", "clear"]
        assert not marker.exists()
        assert not pid_marker.exists()

    @pytest.mark.asyncio
    async def test_run_stalled_marker_write_does_not_block_shutdown(self, tmp_path, monkeypatch):
        """A hung marker write times out; the marker is cleared and _shutdown runs.

        Regression: an unbounded ``await self._marker_write_task`` sat before
        the bounded ``_shutdown()`` call, so a stalled write consumed the
        graceful-shutdown deadline and active slots were SIGKILLed unsaved.
        """
        import kiro_crew
        from kiro_crew.instances import run_marker
        from kiro_crew.slack import gateway as gateway_mod

        orch = _make_orchestrator(no_dashboard=True)
        orch._dashboard_port = 5477

        monkeypatch.setattr(run_marker, "config_dir", lambda: tmp_path)
        run_marker.write_marker(orch._dashboard_port)
        # Shrink the wait budget so the timeout path runs fast in tests.
        monkeypatch.setattr(gateway_mod, "_MARKER_WRITE_WAIT_SECS", 0.05)

        events = []
        stall = threading.Event()
        write_started = threading.Event()
        late_cleared = threading.Event()
        original_write_marker = run_marker.write_marker
        original_clear_marker = run_marker.clear_marker
        original_clear_late = run_marker.clear_late_marker_write

        def stalled_write_marker(port):
            events.append("write-start")
            write_started.set()
            # Held until the finally below releases it, so the shutdown finds the
            # write in flight however long the boot before it takes. No timeout on
            # purpose: any bound here is a wall-clock bound on run()'s boot, which is
            # the race this replaced (the old 5 s stall ended on its own under a
            # slow boot), and the finally always releases it.
            # flake-ok: pre-existing unbounded stall the finally always releases, moved verbatim; tracked for a de-flake pass
            stall.wait()
            original_write_marker(port)  # late write republishes the marker
            events.append("write-end")

        def recording_clear_marker(port):
            # The case is a clear that lands WHILE the write is stalled. On a
            # starved pool the writer can still be queued when the 0.05 s budget
            # expires, so wait for it to start rather than race it.
            if not write_started.wait(_LOST_RUN_SECS):
                events.append("writer-never-started")
            events.append("clear")
            original_clear_marker(port)

        def recording_clear_late(port):
            events.append("late-clear")
            try:
                return original_clear_late(port)
            finally:
                late_cleared.set()

        monkeypatch.setattr(run_marker, "write_marker", stalled_write_marker)
        monkeypatch.setattr(run_marker, "clear_marker", recording_clear_marker)
        monkeypatch.setattr(run_marker, "clear_late_marker_write", recording_clear_late)
        marker = run_marker.marker_path(orch._dashboard_port)
        pid_marker = run_marker.pid_path(orch._dashboard_port)
        assert marker.exists()

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        kiro_crew.shutdown_event.set()
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.slack.events.init_socket_mode"):
                    with patch("kiro_crew.slack.interactions.init"):
                        with patch("kiro_crew.slack.events.SeenCache"):
                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                with patch(
                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                    new_callable=AsyncMock,
                                ):
                                    with patch("os._exit"):
                                        with patch(
                                            "kiro_crew.platform_compat.raise_nofile_soft_limit"
                                        ):
                                            # flake-ok: pre-existing unbounded run that the pre-set shutdown event ends, moved verbatim; tracked for a de-flake pass
                                            await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()
            # Release AND drain the stalled writer inside this finally: if an
            # assertion below failed with the worker still parked, monkeypatch
            # teardown would restore the real config_dir/write_marker and the
            # worker would wake later and write markers OUTSIDE tmp_path.
            stall.set()
            if write_started.is_set():
                # Bounded inside the thread: a cancelled to_thread cannot stop it.
                # flake-ok: pre-existing lost-run ceiling on the parked worker, moved verbatim; tracked for a de-flake pass
                await asyncio.to_thread(late_cleared.wait, _LOST_RUN_SECS)

        # The stalled write did not complete before the bounded wait expired,
        # yet the marker was cleared and graceful shutdown still ran — the
        # timeout kept the deadline intact.
        assert events[:2] == ["write-start", "clear"]
        orch._shutdown.assert_awaited_once()

        # The released writer republished the marker files after the
        # timed-out clear. The writer thread must then self-clear them
        # (same thread, no event-loop callback os._exit could beat),
        # otherwise a stopped gateway leaves stale runtime state behind.
        #
        # The thread's clear is ``clear_late_marker_write``, scoped to the
        # generation: it may run after the listener is free, where a location
        # does not identify its owner, so it removes only this process's own
        # write and only while the pid record still names this process. The
        # shutdown-side ``clear_marker`` runs once and holds the listener.
        assert late_cleared.is_set(), (
            f"the released writer did not self-clear within the {_LOST_RUN_SECS:.0f}s "
            f"lost-run ceiling: {events}"
        )
        assert "write-end" in events
        assert events.index("write-end") > events.index("clear")
        assert events.count("clear") == 1  # the timed-out shutdown clear
        assert events.count("late-clear") == 1  # the writer's own self-clear
        assert events.index("late-clear") > events.index("write-end")
        assert not marker.exists()
        assert not pid_marker.exists()

    @pytest.mark.asyncio
    async def test_run_failed_marker_write_still_clears(self, tmp_path, monkeypatch):
        """A marker write that raises must not skip the shutdown clear."""
        import kiro_crew
        from kiro_crew.instances import run_marker

        orch = _make_orchestrator(no_dashboard=True)
        orch._dashboard_port = 5478

        monkeypatch.setattr(run_marker, "config_dir", lambda: tmp_path)
        run_marker.write_marker(orch._dashboard_port)  # pre-existing marker

        def failing_write_marker(port):
            raise OSError("disk full")

        monkeypatch.setattr(run_marker, "write_marker", failing_write_marker)
        marker = run_marker.marker_path(orch._dashboard_port)
        pid_marker = run_marker.pid_path(orch._dashboard_port)
        assert marker.exists()

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        kiro_crew.shutdown_event.set()
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.slack.events.init_socket_mode"):
                    with patch("kiro_crew.slack.interactions.init"):
                        with patch("kiro_crew.slack.events.SeenCache"):
                            with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                with patch(
                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                    new_callable=AsyncMock,
                                ):
                                    with patch("os._exit"):
                                        with patch(
                                            "kiro_crew.platform_compat.raise_nofile_soft_limit"
                                        ):
                                            # flake-ok: pre-existing unbounded run that the pre-set shutdown event ends, moved verbatim; tracked for a de-flake pass
                                            await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()

        # The failed write must not divert control past the clear: the
        # pre-existing marker files are removed and shutdown completed.
        assert not marker.exists()
        assert not pid_marker.exists()
        orch._shutdown.assert_awaited_once()


class TestRunFreezesBootHeap:
    """run() freezes the boot heap and turns cyclic GC back on once the socket is bound."""

    @staticmethod
    async def _run_and_record_freezes(orch, monkeypatch) -> list[tuple[int, int, bool]]:
        """Drive run() with boot mocked; return (dashboard, api, gc on) per gc.freeze call."""
        import gc

        import kiro_crew

        for name in ("_init_services", "_start_embeddings", "_auto_migrate_memory"):
            setattr(orch, name, AsyncMock())
        for name in ("_init_cron", "_init_heartbeat", "_init_autonudge", "_check_for_updates"):
            setattr(orch, name, AsyncMock())
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._shutdown = AsyncMock()
        freezes: list[tuple[int, int, bool]] = []
        # A spy, not the real freeze: freezing would park this worker's whole heap.
        monkeypatch.setattr(
            gc,
            "freeze",
            lambda: freezes.append(
                (
                    orch._init_dashboard.await_count,
                    orch._init_api_server.await_count,
                    gc.isenabled(),
                )
            ),
        )
        was_enabled = gc.isenabled()
        # What the gateway's console-script entry leaves it in during the boot import.
        gc.disable()
        kiro_crew.shutdown_event.set()
        loop = asyncio.get_running_loop()
        try:
            with (
                patch.object(loop, "add_signal_handler"),
                patch("kiro_crew.slack.events.init_socket_mode"),
                patch("kiro_crew.slack.interactions.init"),
                patch("kiro_crew.slack.events.SeenCache"),
                patch("kiro_crew.session.cleanup_orphaned_sessions"),
                patch("kiro_crew.dashboard.handlers._bg_mcp_probe", new_callable=AsyncMock),
                patch("os._exit"),
                patch("kiro_crew.platform_compat.raise_nofile_soft_limit"),
            ):
                # flake-ok: run() returns because the shutdown event is set before it starts
                await orch.run()
            assert gc.isenabled(), "cyclic GC must be back on once boot is done"
        finally:
            kiro_crew.shutdown_event.clear()
            if was_enabled:
                gc.enable()
        return freezes

    @pytest.mark.asyncio
    async def test_dashboard_boot_freezes_once_after_the_dashboard_binds(self, monkeypatch):
        orch = _make_orchestrator()
        freezes = await self._run_and_record_freezes(orch, monkeypatch)
        # Exactly once, after _init_dashboard was awaited, and before GC is re-enabled.
        assert freezes == [(1, 0, False)]

    @pytest.mark.asyncio
    async def test_api_only_boot_freezes_once_after_the_api_server_binds(self, monkeypatch):
        orch = _make_orchestrator(no_dashboard=True)
        freezes = await self._run_and_record_freezes(orch, monkeypatch)
        assert freezes == [(0, 1, False)]


class _BootStopsHere(BaseException):
    """Ends ``run()`` at the release, so no boot step after it needs a fake."""


class _BootMovedOn(BaseException):
    """Ends ``run()`` at its first step after the release point (``gc.freeze``)."""


def _restore_cfg(*, surface_channel_sessions: bool) -> SimpleNamespace:
    return SimpleNamespace(
        dashboard=SimpleNamespace(
            restore_window_minutes=30,
            restore_sessions=True,
            idle_slot_sweep_days=0,
            surface_channel_sessions=surface_channel_sessions,
        )
    )


class TestOrphanNoticeRelease:
    """``run()`` releases the boot orphan sweep's held notices once the tabs are back.

    The sweep starts with the subagent manager, before the dashboard has restored
    the tabs its notices go to, and a channel conversation's tab comes back only
    with the channel reconciler's first complete pass, after the dashboard start
    has returned. The release follows that pass (at once when channel surfacing
    is off), and runs at once for the API-only server and when the start raises,
    so the owner DM digest still goes out.
    """

    @staticmethod
    def _booting(*, no_dashboard: bool, start_raises: bool = False):
        orch = _make_orchestrator(no_dashboard=no_dashboard)
        events: list[str] = []

        def _release() -> None:
            events.append("release")
            raise _BootStopsHere()

        def _init_subagents() -> None:
            orch.subagent_mgr = SimpleNamespace(release_orphan_notices=_release)
            events.append("subagents")

        async def _start_surface() -> None:
            events.append("surface")
            if start_raises:
                raise RuntimeError("the dashboard port is taken")

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = _init_subagents
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = _start_surface
        orch._init_api_server = _start_surface
        orch._announce_cron_quarantine = MagicMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()
        return orch, events

    @staticmethod
    async def _run_until(orch, stop: type[BaseException]) -> BaseException:
        """Drive ``run()`` until *stop* ends it: the release, or the step after it."""
        import kiro_crew

        # Set like the other boot tests: a run() that never reaches the release
        # leaves at the shutdown wait instead of blocking there.
        kiro_crew.shutdown_event.set()
        loop = asyncio.get_running_loop()
        try:
            with ExitStack() as stack:
                stack.enter_context(patch.object(loop, "add_signal_handler"))
                for target in (
                    "kiro_crew.platform_compat.raise_nofile_soft_limit",
                    "kiro_crew.slack.events.init_socket_mode",
                    "kiro_crew.slack.interactions.init",
                    "kiro_crew.slack.events.SeenCache",
                    "kiro_crew.session.cleanup_orphaned_sessions",
                    "os._exit",
                ):
                    stack.enter_context(patch(target))
                stack.enter_context(
                    patch("kiro_crew.dashboard.handlers._bg_mcp_probe", new_callable=AsyncMock)
                )
                # A spy, not the real freeze: freezing would park this worker's heap.
                stack.enter_context(patch("gc.freeze", side_effect=_BootMovedOn()))
                with pytest.raises(stop) as stopped:
                    await orch.run()
        finally:
            kiro_crew.shutdown_event.clear()
        return stopped.value

    @staticmethod
    def _dashboard_state(tmp_path, monkeypatch):
        """A real ``DashboardState`` whose restores find nothing, and a fresh registry."""
        from kiro_crew import session_surface
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard import server as srv
        from kiro_crew.dashboard.state import DashboardState

        monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
        monkeypatch.setattr(srv.cautious_boot, "pause_before", AsyncMock(return_value=None))
        monkeypatch.setattr(srv.chat, "restore_open_slots_async", AsyncMock(return_value=0))
        monkeypatch.setattr(srv.chat, "restore_recent_sessions_async", AsyncMock(return_value=0))
        monkeypatch.setattr(channel_slots, "RECONCILE_INTERVAL_SECS", 3600)
        monkeypatch.setattr(session_surface, "_dashboard_surfaced", frozenset())
        return DashboardState(
            sessions=MagicMock(count=0), crons=MagicMock(), lessons=MagicMock(), start_time=0.0
        )

    @staticmethod
    async def _stop_dashboard_tasks(state) -> None:
        import contextlib

        task = state._channel_slot_reconciler
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if state._slot_list_rebuild_deadline is not None:
            state._slot_list_rebuild_deadline.cancel()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("no_dashboard", [False, True], ids=["dashboard", "api-only"])
    @pytest.mark.parametrize("start_raises", [False, True], ids=["started", "start-raised"])
    async def test_the_release_follows_the_surface_start(self, no_dashboard, start_raises):
        orch, events = self._booting(no_dashboard=no_dashboard, start_raises=start_raises)

        stopped = await self._run_until(orch, _BootStopsHere)

        assert events == ["subagents", "surface", "release"]
        if start_raises:
            # The release ran while the start's own error was propagating.
            assert isinstance(stopped.__context__, RuntimeError)

    @pytest.mark.asyncio
    async def test_an_api_only_gateway_releases_at_once_beside_its_state(
        self, tmp_path, monkeypatch
    ):
        # ``start_api_server`` builds a DashboardState too, but it restores no
        # tabs, so its ``startup_tabs_rebuilt`` is never set.
        orch, events = self._booting(no_dashboard=True)
        state = self._dashboard_state(tmp_path, monkeypatch)

        async def _start_api_server() -> None:
            orch.dashboard_state = state
            events.append("surface")

        orch._init_api_server = _start_api_server

        await self._run_until(orch, _BootStopsHere)

        assert events == ["subagents", "surface", "release"]
        assert not state.startup_tabs_rebuilt.is_set()

    @pytest.mark.asyncio
    async def test_without_channel_surfacing_the_restores_finish_releases_at_once(
        self, tmp_path, monkeypatch
    ):
        from kiro_crew.dashboard import server as srv

        orch, events = self._booting(no_dashboard=False)
        state = self._dashboard_state(tmp_path, monkeypatch)

        async def _start_dashboard() -> None:
            state.begin_slot_list_rebuild()
            await srv._restore_dashboard_sessions(
                state, _restore_cfg(surface_channel_sessions=False)
            )
            orch.dashboard_state = state
            events.append("surface")

        orch._init_dashboard = _start_dashboard
        try:
            await self._run_until(orch, _BootStopsHere)
        finally:
            await self._stop_dashboard_tasks(state)

        assert events == ["subagents", "surface", "release"]
        assert state._channel_slot_reconciler is None

    @pytest.mark.asyncio
    async def test_a_channel_parents_notice_waits_for_its_rebuilt_tab(
        self, tmp_path, monkeypatch, close_subagent_managers
    ):
        # A Slack conversation was open in a tab at the last shutdown, and its
        # subagent finished there. The real channel reconciler rebuilds the tab
        # after the dashboard start returns; its first pass waits for ``first_pass``.
        from kiro_crew import subagent_persistence as sp
        from kiro_crew.dashboard import channel_slots
        from kiro_crew.dashboard import server as srv
        from kiro_crew.dashboard.chat_utils_slots import dashboard_slot_key
        from kiro_crew.history_projection import METADATA_LINE_CORRUPT
        from kiro_crew.subagent import SubagentManager
        from kiro_crew.testing.wait import await_until

        for var, sub in (("KIROCREW_HOME", "home"), ("KIROCREW_WORKSPACE", "workspace")):
            (tmp_path / sub).mkdir()
            monkeypatch.setenv(var, str(tmp_path / sub))
        sp.create_agent_folder("orp0101", task="summarise the logs", parent_session="slack:1.1")
        assert sp.write_finished_result("orp0101", "the whole answer", sp.update_state)

        state = self._dashboard_state(tmp_path, monkeypatch)
        state.conversation_log = SimpleNamespace(
            list_sessions_status=lambda: (
                [{"key": "slack_1.1", "title": "", "modified": 10**12}],
                True,
            ),
            get_metadata_status=lambda _key: ({}, False),
            metadata_line_state=lambda _key: METADATA_LINE_CORRUPT,
            mtime_of=lambda _key: None,
            read_messages=MagicMock(return_value=[{"role": "user", "content": "hello"}]),
        )
        state.sessions.channel_key_for_stem = lambda stem: (
            "slack:1.1" if stem == "slack_1.1" else ""
        )
        monkeypatch.setattr(channel_slots, "lookup_channel_folder", AsyncMock(return_value=""))
        first_pass = asyncio.Event()
        real_pass = channel_slots._reconcile_pass

        async def _gated_pass(state_, window_minutes):  # type: ignore[no-untyped-def]
            await first_pass.wait()
            return await real_pass(state_, window_minutes)

        monkeypatch.setattr(channel_slots, "_reconcile_pass", _gated_pass)

        owner_dm: list[str] = []

        async def _notify(parent_session, msg, meta=None, delivery=None):  # type: ignore[no-untyped-def]
            # The gateway's own lookup (``_orphan_notify``): the tab showing the parent.
            slot = state.get_slot(dashboard_slot_key(parent_session))
            if slot is None:
                return False
            slot._pending_subagent_failures.append(msg)
            return True

        async def _dm(msg):  # type: ignore[no-untyped-def]
            owner_dm.append(msg)
            return True

        manager = SubagentManager(
            sessions=MagicMock(),
            ctx_builder=MagicMock(),
            on_done=AsyncMock(),
            on_orphan_notify=_notify,
            on_orphan_dm=_dm,
            hold_orphan_notices=True,
        )
        monkeypatch.setattr(manager, "_is_pid_alive", lambda _pid: False)
        monkeypatch.setattr("kiro_crew.subagent.sel", MagicMock())
        orch, _events = self._booting(no_dashboard=False)
        sweeps: list[asyncio.Task[None]] = []

        def _init_subagents() -> None:
            orch.subagent_mgr = manager
            sweeps.append(asyncio.create_task(manager._reconcile_orphans()))

        async def _start_dashboard() -> None:
            state.begin_slot_list_rebuild()
            await srv._restore_dashboard_sessions(
                state, _restore_cfg(surface_channel_sessions=True)
            )
            orch.dashboard_state = state

        orch._init_subagents = _init_subagents
        orch._init_dashboard = _start_dashboard
        try:
            await self._run_until(orch, _BootMovedOn)

            # The boot went on, and the notices are still held: the tab is not back.
            assert (
                not manager._orphan_notice_gate.is_set()
            ), "the notices were released before the channel tab was rebuilt"
            assert state.get_slot("slack_1.1") is None

            first_pass.set()
            await await_until(sweeps[0], what="the sweep the first channel pass released")
        finally:
            await self._stop_dashboard_tasks(state)

        tab = state.get_slot("slack_1.1")
        assert tab is not None and tab.linked_session_key == "slack:1.1"
        assert len(tab._pending_subagent_failures) == 1
        assert "orp0101" in tab._pending_subagent_failures[0]
        assert owner_dm == [], "a notice the rebuilt tab took was also sent to the owner DM"
        assert sp.read_tombstone("orp0101")["recovery_action"] == "delivered"


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_api_server
# ═══════════════════════════════════════════════════════════════════════════


class TestInitApiServer:
    """API-only server initialization."""

    @pytest.mark.asyncio
    async def test_init_api_server(self):
        orch = _make_orchestrator(test_mode=True)
        orch.sessions = _mock_sessions()
        orch.cron_svc = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.task_runner = MagicMock()
        orch.slack = None
        ds = _mock_dashboard_state()
        runner = MagicMock()
        with patch(
            "kiro_crew.dashboard.start_api_server",
            new_callable=AsyncMock,
            return_value=(runner, ds),
        ) as start:
            await orch._init_api_server()
        assert orch.dashboard_state is ds
        assert start.await_args.kwargs["assume_kiro_ready"] is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _init_dashboard wiring
# ═══════════════════════════════════════════════════════════════════════════


class TestInitDashboardWiring:
    """Dashboard wiring with slack and no_crons."""

    @pytest.mark.asyncio
    async def test_dashboard_wires_slack_client(self):
        orch = _make_orchestrator(slack_enabled=True)
        orch.sessions = _mock_sessions()
        orch.cron_svc = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.ctx_builder = _mock_context_builder()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.task_runner = MagicMock()
        mock_slack = MagicMock()
        orch.slack = mock_slack
        ds = _mock_dashboard_state()
        runner = MagicMock()
        with patch(
            "kiro_crew.slack.gateway.start_dashboard",
            new_callable=AsyncMock,
            return_value=(runner, ds),
        ):
            await orch._init_dashboard()
        assert ds.slack_client == mock_slack
        assert ds.no_crons is False

    @pytest.mark.asyncio
    async def test_dashboard_no_crons_flag(self):
        orch = _make_orchestrator(no_crons=True)
        orch.sessions = _mock_sessions()
        orch.cron_svc = MagicMock()
        orch.subagent_mgr = MagicMock()
        orch.ctx_builder = _mock_context_builder()
        orch.conv_log = MagicMock()
        orch.consolidator = MagicMock()
        orch.task_runner = MagicMock()
        orch.slack = None
        ds = _mock_dashboard_state()
        runner = MagicMock()
        with patch(
            "kiro_crew.slack.gateway.start_dashboard",
            new_callable=AsyncMock,
            return_value=(runner, ds),
        ):
            await orch._init_dashboard()
        assert ds.no_crons is True


# ═══════════════════════════════════════════════════════════════════════════
# Tests: run() signal handling and bg session
# ═══════════════════════════════════════════════════════════════════════════


class TestRunSignalAndBgSession:
    """Run method signal handling and background session."""

    @pytest.mark.asyncio
    async def test_run_with_ollama_config(self):
        """run() starts ollama when configured."""
        # no_dashboard=True so the bg-session task short-circuits the dashboard
        # branch (otherwise it races on _local_only/_dashboard_port set by the
        # mocked _init_dashboard).
        orch = _make_orchestrator(no_dashboard=True)
        orch._cfg.memory.embedding_provider = "llama_cpp"

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        # run() spawns _auto_migrate_memory as a fire-and-forget task; with
        # _init_services mocked it would raise AttributeError on
        # vector_memory and surface as an unretrieved-task error at GC time.
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_dashboard = AsyncMock()
        orch._init_autonudge = AsyncMock()
        orch._init_api_server = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        # Use a fresh asyncio.Event bound to this test's loop. The shared
        # _LazyShutdownEvent can be polluted by prior tests in full-file runs.
        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        try:
            with patch.object(loop, "add_signal_handler"):
                with patch("kiro_crew.shutdown_event", fresh_event):
                    with patch("kiro_crew.slack.gateway.shutdown_event", fresh_event):
                        with patch("kiro_crew.slack.events.init_socket_mode"):
                            with patch("kiro_crew.slack.interactions.init"):
                                with patch("kiro_crew.slack.events.SeenCache"):
                                    with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                        with patch(
                                            "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                            new_callable=AsyncMock,
                                        ):
                                            with patch("os._exit"):
                                                with patch(
                                                    "kiro_crew.platform_compat.raise_nofile_soft_limit"
                                                ):
                                                    # flake-ok: pre-existing unbounded run that the pre-set shutdown event ends, moved verbatim; tracked for a de-flake pass
                                                    await orch.run()
        finally:
            pass

        orch._start_embeddings.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════════════
# Tests: _check_missing_deps pip install path
# ═══════════════════════════════════════════════════════════════════════════


class TestBgSessionDashboardBranch:
    """run() -> dashboard URL announcement and the probe-gated session warm."""

    @pytest.mark.asyncio
    async def test_bg_session_prints_dashboard_url(self):
        """_start_bg_session still warms the session pool behind the probe."""
        orch = _make_orchestrator(no_dashboard=False, no_open=True)

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        # run() spawns _auto_migrate_memory as a fire-and-forget task; with
        # _init_services mocked it would raise AttributeError on
        # vector_memory and surface as an unretrieved-task error at GC time.
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        # Real-ish sessions stub so _start_bg_session passes the assert
        orch.sessions = MagicMock()
        orch.sessions.start_pool = AsyncMock()

        # Stub _init_dashboard to set the attributes _start_bg_session reads
        async def _init_dash():
            orch._local_only = True
            orch._configured_host = None
            orch._dashboard_port = 6779

        orch._init_dashboard = _init_dash

        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler"):
            with patch("kiro_crew.shutdown_event", fresh_event):
                with patch("kiro_crew.slack.gateway.shutdown_event", fresh_event):
                    with patch(
                        "kiro_crew.slack.gateway.resolve_dashboard_host", return_value="127.0.0.1"
                    ):
                        with patch(
                            "kiro_crew.slack.gateway.build_dashboard_url",
                            return_value="http://127.0.0.1:6779/?t=tok",
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.format_dashboard_urls",
                                return_value=["url-line-1", "url-line-2"],
                            ):
                                with patch("kiro_crew.slack.events.init_socket_mode"):
                                    with patch("kiro_crew.slack.interactions.init"):
                                        with patch("kiro_crew.slack.events.SeenCache"):
                                            with patch(
                                                "kiro_crew.session.cleanup_orphaned_sessions"
                                            ):
                                                with patch(
                                                    "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                                    new_callable=AsyncMock,
                                                ):
                                                    with patch("os._exit"):
                                                        with patch(
                                                            "kiro_crew.platform_compat.raise_nofile_soft_limit"
                                                        ):
                                                            # flake-ok: pre-existing unbounded run that the pre-set shutdown event ends, moved verbatim; tracked for a de-flake pass
                                                            await orch.run()
                                                            # Let bg_session task drain
                                                            # flake-ok: pre-existing pair of event-loop yields before the URL check, moved verbatim; tracked for a de-flake pass
                                                            await asyncio.sleep(0)
                                                            # flake-ok: pre-existing event-loop yield, the second of the pair, moved verbatim; tracked for a de-flake pass
                                                            await asyncio.sleep(0)

        orch.sessions.start_pool.assert_awaited_once_with(blocking=False)

    @pytest.mark.asyncio
    async def test_dashboard_url_is_printed_before_the_mcp_probe_is_awaited(self):
        """The URL must not wait on the MCP probe.

        The port is bound before either happens, and nothing about formatting a
        URL depends on MCP state — only session spawn does (kiro-cli reads
        mcp.json at spawn time). Printing after the probe cost the operator up
        to mcp_probe_timeout_secs+15 of blank screen, and all of it whenever the
        probe timed out.

        Asserted as an ORDER, not a call count, because the defect this pins is
        purely positional: both the print and the probe happened either way.
        """
        orch = _make_orchestrator(no_dashboard=False, no_open=True)

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        orch.sessions = MagicMock()
        orch.sessions.start_pool = AsyncMock()

        async def _init_dash():
            orch._local_only = True
            orch._configured_host = None
            orch._dashboard_port = 6779

        orch._init_dashboard = _init_dash

        # One ordered trace of both events. The URL lines are a distinctive
        # sentinel so ordinary boot chatter cannot be mistaken for them.
        trace: list[str] = []
        real_print = print

        def _tracing_print(*args, **kwargs):
            if args and isinstance(args[0], str) and args[0].startswith("url-line"):
                trace.append("url")
            real_print(*args, **kwargs)

        async def _tracing_probe(_sessions=None):
            trace.append("probe")

        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler"):
            with patch("kiro_crew.shutdown_event", fresh_event):
                with patch("kiro_crew.slack.gateway.shutdown_event", fresh_event):
                    with patch(
                        "kiro_crew.slack.gateway.resolve_dashboard_host", return_value="127.0.0.1"
                    ):
                        with patch(
                            "kiro_crew.slack.gateway.build_dashboard_url",
                            return_value="http://127.0.0.1:6779/?t=tok",
                        ):
                            with patch(
                                "kiro_crew.slack.gateway.format_dashboard_urls",
                                return_value=["url-line-1", "url-line-2"],
                            ):
                                with patch("builtins.print", _tracing_print):
                                    with patch("kiro_crew.slack.events.init_socket_mode"):
                                        with patch("kiro_crew.slack.interactions.init"):
                                            with patch("kiro_crew.slack.events.SeenCache"):
                                                with patch(
                                                    "kiro_crew.session.cleanup_orphaned_sessions"
                                                ):
                                                    with patch(
                                                        "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                                        _tracing_probe,
                                                    ):
                                                        with patch("os._exit"):
                                                            with patch(
                                                                "kiro_crew.platform_compat.raise_nofile_soft_limit"
                                                            ):
                                                                # flake-ok: pre-existing unbounded run that the pre-set shutdown event ends, moved verbatim; tracked for a de-flake pass
                                                                await orch.run()
                                                                # flake-ok: pre-existing pair of event-loop yields before the URL check, moved verbatim; tracked for a de-flake pass
                                                                await asyncio.sleep(0)
                                                                # flake-ok: pre-existing event-loop yield, the second of the pair, moved verbatim; tracked for a de-flake pass
                                                                await asyncio.sleep(0)

        assert "url" in trace, f"dashboard URL was never printed; trace={trace}"
        assert "probe" in trace, f"MCP probe was never awaited; trace={trace}"
        assert trace.index("url") < trace.index("probe"), (
            "dashboard URL was printed only AFTER the MCP probe was awaited — "
            f"the boot-delay regression is back; trace={trace}"
        )

    @pytest.mark.asyncio
    async def test_failing_url_announcement_does_not_abort_boot(self):
        """Announcing the URL is best effort — it must not take the gateway down.

        This block runs on the synchronous boot path, ahead of the MCP probe,
        so a raise here reaches the boot path. The fault isolation has to be
        explicit or a formatting/token failure becomes a failed boot of an
        already-listening dashboard.
        """
        orch = _make_orchestrator(no_dashboard=False, no_open=True)

        orch._init_services = AsyncMock()
        orch._start_embeddings = AsyncMock()
        orch._auto_migrate_memory = AsyncMock()
        orch._init_cron = AsyncMock()
        orch._init_heartbeat = AsyncMock()
        orch._init_mcp_discovery = MagicMock()
        orch._init_subagents = MagicMock()
        orch._init_task_runner = MagicMock()
        orch._init_autonudge = AsyncMock()
        orch._check_for_updates = AsyncMock()
        orch._shutdown = AsyncMock()

        orch.sessions = MagicMock()
        orch.sessions.start_pool = AsyncMock()

        async def _init_dash():
            orch._local_only = True
            orch._configured_host = None
            orch._dashboard_port = 6779

        orch._init_dashboard = _init_dash

        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        with patch.object(loop, "add_signal_handler"):
            with patch("kiro_crew.shutdown_event", fresh_event):
                with patch("kiro_crew.slack.gateway.shutdown_event", fresh_event):
                    with patch(
                        "kiro_crew.slack.gateway.resolve_dashboard_host", return_value="127.0.0.1"
                    ):
                        with patch(
                            "kiro_crew.slack.gateway.format_dashboard_urls",
                            side_effect=RuntimeError("cannot format URL"),
                        ):
                            with patch("kiro_crew.slack.events.init_socket_mode"):
                                with patch("kiro_crew.slack.interactions.init"):
                                    with patch("kiro_crew.slack.events.SeenCache"):
                                        with patch("kiro_crew.session.cleanup_orphaned_sessions"):
                                            with patch(
                                                "kiro_crew.dashboard.handlers._bg_mcp_probe",
                                                new_callable=AsyncMock,
                                            ):
                                                with patch("os._exit"):
                                                    with patch(
                                                        "kiro_crew.platform_compat.raise_nofile_soft_limit"
                                                    ):
                                                        # Must not raise.
                                                        # flake-ok: pre-existing unbounded run that the pre-set shutdown event ends, moved verbatim; tracked for a de-flake pass
                                                        await orch.run()
                                                        # flake-ok: pre-existing pair of event-loop yields before the boot check, moved verbatim; tracked for a de-flake pass
                                                        await asyncio.sleep(0)
                                                        # flake-ok: pre-existing event-loop yield, the second of the pair, moved verbatim; tracked for a de-flake pass
                                                        await asyncio.sleep(0)

        # Boot carried on past the failed announcement.
        orch.sessions.start_pool.assert_awaited_once_with(blocking=False)

    @pytest.mark.asyncio
    async def test_the_session_warm_task_survives_a_gc_pass(self):
        """The probe-gated session warm is held until it finishes.

        The loop holds a task only weakly. A warm parked on a future that
        nothing else references is a garbage cycle unless boot keeps the task.
        """
        orch = _make_orchestrator(no_dashboard=False, no_open=True)
        for name in (
            "_init_services",
            "_start_embeddings",
            "_auto_migrate_memory",
            "_init_cron",
            "_init_heartbeat",
            "_init_autonudge",
            "_check_for_updates",
            "_shutdown",
        ):
            setattr(orch, name, AsyncMock())
        for name in ("_init_mcp_discovery", "_init_subagents", "_init_task_runner"):
            setattr(orch, name, MagicMock())

        parked = asyncio.Event()
        refs: list = []

        async def _start_pool_that_parks(**_kwargs):
            current = asyncio.current_task()
            assert current is not None
            refs.append(weakref.ref(current))
            del current
            parked.set()
            await asyncio.get_running_loop().create_future()

        orch.sessions = MagicMock()
        orch.sessions.start_pool = _start_pool_that_parks

        async def _init_dash():
            orch._local_only = True
            orch._configured_host = None
            orch._dashboard_port = 6779

        orch._init_dashboard = _init_dash

        fresh_event = asyncio.Event()
        fresh_event.set()
        loop = asyncio.get_running_loop()
        with ExitStack() as stack:
            stack.enter_context(patch.object(loop, "add_signal_handler"))
            stack.enter_context(patch("kiro_crew.shutdown_event", fresh_event))
            stack.enter_context(patch("kiro_crew.slack.gateway.shutdown_event", fresh_event))
            stack.enter_context(
                patch("kiro_crew.slack.gateway.resolve_dashboard_host", return_value="127.0.0.1")
            )
            stack.enter_context(
                patch("kiro_crew.slack.gateway.format_dashboard_urls", return_value=["url"])
            )
            stack.enter_context(patch("kiro_crew.slack.events.init_socket_mode"))
            stack.enter_context(patch("kiro_crew.slack.interactions.init"))
            stack.enter_context(patch("kiro_crew.slack.events.SeenCache"))
            stack.enter_context(patch("kiro_crew.session.cleanup_orphaned_sessions"))
            stack.enter_context(
                patch("kiro_crew.dashboard.handlers._bg_mcp_probe", new_callable=AsyncMock)
            )
            stack.enter_context(patch("os._exit"))
            raise_nofile_soft_limit = stack.enter_context(
                patch("kiro_crew.slack.gateway.platform_compat.raise_nofile_soft_limit")
            )
            await finished(orch.run(), "the gateway boot")
            await await_until(parked, what="the session warm to park")

        raise_nofile_soft_limit.assert_called_once_with(10240)
        (ref,) = refs
        gc.collect()
        task = ref()
        assert task is not None, "the session warm task was garbage-collected"
        assert task in orch._background_tasks
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await await_until(task, what="the cancelled warm task")
        assert task not in orch._background_tasks


class TestInitServicesLoopResponsiveness:
    """The event loop must keep servicing callbacks during slow startup work.

    Invariant: the loop runs callbacks one at a time, so a
    synchronous subprocess/scan inside ``_init_services`` starves every other
    coroutine — including the loop-stall watchdog heartbeat once armed — for
    its whole duration.

    These tests assert the PROPERTY, not a duration: a fast ticker
    runs concurrently with the init work, and the stand-in for each slow
    call blocks — in whatever execution context production invoked it —
    until it OBSERVES the ticker advance. Fixed code runs the work off the
    loop (``asyncio.to_thread`` / an async subprocess), so the loop stays
    free, the ticker advances, and the probe returns almost immediately. A
    mutant reverted to the old synchronous on-loop shape blocks the loop
    itself, the ticker can never advance while the probe waits, and the
    probe gives up at a deliberately generous deadline and flags
    starvation.

    The previous shape bounded the ticker's max inter-tick gap by an
    absolute, load-adaptive ceiling; a scheduler stall on a saturated
    runner blew the ceiling with the invariant intact (a 1.87s gap against
    the 0.40s floor at a commit whose diff contained zero Python files).
    Polling for the property makes host contention cost only wall-clock,
    never the verdict, while an on-loop regression still fails
    deterministically: ticks CANNOT happen while the loop is blocked, so no
    amount of waiting turns a mutant green.
    """

    # Ticks a probe must observe while the slow work is in flight. Each tick
    # proves the loop scheduled another coroutine DURING the work; requiring
    # a handful rules out a single lucky wakeup counting as liveness.
    _MIN_TICKS_DURING_WORK = 5
    # How long a probe waits for those ticks before declaring the loop
    # starved. Generous on purpose (testing-conventions § Determinism: poll
    # with a generous deadline): a loaded host only DELAYS ticks, so
    # contention costs wall-clock, never the verdict. Only a blocked loop —
    # where ticks cannot happen at all — exhausts it, so this is paid only
    # on a genuinely regressed run.
    _PROBE_DEADLINE_SECS = 30.0

    @staticmethod
    def _make_ticker(state: dict):
        """A coroutine that advances ``state['ticks']`` whenever the loop is free.

        Stand-in for the loop-stall watchdog heartbeat: anything that blocks
        the loop freezes this counter for the whole block.
        """

        async def _ticker():
            while True:
                await asyncio.sleep(
                    0.01
                )  # flake-ok: measurement tool, sleep interval sets ticker resolution for loop-blockage detection
                state["ticks"] += 1

        return _ticker

    def _make_probe(self, state: dict, label: str, result=None):
        """A stand-in for slow init work that polls the loop-liveness property.

        Runs in whatever execution context production invokes it in. Off the
        loop (the fixed shape) the ticker keeps running, the tick delta
        reaches ``_MIN_TICKS_DURING_WORK`` almost immediately, and *label* is
        recorded in ``state['probed']``. On the loop (the regressed shape)
        the ticker is starved for exactly as long as this function runs, the
        delta can never advance, and *label* lands in ``state['starved']``
        once the deadline expires.
        """

        def _probe(*args, **kwargs):
            start = state["ticks"]
            # flake-ok: pre-existing monotonic deadline of a bounded poll, moved verbatim; tracked for a de-flake pass
            give_up = time.monotonic() + self._PROBE_DEADLINE_SECS
            while state["ticks"] - start < self._MIN_TICKS_DURING_WORK:
                # flake-ok: pre-existing monotonic deadline of a bounded poll, moved verbatim; tracked for a de-flake pass
                if time.monotonic() >= give_up:
                    state["starved"].append(label)
                    return result
                time.sleep(
                    0.01
                )  # flake-ok: measurement tool, yields to let ticker advance during off-loop work probe
            state["probed"].append(label)
            return result

        return _probe

    @pytest.mark.asyncio
    async def test_slow_pip_install_does_not_starve_heartbeat(self):
        orch = _make_orchestrator()
        state: dict = {"ticks": 0, "starved": [], "probed": []}
        _ticker = self._make_ticker(state)

        async def _async_exec(*args, **kwargs):
            # The FIXED shape: production awaits communicate() on the loop,
            # so this waits for the ticker asynchronously — each await IS the
            # loop servicing another callback, which is the property.
            proc = MagicMock()
            proc.returncode = 0
            proc.kill = MagicMock()

            async def _communicate():
                start = state["ticks"]
                # flake-ok: pre-existing monotonic deadline of a bounded poll, moved verbatim; tracked for a de-flake pass
                give_up = time.monotonic() + self._PROBE_DEADLINE_SECS
                while state["ticks"] - start < self._MIN_TICKS_DURING_WORK:
                    # flake-ok: pre-existing monotonic deadline of a bounded poll, moved verbatim; tracked for a de-flake pass
                    if time.monotonic() >= give_up:
                        state["starved"].append("pip-communicate")
                        return (b"", b"")
                    await asyncio.sleep(
                        0.01
                    )  # flake-ok: measurement tool, yields to let ticker advance during on-loop work simulation
                state["probed"].append("pip-communicate")
                return (b"", b"")

            proc.communicate = MagicMock(side_effect=_communicate)
            return proc

        # The OLD, buggy shape: a mutant reverted to subprocess.run invokes
        # this synchronously ON the loop, where the probe's ticks can never
        # arrive — it flags starvation at the deadline. Patched on the
        # subprocess MODULE (not the gateway namespace) because the fixed
        # gateway does not import subprocess at all.
        _blocking_run = self._make_probe(
            state,
            "pip-blocking-run",
            result=MagicMock(returncode=0, stdout="", stderr=b""),
        )

        with patch("importlib.util.find_spec", return_value=None):
            with patch.dict("os.environ", {"KIROCREW_PROJECT_DIR": "/proj"}):
                with patch.object(GatewayOrchestrator, "_is_brazil_install", return_value=False):
                    with patch("asyncio.create_subprocess_exec", side_effect=_async_exec):
                        with patch(
                            "subprocess.run",
                            side_effect=_blocking_run,
                        ):
                            ticker_task = asyncio.create_task(_ticker())
                            try:
                                # Above every probe deadline so a regressed
                                # run fails on the starvation assert below,
                                # not on a torn-down timeout.
                                # flake-ok: pre-existing literal bound above the probe deadlines, moved verbatim; tracked for a de-flake pass
                                await asyncio.wait_for(orch._check_missing_deps(), timeout=60)
                            finally:
                                ticker_task.cancel()
        assert state["starved"] == [], (
            f"loop starved during dep install: {state['starved']} observed no "
            f"ticker progress within {self._PROBE_DEADLINE_SECS:.0f}s"
        )
        assert "pip-communicate" in state["probed"], (
            "dep install never awaited the async subprocess — the loop-free "
            f"path was not taken (probed={state['probed']})"
        )

    @pytest.mark.asyncio
    async def test_slow_fts_rebuild_does_not_starve_heartbeat(self):
        """rebuild_index and vector init scale with usage; both must run off-loop."""
        orch = _make_orchestrator(slack_enabled=False)
        state: dict = {"ticks": 0, "starved": [], "probed": []}
        _ticker = self._make_ticker(state)

        mock_mem_inst = MagicMock()
        mock_mem_inst.init = MagicMock()
        mock_mem_inst.rebuild_index = MagicMock(
            side_effect=self._make_probe(state, "rebuild-index", result=3)
        )
        mock_vm_inst = MagicMock()
        mock_vm_inst.init = MagicMock(side_effect=self._make_probe(state, "vector-init"))
        with patch("kiro_crew.slack.gateway.MemoryStore", return_value=mock_mem_inst):
            with patch("kiro_crew.vector_memory.VectorMemoryStore") as mock_vm:
                mock_vm.return_value = mock_vm_inst
                with patch("kiro_crew.slack.gateway.SkillsLoader"):
                    with patch("kiro_crew.slack.gateway.HookManager"):
                        with patch("kiro_crew.slack.gateway.LessonStore"):
                            with patch("kiro_crew.slack.gateway.ContextBuilder"):
                                with patch(
                                    "kiro_crew.slack.gateway.ConversationLog",
                                    return_value=MagicMock(),
                                ):
                                    with patch("kiro_crew.slack.gateway.SessionManager"):
                                        with patch("kiro_crew.slack.gateway.HistoryConsolidator"):
                                            with patch("kiro_crew.slack.gateway.ChannelHistory"):
                                                with patch(
                                                    "kiro_crew.agent.rebuild_agent_config",
                                                    return_value=Path("/tmp/a"),
                                                ):
                                                    with patch(
                                                        "asyncio.create_subprocess_exec",
                                                        new=AsyncMock(
                                                            return_value=_fake_async_proc(
                                                                stdout=b"kiro-cli 1.30.0"
                                                            )
                                                        ),
                                                    ):
                                                        ticker_task = asyncio.create_task(_ticker())
                                                        try:
                                                            # Above the sum of both probe
                                                            # deadlines so a regressed run
                                                            # fails on the starvation
                                                            # assert, not on teardown.
                                                            # flake-ok: pre-existing literal bound above the probe deadlines, moved verbatim; tracked for a de-flake pass
                                                            await asyncio.wait_for(
                                                                orch._init_services(), timeout=90
                                                            )
                                                            assert state["probed"] == []
                                                            orch.ctx_builder.memory = mock_mem_inst
                                                            with (
                                                                patch(
                                                                    "kiro_crew.context.reset_memory_caches"
                                                                ),
                                                                patch(
                                                                    "kiro_crew.memory_backup.apply_pending_member_restores",
                                                                    return_value={},
                                                                ),
                                                            ):
                                                                # flake-ok: pre-existing literal bound above the probe deadlines, moved verbatim; tracked for a de-flake pass
                                                                assert await asyncio.wait_for(
                                                                    asyncio.to_thread(
                                                                        orch._initialize_memory_worker
                                                                    ),
                                                                    timeout=90,
                                                                )
                                                        finally:
                                                            await asyncio.to_thread(
                                                                orch._stop_memory_startup
                                                            )
                                                            ticker_task.cancel()
        assert state["starved"] == [], (
            f"loop starved during service init: {state['starved']} observed no "
            f"ticker progress within {self._PROBE_DEADLINE_SECS:.0f}s"
        )
        assert {"rebuild-index", "vector-init"} <= set(state["probed"]), (
            "service init skipped a probed call — the invariant was not "
            f"exercised (probed={state['probed']})"
        )
