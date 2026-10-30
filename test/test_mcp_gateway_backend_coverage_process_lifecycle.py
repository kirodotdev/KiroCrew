"""How a pooled backend process is spawned, probed for liveness and torn down.

These tests pin ``spawn_backend`` with the subprocess seam faked: the spawn env markers and pipes,
the start id read before any await, no stderr drain without a stderr pipe, and a child killed when
its pipes are missing. The contained temp dir it injects puts ``TMPDIR``, ``TMP`` and ``TEMP`` under
the managed root and overrides ambient keys; an unsealed declaration is honored and a partial one
strips the competing ambient keys, while a sealed declaration (checked off the event loop) or one
whose seal check fails gets a managed dir and a warning that never logs a secret-backed path; an
allocation failure fails open, a failed spawn reclaims the fresh dir and shutdown leaves the dir for
the sweep. ``send_initialize`` seeds the initialize cache past boot noise and detects the
caller-identity capability, forwards a custom client info, refuses to race a running stdout pump and
raises on an error, a non-object result or EOF. The rest is liveness and teardown:
``cancel_in_flight_for_stub`` sends one cancel per in-flight request of the stub,
``recycle_if_idle`` quarantines a backend that still has co-tenants and kills and audits an idle one
(a refused pid reported not recycled, a failed tree kill falling back to a pid kill, an already-dead
backend left alone, a signal neither kill delivered audited as failed), ``_cancel_background_tasks``
clears both pumps, ``shutdown`` closes stdin and escalates to a tree kill and then a process kill,
and ``_heartbeat_once`` reads a reaped, idle or alive backend, pings under the reserved id and
treats a failed ping write as a liveness failure. A topic of the
``test_mcp_gateway_backend_coverage`` family: ``test_mcp_gateway_backend_coverage.py`` keeps the
first-handshake deadline and ``prime_initialize``, the other siblings are
``test_mcp_gateway_backend_coverage_route_backend_line.py``,
``test_mcp_gateway_backend_coverage_call_metrics_logging.py``,
``test_mcp_gateway_backend_coverage_lease_settlement.py``,
``test_mcp_gateway_backend_coverage_caller_grants_eviction.py``,
``test_mcp_gateway_backend_coverage_stdio_pipes.py`` and
``test_mcp_gateway_backend_coverage_mcp_apps.py``, and the shared fixture and helpers live in
``mcp_gateway_backend_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional, cast
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp_gateway_backend_coverage_helpers import _no_real_metrics_file  # noqa: F401 -- autouse
from mcp_gateway_backend_coverage_helpers import (
    _drain,
    _frames,
    _line,
    _make_backend,
    _pool_key,
    _reader,
)

from kiro_crew.mcp_gateway import backend as backend_mod
from kiro_crew.mcp_gateway.backend import (
    HEARTBEAT_PING_ID,
    _PendingRequest,
    send_initialize,
    spawn_backend,
)

# --- Cancellation / recycle / shutdown -------------------------------------


class TestCancelInFlight:
    @pytest.mark.asyncio
    async def test_sends_one_cancel_per_in_flight_request(self) -> None:
        backend = _make_backend()
        backend._pending_requests.update(
            {
                "gw-1": _PendingRequest("s1", 1, "tools/call"),
                "gw-2": _PendingRequest("s1", 2, "tools/call"),
                "gw-3": _PendingRequest("s2", 3, "tools/call"),
            }
        )
        assert await backend.cancel_in_flight_for_stub("s1") == ["gw-1", "gw-2"]
        frames = _frames(backend)
        assert [f["params"]["requestId"] for f in frames] == ["gw-1", "gw-2"]
        assert all(f["method"] == "notifications/cancelled" for f in frames)

    @pytest.mark.asyncio
    async def test_no_in_flight_work_writes_nothing(self) -> None:
        backend = _make_backend()
        assert await backend.cancel_in_flight_for_stub("s1") == []
        assert _frames(backend) == []

    @pytest.mark.asyncio
    async def test_dead_backend_short_circuits(self) -> None:
        backend = _make_backend()
        backend._dead_reason = "gone"
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        assert await backend.cancel_in_flight_for_stub("s1") == []

    @pytest.mark.asyncio
    async def test_broken_pipe_stops_sending(self) -> None:
        backend = _make_backend()
        cast(Any, backend.stdin).write.side_effect = BrokenPipeError("epipe")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        assert await backend.cancel_in_flight_for_stub("s1") == []

    @pytest.mark.asyncio
    async def test_many_cancels_are_summarised_in_the_log(self) -> None:
        backend = _make_backend()
        for i in range(7):
            backend._pending_requests[f"gw-{i}"] = _PendingRequest("s1", i, "tools/call")
        assert len(await backend.cancel_in_flight_for_stub("s1")) == 7


class TestRecycleIfIdle:
    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing class autouse fixture moved verbatim; no test of the class calls monkeypatch.undo()
    def _no_real_signals(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(backend_mod, "SecurityEventLog", MagicMock())
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_process_tree_async", AsyncMock(return_value=True)
        )
        monkeypatch.setattr(
            backend_mod.platform_compat, "kill_pid_async", AsyncMock(return_value=True)
        )

    @pytest.mark.asyncio
    async def test_co_tenants_present_quarantines_instead(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        assert await backend.recycle_if_idle() is False
        assert backend.quarantined is True
        assert backend.is_alive is True

    @pytest.mark.asyncio
    async def test_idle_backend_is_killed_and_audited(self) -> None:
        backend = _make_backend()
        assert await backend.recycle_if_idle() is True
        assert "recycled after last stub detached" in (backend.dead_reason or "")
        cast(Any, backend_mod.platform_compat.kill_process_tree_async).assert_awaited_once()
        cast(Any, backend_mod.SecurityEventLog).return_value.log_api_access.assert_called_once()

    @pytest.mark.asyncio
    async def test_audit_failure_never_breaks_recycle(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sel = MagicMock()
        sel.return_value.log_api_access.side_effect = RuntimeError("sel down")
        monkeypatch.setattr(backend_mod, "SecurityEventLog", sel)
        backend = _make_backend()
        assert await backend.recycle_if_idle() is True

    @pytest.mark.asyncio
    async def test_refused_pid_reports_not_recycled(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "kill_process_tree_async",
            AsyncMock(side_effect=ValueError("refused pid")),
        )
        backend = _make_backend()
        assert await backend.recycle_if_idle() is False
        assert backend.dead_reason is None

    @pytest.mark.asyncio
    async def test_tree_kill_failure_falls_back_to_pid_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "kill_process_tree_async",
            AsyncMock(side_effect=ProcessLookupError()),
        )
        pid_kill = AsyncMock(return_value=True)
        monkeypatch.setattr(backend_mod.platform_compat, "kill_pid_async", pid_kill)
        backend = _make_backend()
        assert await backend.recycle_if_idle() is True
        pid_kill.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_already_dead_backend_is_not_re_killed(self) -> None:
        backend = _make_backend(returncode=0)
        assert await backend.recycle_if_idle() is False
        cast(Any, backend_mod.platform_compat.kill_process_tree_async).assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_signal_the_fallback_could_not_deliver_is_audited_failed_not_killed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A kill that was refused all the way down is recorded as a failure, not a kill.

        Both signals were suppressed and the audit still said ``killed`` with
        the process alive -- the shape the sub-agent and cron reapers record as
        ``failed``. The pool still drops the backend (no consumer is left); its
        reason and the audit row name what the signal could not do.
        """
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "kill_process_tree_async",
            AsyncMock(side_effect=PermissionError("Operation not permitted")),
        )
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "kill_pid_async",
            AsyncMock(side_effect=PermissionError("Operation not permitted")),
        )
        backend = _make_backend()
        assert await backend.recycle_if_idle() is True
        audit = cast(Any, backend_mod.SecurityEventLog).return_value.log_api_access
        audit.assert_called_once()
        assert audit.call_args.kwargs["outcome"] == "failed", (
            "the audit says killed for a process the signal left alive: "
            f"{audit.call_args.kwargs!r}"
        )
        assert "; kill failed: PermissionError: Operation not permitted" in (
            audit.call_args.kwargs["error"]
        )
        assert "; kill failed: PermissionError" in (backend.dead_reason or "")
        assert backend.is_alive is False

    @pytest.mark.asyncio
    async def test_a_process_gone_before_the_fallback_signal_is_a_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: ``ProcessLookupError`` on the fallback means the process is gone -- killed."""
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "kill_process_tree_async",
            AsyncMock(side_effect=OSError("tree not signalable")),
        )
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "kill_pid_async",
            AsyncMock(side_effect=ProcessLookupError()),
        )
        backend = _make_backend()
        assert await backend.recycle_if_idle() is True
        audit = cast(Any, backend_mod.SecurityEventLog).return_value.log_api_access
        assert audit.call_args.kwargs["outcome"] == "killed"
        assert "kill failed" not in (backend.dead_reason or "")


class TestBackgroundTasksAndShutdown:
    @pytest.mark.asyncio
    async def test_cancel_background_tasks_clears_both_pumps(self) -> None:
        backend = _make_backend()
        # flake-ok: pre-existing never-ending stand-in task the code under test cancels, moved verbatim
        backend._stdout_task = asyncio.create_task(asyncio.sleep(3600))
        # flake-ok: pre-existing never-ending stand-in task the code under test cancels, moved verbatim
        backend._stderr_task = asyncio.create_task(asyncio.sleep(3600))
        await backend._cancel_background_tasks()
        assert backend._stdout_task is None and backend._stderr_task is None

    @pytest.mark.asyncio
    async def test_shutdown_of_exited_process_is_a_fast_noop(self) -> None:
        backend = _make_backend(returncode=0)
        await backend.shutdown()
        cast(Any, backend.stdin).close.assert_not_called()
        # The early return records no shutdown reason of its own.
        assert backend.dead_reason is None

    @pytest.mark.asyncio
    async def test_graceful_shutdown_closes_stdin_and_records_reason(self) -> None:
        backend = _make_backend()
        cast(Any, backend.process).wait = AsyncMock(return_value=0)
        await backend.shutdown(timeout=5)
        cast(Any, backend.stdin).close.assert_called_once()
        assert backend.dead_reason is not None
        assert backend.dead_reason.startswith("shutdown rc=")

    @pytest.mark.asyncio
    async def test_shutdown_preserves_an_existing_dead_reason(self) -> None:
        backend = _make_backend()
        backend._dead_reason = "wedged: recycled earlier"
        await backend.shutdown(timeout=5)
        assert backend.dead_reason == "wedged: recycled earlier"

    @pytest.mark.asyncio
    async def test_stdin_close_error_is_swallowed(self) -> None:
        backend = _make_backend()
        cast(Any, backend.stdin).close.side_effect = RuntimeError("already closed")
        await backend.shutdown(timeout=5)
        assert backend.dead_reason is not None

    @pytest.mark.asyncio
    async def test_timeout_escalates_to_tree_kill(self, monkeypatch: pytest.MonkeyPatch) -> None:
        tree_kill = AsyncMock(return_value=True)
        monkeypatch.setattr(backend_mod.platform_compat, "kill_process_tree_async", tree_kill)
        backend = _make_backend()
        # timeout=0 makes the first wait_for fail synchronously.
        await backend.shutdown(timeout=0)
        tree_kill.assert_awaited_once_with(backend.process.pid, backend_mod.platform_compat.SIGKILL)
        cast(Any, backend.process).kill.assert_not_called()
        assert backend.dead_reason == "shutdown rc=None"

    @pytest.mark.asyncio
    async def test_tree_kill_failure_falls_back_to_process_kill(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "kill_process_tree_async",
            AsyncMock(side_effect=OSError("no perm")),
        )
        backend = _make_backend()
        await backend.shutdown(timeout=0)
        cast(Any, backend.process).kill.assert_called_once()
        assert backend.dead_reason == "shutdown rc=None"

    @pytest.mark.asyncio
    async def test_process_kill_of_vanished_pid_is_tolerated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            backend_mod.platform_compat,
            "kill_process_tree_async",
            AsyncMock(side_effect=OSError("no perm")),
        )
        backend = _make_backend()
        cast(Any, backend.process).kill.side_effect = ProcessLookupError()
        await backend.shutdown(timeout=0)
        assert backend.dead_reason is not None


# --- Heartbeat edges not covered by the wedge suite ------------------------


class TestHeartbeatEdges:
    @pytest.mark.asyncio
    async def test_reaped_process_is_gone_with_a_synthesized_reason(self) -> None:
        backend = _make_backend(returncode=7)
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest("s1", 1, "tools/call")
        assert await backend._heartbeat_once(time.monotonic()) == "gone"
        assert backend.dead_reason == "process exited rc=7"
        assert (await _drain(inbox))["id"] == 1

    @pytest.mark.asyncio
    async def test_oldest_pending_scan_keeps_the_first_seen_maximum(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        now = time.monotonic()
        backend._pending_requests.update(
            {
                "gw-old": _PendingRequest("s1", 1, "tools/call", t_start_ms=(now - 30.0) * 1000.0),
                "gw-new": _PendingRequest("s1", 2, "tools/call", t_start_ms=now * 1000.0),
            }
        )
        # Neither is old enough to be wedged, so this stays "alive" — the point
        # is that the younger entry does not displace the older one.
        assert await backend._heartbeat_once(now) == "alive"
        assert backend._warned_slow_ids == set()

    @pytest.mark.asyncio
    async def test_idle_backend_is_left_to_the_idle_sweep(self) -> None:
        backend = _make_backend()
        assert await backend._heartbeat_once(time.monotonic()) == "idle"
        assert _frames(backend) == []

    @pytest.mark.asyncio
    async def test_alive_backend_is_pinged_under_the_reserved_id(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        assert await backend._heartbeat_once(time.monotonic()) == "alive"
        (frame,) = _frames(backend)
        assert frame == {"jsonrpc": "2.0", "id": HEARTBEAT_PING_ID, "method": "ping"}

    @pytest.mark.asyncio
    async def test_ping_write_failure_is_a_liveness_failure(self) -> None:
        backend = _make_backend()
        inbox = await backend.attach_stub("s1")
        backend._pending_requests["gw-1"] = _PendingRequest(
            "s1", 1, "tools/call", t_start_ms=time.monotonic() * 1000.0
        )
        cast(Any, backend.stdin).write.side_effect = ConnectionResetError("reset")
        assert await backend._heartbeat_once(time.monotonic()) == "gone"
        assert "heartbeat ping write failed" in (backend.dead_reason or "")
        assert (await _drain(inbox))["error"]["code"] == -32000

    @pytest.mark.asyncio
    async def test_warned_slow_ids_pruned_when_requests_complete(self) -> None:
        backend = _make_backend()
        await backend.attach_stub("s1")
        backend._warned_slow_ids = {"gw-stale"}
        assert await backend._heartbeat_once(time.monotonic()) == "alive"
        assert backend._warned_slow_ids == set()


# --- spawn_backend / send_initialize ---------------------------------------


class _FakeProcess:
    """Stand-in for asyncio.subprocess.Process with in-memory pipes."""

    def __init__(
        self,
        *,
        with_pipes: bool = True,
        with_stderr: bool = True,
        stderr_lines: bytes = b"",
    ) -> None:
        # nosemgrep: kirocrew.mock-pid-allocatable -- pre-existing mock pid moved verbatim; the process double is never signalled
        self.pid = 5150
        self.returncode: Optional[int] = None
        self.stdin = MagicMock() if with_pipes else None
        if self.stdin is not None:
            self.stdin.drain = AsyncMock()
        self.stdout = asyncio.StreamReader() if with_pipes else None
        self.stderr: Optional[asyncio.StreamReader] = None
        if with_stderr:
            self.stderr = asyncio.StreamReader()
            self.stderr.feed_data(stderr_lines)
            self.stderr.feed_eof()
        self.killed = False

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def fake_spawn(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the real subprocess spawn with an in-memory fake."""
    captured: dict[str, Any] = {}

    async def _fake_exec(program: str, *args: str, **kwargs: Any) -> _FakeProcess:
        captured["program"] = program
        captured["args"] = list(args)
        captured["kwargs"] = kwargs
        proc = _FakeProcess(
            with_pipes=captured.get("with_pipes", True),
            with_stderr=captured.get("with_stderr", True),
            stderr_lines=captured.get("stderr_lines", b""),
        )
        captured["process"] = proc
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    return captured


class TestSpawnBackend:
    @pytest.mark.asyncio
    async def test_spawn_marks_env_and_wires_pipes(self, fake_spawn: dict[str, Any]) -> None:
        fake_spawn["stderr_lines"] = b"boot line\n"
        backend = await spawn_backend(
            _pool_key(),
            "/usr/bin/example-mcp",
            ["--stdio"],
            {"PATH": "/usr/bin"},
            "/nonexistent-work-dir",
        )
        assert fake_spawn["program"] == "/usr/bin/example-mcp"
        assert fake_spawn["args"] == ["--stdio"]
        env = fake_spawn["kwargs"]["env"]
        assert env["PATH"] == "/usr/bin"
        assert env[backend_mod.KIROCREW_SPAWNED_ENV] == backend_mod.KIROCREW_SPAWNED_VALUE
        assert env[backend_mod.POOLED_BACKEND_ENV] == backend_mod.POOLED_BACKEND_VALUE
        assert env[backend_mod.POOLED_RESPAWN_COMMAND_ENV] == "/usr/bin/example-mcp"
        assert fake_spawn["kwargs"]["start_new_session"] is True
        assert backend.pid == 5150
        assert backend._last_ping_response_mono > 0
        assert backend._stderr_task is not None
        # flake-ok: pre-existing await of the stderr drain, which ends at the fake pipe's EOF, moved verbatim
        await backend._stderr_task

    @pytest.mark.asyncio
    async def test_spawn_captures_the_start_id_before_any_await(
        self, fake_spawn: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The backend record publishes this id; it is read once at spawn,
        before the loop can run the child watcher, never at heartbeat time."""
        seen: list[int] = []

        def _start_id(pid: int) -> str:
            seen.append(pid)
            return f"spawn-{pid}"

        monkeypatch.setattr(backend_mod.platform_compat, "get_process_start_id", _start_id)
        backend = await spawn_backend(_pool_key(), "cmd", [], {}, "/nonexistent-work-dir")
        assert backend.start_id == "spawn-5150"
        assert seen == [5150]
        if backend._stderr_task is not None:
            # flake-ok: pre-existing await of the stderr drain, which ends at the fake pipe's EOF, moved verbatim
            await backend._stderr_task

    @pytest.mark.asyncio
    async def test_no_stderr_pipe_means_no_drain_task(self, fake_spawn: dict[str, Any]) -> None:
        fake_spawn["with_stderr"] = False
        backend = await spawn_backend(_pool_key(), "cmd", [], {}, "/nonexistent-work-dir")
        assert backend._stderr_task is None

    @pytest.mark.asyncio
    async def test_missing_pipes_kills_the_child_and_raises(
        self, fake_spawn: dict[str, Any]
    ) -> None:
        fake_spawn["with_pipes"] = False
        with pytest.raises(RuntimeError, match="subprocess pipes not attached"):
            await spawn_backend(_pool_key(), "cmd", [], {}, "/nonexistent-work-dir")
        assert fake_spawn["process"].killed is True


class TestSendInitialize:
    @pytest.mark.asyncio
    async def test_success_seeds_cache_and_detects_capability(self) -> None:
        backend = _make_backend()
        result: dict[str, Any] = {
            "capabilities": {"experimental": {"kirocrew.caller-identity": {}}}
        }
        backend.stdout = cast(
            Any,
            _reader(
                b"backend boot noise, not json\n",
                _line([1, 2, 3]),
                _line({"id": "some-other-id", "result": {}}),
                _line({"jsonrpc": "2.0", "id": backend_mod._GATEWAY_INIT_ID, "result": result}),
            ),
        )
        assert await send_initialize(backend, timeout=5) == result
        assert backend.supports_caller_identity is True
        assert backend._init_state == "ready"
        assert backend._init_result == result
        request = _frames(backend)[0]
        assert request["method"] == "initialize"
        assert request["params"]["clientInfo"]["name"] == "kirocrew-gateway"

    @pytest.mark.asyncio
    async def test_custom_client_info_is_forwarded(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(
            Any,
            _reader(_line({"id": backend_mod._GATEWAY_INIT_ID, "result": {"capabilities": {}}})),
        )
        await send_initialize(backend, client_info={"name": "probe", "version": "9"}, timeout=5)
        assert _frames(backend)[0]["params"]["clientInfo"] == {"name": "probe", "version": "9"}
        assert backend.supports_caller_identity is False

    @pytest.mark.asyncio
    async def test_refuses_to_race_a_running_stdout_pump(self) -> None:
        backend = _make_backend()
        # flake-ok: pre-existing never-ending stand-in pump task that the test's finally cancels, moved verbatim
        task = asyncio.create_task(asyncio.sleep(3600))
        backend._stdout_task = task
        try:
            with pytest.raises(RuntimeError, match="must run before the stdout pump"):
                await send_initialize(backend)
        finally:
            task.cancel()
            # flake-ok: pre-existing await of the stand-in task the finally just cancelled, moved verbatim
            await asyncio.gather(task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_error_response_raises_value_error(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(
            Any, _reader(_line({"id": backend_mod._GATEWAY_INIT_ID, "error": {"code": -1}}))
        )
        with pytest.raises(ValueError, match="returned initialize error"):
            await send_initialize(backend, timeout=5)

    @pytest.mark.asyncio
    async def test_non_dict_result_raises_value_error(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(
            Any, _reader(_line({"id": backend_mod._GATEWAY_INIT_ID, "result": "nope"}))
        )
        with pytest.raises(ValueError, match="missing/malformed result"):
            await send_initialize(backend, timeout=5)

    @pytest.mark.asyncio
    async def test_eof_before_response_raises_value_error(self) -> None:
        backend = _make_backend()
        backend.stdout = cast(Any, _reader(b'{"partial"'))
        with pytest.raises(ValueError, match="closed stdout before initialize response"):
            await send_initialize(backend, timeout=5)


class TestBackendTmpContainment:
    """Spawn injects a contained temp dir; shutdown reclaims it."""

    @pytest.mark.asyncio
    async def test_spawn_contains_temp_under_managed_root(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        from pathlib import Path

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        await spawn_backend(_pool_key(), "/usr/bin/example-mcp", [], {}, "/nonexistent-work-dir")

        env = fake_spawn["kwargs"]["env"]
        root = home / "run" / "mcp-tmp"
        for key in ("TMPDIR", "TMP", "TEMP"):
            assert Path(env[key]).parent == root, key
        assert env["TMPDIR"] == env["TMP"] == env["TEMP"]
        contained = Path(env["TMPDIR"])
        assert contained.is_dir()
        # Liveness anchor for the sweep: allocation + spawn leave an owner
        # record on disk -- the sweep's ONLY state (no in-memory field).
        assert (contained / bt.OWNER_FILENAME).is_file()

    @pytest.mark.asyncio
    async def test_unsealed_declaration_is_honored_verbatim(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        from kiro_crew import sandbox as sandbox_mod
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox_mod, "classify_declared_temp_path", lambda _path: None)

        await spawn_backend(
            _pool_key(),
            "/usr/bin/example-mcp",
            [],
            {"tmpdir": "/mnt/bigdisk/tmp"},
            "/nonexistent-work-dir",
            declared_temp_keys=("tmpdir",),
        )

        env = fake_spawn["kwargs"]["env"]
        assert env["TMPDIR"] == "/mnt/bigdisk/tmp"
        assert "tmpdir" not in env
        assert not (home / "run" / "mcp-tmp").exists() or not any(
            (home / "run" / "mcp-tmp").iterdir()
        )

    @pytest.mark.asyncio
    async def test_sealed_declaration_uses_managed_temp_and_warns(
        self, fake_spawn, monkeypatch, tmp_path, caplog
    ) -> None:
        from kiro_crew import sandbox as sandbox_mod
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        declared = str(home / "run" / "custom-tmp")
        loop_thread = threading.get_ident()
        classifier_threads: list[int] = []

        def _sealed(_path: str) -> str:
            classifier_threads.append(threading.get_ident())
            return "sealed"

        monkeypatch.setattr(sandbox_mod, "classify_declared_temp_path", _sealed)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await spawn_backend(
                _pool_key(),
                "/usr/bin/example-mcp",
                [],
                {"TMPDIR": declared},
                "/nonexistent-work-dir",
                declared_temp_keys=("TMPDIR",),
            )

        env = fake_spawn["kwargs"]["env"]
        managed = Path(env["TMPDIR"])
        assert managed.parent == home / "run" / "mcp-tmp"
        assert env["TMP"] == env["TEMP"] == str(managed)
        assert declared not in env.values()
        assert classifier_threads and all(thread != loop_thread for thread in classifier_threads)
        warning = next(
            record.getMessage()
            for record in caplog.records
            if "ignoring spec-declared" in record.getMessage()
        )
        assert f"TMPDIR={declared!r}" in warning
        assert "inside the sandbox-sealed runtime parent" in warning

    @pytest.mark.skipif(sys.platform == "win32", reason="nothing seals run/ on Windows")
    @pytest.mark.asyncio
    async def test_real_classifier_refuses_a_sealed_declaration(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        from kiro_crew import sandbox as sandbox_mod
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        (home / "run").mkdir(parents=True)
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        monkeypatch.setattr(sandbox_mod, "config_dir", lambda: home)
        declared = str(home / "run" / "custom-tmp")

        await spawn_backend(
            _pool_key(),
            "/usr/bin/example-mcp",
            [],
            {"TMPDIR": declared},
            "/nonexistent-work-dir",
            declared_temp_keys=("TMPDIR",),
        )

        env = fake_spawn["kwargs"]["env"]
        managed = Path(env["TMPDIR"])
        assert managed.parent == home / "run" / "mcp-tmp"
        assert declared not in env.values()

    @pytest.mark.asyncio
    async def test_secret_backed_refusal_never_logs_the_resolved_path(
        self, fake_spawn, monkeypatch, tmp_path, caplog
    ) -> None:
        from kiro_crew import sandbox as sandbox_mod
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        resolved_secret = str(home / "run" / "vault-secret-value")
        monkeypatch.setattr(sandbox_mod, "classify_declared_temp_path", lambda _path: "sealed")

        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await spawn_backend(
                _pool_key(),
                "/usr/bin/example-mcp",
                [],
                {"TMPDIR": resolved_secret},
                "/nonexistent-work-dir",
                declared_temp_keys=("TMPDIR",),
                secret_env_keys=("TMPDIR",),
            )

        warning = next(
            record.getMessage()
            for record in caplog.records
            if "ignoring spec-declared" in record.getMessage()
        )
        assert resolved_secret not in warning
        assert "TMPDIR='<resolved secret>'" in warning

    @pytest.mark.asyncio
    async def test_check_failure_uses_managed_temp_and_warns(
        self, fake_spawn, monkeypatch, tmp_path, caplog
    ) -> None:
        from kiro_crew import sandbox as sandbox_mod
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)
        declared = str(home / "run" / "custom-tmp")

        def _raise(_path: str) -> None:
            raise OSError("classifier unavailable\nFORGED")

        monkeypatch.setattr(sandbox_mod, "classify_declared_temp_path", _raise)

        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await spawn_backend(
                _pool_key(),
                "/usr/bin/example-mcp",
                [],
                {"TMPDIR": declared},
                "/nonexistent-work-dir",
                declared_temp_keys=("TMPDIR",),
            )

        env = fake_spawn["kwargs"]["env"]
        managed = Path(env["TMPDIR"])
        assert managed.parent == home / "run" / "mcp-tmp"
        assert declared not in env.values()
        warning = next(
            record.getMessage()
            for record in caplog.records
            if "ignoring spec-declared" in record.getMessage()
        )
        assert "seal check itself failed" in warning
        assert "\\nFORGED" in warning
        assert "\nFORGED" not in warning

    @pytest.mark.asyncio
    async def test_refusal_allocation_failure_uses_the_platform_default(
        self, fake_spawn, monkeypatch, caplog
    ) -> None:
        from kiro_crew import sandbox as sandbox_mod

        monkeypatch.setattr(sandbox_mod, "classify_declared_temp_path", lambda _path: "sealed")
        monkeypatch.setattr(
            backend_mod,
            "allocate_backend_tmp",
            MagicMock(side_effect=OSError("disk full")),
        )

        with caplog.at_level(logging.WARNING, logger="kiro_crew.mcp_gateway.backend"):
            await spawn_backend(
                _pool_key(),
                "/usr/bin/example-mcp",
                [],
                {"TMPDIR": "/sealed/temp"},
                "/nonexistent-work-dir",
                declared_temp_keys=("TMPDIR",),
            )

        env = fake_spawn["kwargs"]["env"]
        assert not [key for key in env if key.upper() in ("TMPDIR", "TMP", "TEMP")]
        allocation_warning = next(
            record.getMessage()
            for record in caplog.records
            if "could not allocate a contained temp dir" in record.getMessage()
        )
        assert "platform default" in allocation_warning
        assert "inherited temp" not in allocation_warning

    @pytest.mark.asyncio
    async def test_partial_declaration_strips_competing_ambient_keys(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        # A spec declaring only TMP must actually govern the child: tempfile
        # consults TMPDIR before TMP, so leaving the daemon's ambient TMPDIR
        # in place would silently defeat the declaration (macOS always
        # exports one). Yield = declared keys kept, undeclared canonical
        # keys pruned.
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        await spawn_backend(
            _pool_key(),
            "/usr/bin/example-mcp",
            [],
            {"TMPDIR": "/var/folders/xy/ambient", "TMP": "/mnt/bigdisk/tmp"},
            "/nonexistent-work-dir",
            declared_temp_keys=("TMP",),
        )

        env = fake_spawn["kwargs"]["env"]
        assert "TMPDIR" not in env
        assert env["TMP"] == "/mnt/bigdisk/tmp"
        assert not (home / "run" / "mcp-tmp").exists() or not any(
            (home / "run" / "mcp-tmp").iterdir()
        )

    @pytest.mark.asyncio
    async def test_ambient_temp_does_not_suppress_containment(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        # Regression: the resolver folds the daemon's own inherited environ
        # into the spawn env, and macOS always exports TMPDIR (Windows: TMP /
        # TEMP). An env-membership gate read that ambient value as an operator
        # declaration and disabled containment on those platforms entirely.
        # Ambient keys must be OVERRIDDEN by the managed triple.
        from pathlib import Path

        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        await spawn_backend(
            _pool_key(),
            "/usr/bin/example-mcp",
            [],
            {"TMPDIR": "/var/folders/xy/ambient", "TMP": r"C:\Users\x\tmp"},
            "/nonexistent-work-dir",
            declared_temp_keys=(),
        )

        env = fake_spawn["kwargs"]["env"]
        root = home / "run" / "mcp-tmp"
        for key in ("TMPDIR", "TMP", "TEMP"):
            assert Path(env[key]).parent == root, key
        assert Path(env["TMPDIR"]).is_dir()

    @pytest.mark.asyncio
    async def test_spawn_survives_allocation_failure(self, fake_spawn, monkeypatch) -> None:
        # Fail-open: containment is hygiene, not a spawn prerequisite. The
        # allocate step raises when the dir or its owner record cannot be
        # written (ENOSPC / inode exhaustion) -- the spawn proceeds with
        # inherited temp and nothing is left on disk to reclaim.
        from kiro_crew.mcp_gateway import backend as backend_mod

        def _boom(_digest: str):
            raise OSError("no space left on device")

        monkeypatch.setattr(backend_mod, "allocate_backend_tmp", _boom)

        await spawn_backend(
            _pool_key(), "/usr/bin/example-mcp", [], {"PATH": "/usr/bin"}, "/nonexistent-work-dir"
        )

        env = fake_spawn["kwargs"]["env"]
        assert "TMPDIR" not in env

    @pytest.mark.asyncio
    async def test_spawn_failure_reclaims_the_fresh_dir(self, monkeypatch, tmp_path) -> None:
        # Unowned-by-a-live-process dirs are never deleted by the sweeps, so
        # the spawn-failure path is the ONLY reclamation point for a dir
        # whose process never existed.
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        async def _boom(*_args, **_kwargs):
            raise OSError("exec failed")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", _boom)

        with pytest.raises(OSError):
            await spawn_backend(
                _pool_key(), "/usr/bin/example-mcp", [], {}, "/nonexistent-work-dir"
            )

        root = home / "run" / "mcp-tmp"
        assert not root.exists() or list(root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_shutdown_leaves_the_dir_for_the_sweep(
        self, fake_spawn, monkeypatch, tmp_path
    ) -> None:
        # Deliberately NO deletion on shutdown: the launcher's exit is not
        # proof its process TREE is gone (a wrapper exits while its server
        # child lives on). The sweep's dual condition (owner dead AND idle)
        # is the single deletion authority.
        from kiro_crew.mcp_gateway import backend_tmp as bt

        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setattr(bt, "config_dir", lambda: home)

        from pathlib import Path

        backend = await spawn_backend(
            _pool_key(), "/usr/bin/example-mcp", [], {}, "/nonexistent-work-dir"
        )
        tmp_dir = Path(fake_spawn["kwargs"]["env"]["TMPDIR"])
        assert tmp_dir.is_dir()

        fake_spawn["process"].returncode = 0
        await backend.shutdown()

        assert tmp_dir.is_dir(), "shutdown must not delete; the sweep owns deletion"
