"""Tests for the off-loop offload of the blocking ps/pgrep PID helpers.

The wedge fix moves the macOS-blocking PID helpers (``ps``/``pgrep`` spawns +
the ``os.kill`` sweep) off the asyncio event loop onto the dedicated
``subprocess_executor`` pool. These tests pin two contracts that no existing
test covered and that could silently regress to an on-loop call:

1. ``_capture_child_records`` returns the ``{pid: (start_time, basename)}`` map.
2. ``_kill_process`` runs the blocking child-scan and the escaped-child sweep on
   a ``subprocess_executor`` worker thread (``mc-subproc-*``) — never inline on
   the event-loop thread, and never on the maintenance pool the orphan-reaping
   sweep needs.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.acp.client as client_mod
import kiro_crew.executors as ex
from kiro_crew.acp.client import AcpClient, _capture_child_records


def teardown_function() -> None:
    # The offload test creates the process-wide subprocess pool; don't leak it.
    ex.shutdown_maintenance_executor()


class TestCaptureChildRecords:
    def test_returns_start_time_and_basename_per_pid(self) -> None:
        with (
            patch.object(client_mod, "_get_start_time", side_effect=lambda p: p * 10),
            patch.object(client_mod, "_read_basename", side_effect=lambda p: f"bin{p}".encode()),
        ):
            result = _capture_child_records([1, 2, 3])
        assert result == {
            1: (10, b"bin1"),
            2: (20, b"bin2"),
            3: (30, b"bin3"),
        }

    def test_empty_pid_list_returns_empty_map(self) -> None:
        assert _capture_child_records([]) == {}


class TestOffloadRunsOnSubprocessPool:
    """The blocking PID scan + sweep must execute on a subprocess_executor
    worker thread, proving they are off the event loop AND on the dedicated
    pool (not the maintenance pool the orphan sweep relies on)."""

    @pytest.mark.asyncio
    async def test_kill_process_runs_scan_and_sweep_off_loop(self) -> None:
        loop_thread = threading.current_thread()
        scan_threads: list[threading.Thread] = []
        sweep_threads: list[threading.Thread] = []

        def _record_scan(_pid):
            scan_threads.append(threading.current_thread())
            return []

        def _record_sweep(_merged):
            sweep_threads.append(threading.current_thread())

        client = AcpClient()
        proc = MagicMock()
        proc.returncode = None
        proc.pid = 4321
        proc.stdin = proc.stdout = proc.stderr = None

        async def _wait() -> int:
            return 0

        proc.wait = _wait
        client._process = proc
        client._pid = 4321
        client._child_pids = {}

        with (
            patch.object(client_mod, "_get_child_pids", side_effect=_record_scan),
            patch.object(client_mod, "_kill_escaped_children", side_effect=_record_sweep),
            patch.object(client_mod.os, "killpg"),
            patch.object(client_mod.os, "getpgid", return_value=4321),
        ):
            await client._kill_process(force=False)

        assert scan_threads, "_get_child_pids must have run"
        assert sweep_threads, "_kill_escaped_children must have run"
        # Off the loop: never on the thread that drives the event loop.
        for t in scan_threads + sweep_threads:
            assert t is not loop_thread, "blocking helper ran on the event-loop thread"
            assert t.name.startswith("mc-subproc"), (
                f"must run on subprocess_executor (mc-subproc), got {t.name!r}"
            )
        # Sanity: the subprocess pool — not the maintenance pool — owns these.
        assert ex.subprocess_executor()._thread_name_prefix == "mc-subproc"
