"""Bounded rejection writes: a stalled ACP stdin fails fast as a dead process.

A permission answer goes to the backend over its stdin pipe. ``stdin.drain()``
returns at once while the pipe has room and parks only when the writer is
flow-control paused -- the backend has stopped reading and the pipe behind it
is full. Left alone, the deny path that awaited it stayed parked until the turn
deadline cancelled the coroutine -- the audit record survives that
(test_deny_audit_first.py), but the turn hangs for its whole budget and the
recovery that a *closed* pipe already gets (``AcpProcessDied`` -> session reset
+ bounded requeue) never engages.

Both transports answer permissions: ``AcpClient`` (process per session) and the
shared ``AcpRuntime`` (the default kiro backend, one stdin for every multiplexed
session; its ``AcpRuntimeDead`` is translated to ``AcpProcessDied`` by the
session provider). These tests pin the bound on the response-frame writers of
both -- ``_send_response`` / ``_send_error`` and ``send_response`` /
``send_error``:

* a drain that never completes raises ``AcpProcessDied`` within the bound, on
  both ``reject_tool`` branches (advertised reject option / ``cancelled``
  fallback) and on the unknown-method error reply;
* the failure is logged at warning with the request id, and the id is
  ``repr``-escaped and credential-redacted so a backend-authored id can neither
  inject control characters nor leak a token into the log or the error text;
* a drain that completes is untouched: one write, one drain, ``_last_activity``
  stamped, and the broken-pipe mapping still applies;
* the bound is a NO-PROGRESS bound: a writer whose buffer keeps shrinking (a
  multi-MB prompt frame from another session draining ahead of the response on
  the shared stdin) is a live reader and is waited on past the bound; only a
  buffer that did not shrink for the whole bound is a stall. Without that, the
  shared runtime would be marked dead -- tearing down every co-tenant session --
  by an ordinary large prompt;
* every stdin write on a transport takes that transport's write lock, and the
  response write waits for the lock under the same no-progress bound. The
  buffer-size measurement is exact only with one writer in flight: a
  concurrent append would offset the bytes the reader consumed and read as
  "no progress" on a healthy backend.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import pathlib
import re
import sys
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from test_update_provider import _UNALLOCATABLE_PID

import kiro_crew.acp._dispatch as acp_dispatch
import kiro_crew.acp.client as acp_client
import kiro_crew.acp.runtime as acp_runtime
from kiro_crew.acp.client import (
    AcpClient,
    AcpProcessDied,
    _loggable_request_id,
    await_under_no_progress_bound,
    write_notification_best_effort,
    write_response_frame_bounded,
)
from kiro_crew.acp.runtime import AcpRuntime, AcpRuntimeDead, AcpRuntimeStdinStalled
from kiro_crew.acp.types import JsonRpcMessage

# Short enough that a stalled drain fails in well under a second, long enough
# that a healthy AsyncMock drain (resolves on the next loop tick) never trips it.
_TEST_BOUND = 0.05
# Outer guard: on a build without the bound the stalled drain hangs forever, so
# the test must FAIL (asyncio.TimeoutError is not AcpProcessDied), not hang.
_OUTER_GUARD = 1.0


async def _never_completes(*_args, **_kwargs) -> None:
    await asyncio.Event().wait()


def _client_with_stdin(*, stalled: bool) -> tuple[AcpClient, MagicMock]:
    client = AcpClient()
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock(side_effect=_never_completes) if stalled else AsyncMock()
    proc.returncode = None
    client._process = proc
    return client, proc


def _runtime_with_stdin(*, stalled: bool) -> tuple[AcpRuntime, MagicMock]:
    rt = AcpRuntime(work_dir="/tmp")
    proc = MagicMock()
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock(side_effect=_never_completes) if stalled else AsyncMock()
    proc.returncode = None
    proc.pid = _UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = _UNALLOCATABLE_PID
    rt._initialized = True
    return rt, proc


@pytest.fixture
def short_bound(monkeypatch: pytest.MonkeyPatch) -> float:
    # raising=False: on a build without the bound the attribute is absent and the
    # tests must run (and fail on behaviour), not error at setup. runtime.py binds
    # the name at import, so it is patched there too.
    monkeypatch.setattr(acp_client, "_RESPONSE_WRITE_BOUND_SECS", _TEST_BOUND, raising=False)
    monkeypatch.setattr(acp_runtime, "_RESPONSE_WRITE_BOUND_SECS", _TEST_BOUND, raising=False)
    # Plain mocks expose no progress signal, so they run on the unobservable
    # window; keep it equal to the bound so those tests stay fast and exact.
    monkeypatch.setattr(
        acp_client, "_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS", _TEST_BOUND, raising=False
    )
    # The progress semantics under test are the selector-loop ones; pin that
    # classification so the same tests mean the same thing on the Windows
    # shards, whose real loop is the proactor. The proactor branch has its own
    # tests, which override this.
    monkeypatch.setattr(acp_client, "_is_proactor_loop", lambda _loop: False, raising=False)
    return _TEST_BOUND


class TestStalledDrainFailsFast:
    """Attack: pipe open, reader gone -- the rejection must not hang the turn."""

    @pytest.mark.asyncio
    async def test_reject_tool_cancelled_fallback_raises_within_the_bound(
        self, short_bound: float
    ) -> None:
        client, proc = _client_with_stdin(stalled=True)
        started = time.monotonic()
        with pytest.raises(AcpProcessDied, match=r"stdin stalled.* req='req-7'"):
            await asyncio.wait_for(client.reject_tool("req-7"), timeout=_OUTER_GUARD)
        elapsed = time.monotonic() - started
        assert elapsed < _OUTER_GUARD, "the bound did not fire; the outer guard did"
        assert elapsed >= short_bound * 0.5, "raised before the bound elapsed"
        proc.stdin.write.assert_called_once()
        proc.stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reject_tool_advertised_reject_option_raises_within_the_bound(
        self, short_bound: float
    ) -> None:
        client, proc = _client_with_stdin(stalled=True)
        client._permission_options["req-8"] = {"reject": "reject", "once": "allow"}
        with pytest.raises(AcpProcessDied, match=r"stdin stalled.* req='req-8'"):
            await asyncio.wait_for(client.reject_tool("req-8"), timeout=_OUTER_GUARD)
        proc.stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_error_raises_within_the_bound(self, short_bound: float) -> None:
        client, _proc = _client_with_stdin(stalled=True)
        with pytest.raises(AcpProcessDied, match=r"stdin stalled.* req=42"):
            await asyncio.wait_for(
                client._send_error(42, -32601, "Method not found: x/y"),
                timeout=_OUTER_GUARD,
            )

    @pytest.mark.asyncio
    async def test_stall_is_logged_at_warning_with_the_request_id(
        self, short_bound: float, caplog: pytest.LogCaptureFixture
    ) -> None:
        client, _proc = _client_with_stdin(stalled=True)
        with caplog.at_level(logging.WARNING, logger=acp_client.logger.name):
            with pytest.raises(AcpProcessDied):
                await asyncio.wait_for(client.reject_tool("req-9"), timeout=_OUTER_GUARD)
        stall_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "stalled" in r.getMessage()
        ]
        assert len(stall_records) == 1, [r.getMessage() for r in caplog.records]
        assert "req='req-9'" in stall_records[0].getMessage()
        # The line states the window that was actually measured.
        assert f"for {short_bound:g}s" in stall_records[0].getMessage()

    @pytest.mark.asyncio
    async def test_backend_authored_request_id_is_escaped_in_log_and_message(
        self, short_bound: float, caplog: pytest.LogCaptureFixture
    ) -> None:
        hostile = "req\x1b[2J\nFAKE LOG LINE"
        client, _proc = _client_with_stdin(stalled=True)
        with caplog.at_level(logging.WARNING, logger=acp_client.logger.name):
            with pytest.raises(AcpProcessDied) as excinfo:
                await asyncio.wait_for(client.reject_tool(hostile), timeout=_OUTER_GUARD)
        rendered = [r.getMessage() for r in caplog.records if "stalled" in r.getMessage()]
        assert len(rendered) == 1
        assert "\x1b" not in rendered[0] and "\n" not in rendered[0]
        assert "\x1b" not in str(excinfo.value) and "\n" not in str(excinfo.value)
        assert repr(hostile) in rendered[0]

    @pytest.mark.asyncio
    async def test_credential_shaped_request_id_is_redacted_in_log_and_message(
        self, short_bound: float, caplog: pytest.LogCaptureFixture
    ) -> None:
        # The id is backend-authored; a token-shaped one must not reach the
        # gateway log or the exception text that rides into session cards.
        token_id = "ghp_" + "A" * 36
        client, _proc = _client_with_stdin(stalled=True)
        with caplog.at_level(logging.WARNING, logger=acp_client.logger.name):
            with pytest.raises(AcpProcessDied) as excinfo:
                await asyncio.wait_for(client.reject_tool(token_id), timeout=_OUTER_GUARD)
        rendered = [r.getMessage() for r in caplog.records if "stalled" in r.getMessage()]
        assert len(rendered) == 1
        assert token_id not in rendered[0] and "[REDACTED" in rendered[0]
        assert token_id not in str(excinfo.value) and "[REDACTED" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_stall_does_not_stamp_activity(self, short_bound: float) -> None:
        client, _proc = _client_with_stdin(stalled=True)
        client._last_activity = 0.0
        with pytest.raises(AcpProcessDied):
            await asyncio.wait_for(client.reject_tool("req-10"), timeout=_OUTER_GUARD)
        assert client._last_activity == 0.0, "a failed delivery must not look like activity"


class TestHealthyDrainUnchanged:
    """Control: a live pipe sees one write, one drain, and the activity stamp."""

    @pytest.mark.asyncio
    async def test_reject_tool_cancelled_fallback_still_delivers(self) -> None:
        client, proc = _client_with_stdin(stalled=False)
        client._last_activity = 0.0
        await asyncio.wait_for(client.reject_tool("req-1"), timeout=_OUTER_GUARD)
        proc.stdin.write.assert_called_once()
        proc.stdin.drain.assert_awaited_once()
        assert b'"id": "req-1"' in proc.stdin.write.call_args.args[0]
        assert client._last_activity > 0.0

    @pytest.mark.asyncio
    async def test_reject_tool_advertised_option_still_delivers(self) -> None:
        client, proc = _client_with_stdin(stalled=False)
        client._permission_options["req-2"] = {"reject": "reject", "once": "allow"}
        await asyncio.wait_for(client.reject_tool("req-2"), timeout=_OUTER_GUARD)
        payload = proc.stdin.write.call_args.args[0]
        assert b'"optionId": "reject"' in payload
        proc.stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_send_error_still_delivers(self) -> None:
        client, proc = _client_with_stdin(stalled=False)
        await asyncio.wait_for(
            client._send_error(3, -32601, "Method not found: x/y"), timeout=_OUTER_GUARD
        )
        assert b'"code": -32601' in proc.stdin.write.call_args.args[0]
        proc.stdin.drain.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_broken_pipe_mapping_survives_the_bound(self) -> None:
        client, proc = _client_with_stdin(stalled=False)
        proc.stdin.drain.side_effect = BrokenPipeError("Broken pipe")
        with pytest.raises(AcpProcessDied, match="pipe broken"):
            await client.reject_tool("req-4")

    @pytest.mark.asyncio
    async def test_drain_slower_than_a_tick_but_inside_the_bound_succeeds(
        self, short_bound: float
    ) -> None:
        # Backpressure that clears is not a stall: a drain that takes a real
        # fraction of the bound must still succeed.
        async def _slow_drain(*_a, **_k) -> None:
            await asyncio.sleep(short_bound * 0.2)

        client, proc = _client_with_stdin(stalled=False)
        proc.stdin.drain = AsyncMock(side_effect=_slow_drain)
        await asyncio.wait_for(client.reject_tool("req-5"), timeout=_OUTER_GUARD)
        proc.stdin.drain.assert_awaited_once()


class TestRuntimeStalledDrainFailsFast:
    """Same attack on the shared runtime: one stdin for every session."""

    @pytest.mark.asyncio
    async def test_send_response_raises_dead_within_the_bound_and_marks_dead(
        self, short_bound: float, caplog: pytest.LogCaptureFixture
    ) -> None:
        rt, proc = _runtime_with_stdin(stalled=True)
        started = time.monotonic()
        with caplog.at_level(logging.WARNING, logger=acp_runtime.logger.name):
            with pytest.raises(AcpRuntimeDead, match=r"stdin stalled.* req='req-r1'"):
                await asyncio.wait_for(
                    rt.send_response("req-r1", {"outcome": {"outcome": "cancelled"}}),
                    timeout=_OUTER_GUARD,
                )
        assert time.monotonic() - started < _OUTER_GUARD
        assert rt._dead is True, "a stalled shared pipe is a dead runtime"
        proc.stdin.drain.assert_awaited_once()
        # _mark_dead logs its own line carrying the reason; the delivery line is
        # the one that names the request.
        stalled = [r.getMessage() for r in caplog.records if "stdin stalled" in r.getMessage()]
        assert len(stalled) == 1 and "req='req-r1'" in stalled[0]

    @pytest.mark.asyncio
    async def test_send_error_raises_dead_within_the_bound(self, short_bound: float) -> None:
        rt, _proc = _runtime_with_stdin(stalled=True)
        with pytest.raises(AcpRuntimeDead, match=r"stdin stalled.* req=7"):
            await asyncio.wait_for(
                rt.send_error(7, -32601, "Method not found: x/y"), timeout=_OUTER_GUARD
            )
        assert rt._dead is True

    @pytest.mark.asyncio
    async def test_credential_shaped_request_id_is_redacted(
        self, short_bound: float, caplog: pytest.LogCaptureFixture
    ) -> None:
        token_id = "ghp_" + "B" * 36
        rt, _proc = _runtime_with_stdin(stalled=True)
        with caplog.at_level(logging.WARNING, logger=acp_runtime.logger.name):
            with pytest.raises(AcpRuntimeDead) as excinfo:
                await asyncio.wait_for(
                    rt.send_response(token_id, {"outcome": {"outcome": "cancelled"}}),
                    timeout=_OUTER_GUARD,
                )
        stalled = [r.getMessage() for r in caplog.records if "stdin stalled" in r.getMessage()]
        assert len(stalled) == 1
        assert token_id not in stalled[0] and "[REDACTED" in stalled[0]
        assert token_id not in str(excinfo.value) and "[REDACTED" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_healthy_send_response_unchanged(self) -> None:
        rt, proc = _runtime_with_stdin(stalled=False)
        await asyncio.wait_for(
            rt.send_response("req-r2", {"outcome": {"outcome": "cancelled"}}),
            timeout=_OUTER_GUARD,
        )
        proc.stdin.write.assert_called_once()
        proc.stdin.drain.assert_awaited_once()
        assert b'"id": "req-r2"' in proc.stdin.write.call_args.args[0]
        assert rt._dead is False

    @pytest.mark.asyncio
    async def test_broken_pipe_mapping_survives_the_bound(self) -> None:
        rt, proc = _runtime_with_stdin(stalled=False)
        proc.stdin.drain.side_effect = BrokenPipeError("Broken pipe")
        with pytest.raises(AcpRuntimeDead, match="pipe broken"):
            await rt.send_error(3, -32601, "Method not found: x/y")
        assert rt._dead is True


def _stdin_with_buffer(sizes: list[int], *, drain_after: int | None) -> MagicMock:
    """A writer whose reported write-buffer size walks ``sizes`` on each poll.

    ``drain_after`` = number of polls after which the drain completes; ``None``
    parks it forever. The first poll is the baseline taken before waiting.
    """
    stdin = MagicMock()
    polls = {"n": 0}
    gate = asyncio.Event()

    def _size() -> int:
        idx = min(polls["n"], len(sizes) - 1)
        polls["n"] += 1
        if drain_after is not None and polls["n"] > drain_after:
            gate.set()
        return sizes[idx]

    stdin.transport = MagicMock()
    stdin.transport.get_write_buffer_size = MagicMock(side_effect=_size)
    stdin.polls = polls  # poll count = windows observed; the deterministic clock

    async def _drain() -> None:
        await gate.wait()

    stdin.drain = AsyncMock(side_effect=_drain)
    return stdin


class TestNoProgressBound:
    """A paused writer that is still being consumed is alive; one that is not, is dead."""

    @pytest.mark.asyncio
    async def test_shrinking_buffer_keeps_waiting_past_the_bound_and_completes(
        self, short_bound: float
    ) -> None:
        # Baseline 3 MiB, then the reader consumes ~1 MiB per bound window; the
        # drain completes after the third poll. Three bounds elapse and nothing
        # is declared dead.
        stdin = _stdin_with_buffer([3_000_000, 2_000_000, 1_000_000, 0], drain_after=3)
        started = time.monotonic()
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        elapsed = time.monotonic() - started
        assert ok is True
        assert elapsed >= short_bound * 2, "gave up before the buffer stopped shrinking"

    @pytest.mark.asyncio
    async def test_flat_buffer_is_a_stall_at_exactly_one_bound(self, short_bound: float) -> None:
        stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is False
        # baseline poll + exactly one end-of-window poll: no second window.
        assert stdin.polls["n"] == 2, "a flat buffer must fail at the first window"

    @pytest.mark.asyncio
    async def test_progress_then_flat_fails_after_the_flat_window(self, short_bound: float) -> None:
        # One window of progress, then nothing: the second window is the stall.
        stdin = _stdin_with_buffer([3_000_000, 2_000_000, 2_000_000, 2_000_000], drain_after=None)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is False
        # baseline, one window of progress, one flat window: three polls.
        assert stdin.polls["n"] == 3

    @pytest.mark.asyncio
    async def test_growth_is_activity_not_a_stall(self, short_bound: float) -> None:
        # A writer the lock woke ahead of this caller lands its frame on the
        # pipe: the level RISES. That is not a reader that consumed nothing --
        # measure again from the new level; only a level that holds still for a
        # whole window is the stall.
        stdin = _stdin_with_buffer([3_000_000, 5_000_000, 4_000_000, 0], drain_after=3)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_growth_then_flat_is_a_stall_after_the_flat_window(
        self, short_bound: float
    ) -> None:
        stdin = _stdin_with_buffer([3_000_000, 5_000_000, 5_000_000, 5_000_000], drain_after=None)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is False
        assert stdin.polls["n"] == 3

    @pytest.mark.asyncio
    async def test_a_byte_per_window_trickle_is_a_stall(self, short_bound: float) -> None:
        # A reader that consumes one byte per window would extend the window
        # forever; a drop under the progress floor is not consumption.
        stdin = _stdin_with_buffer([3_000_000, 2_999_999, 2_999_998], drain_after=None)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is False
        assert stdin.polls["n"] == 2, "a sub-floor drop must fail at the first window"

    @pytest.mark.asyncio
    async def test_a_drop_of_exactly_the_floor_is_progress(self, short_bound: float) -> None:
        floor = acp_client._RESPONSE_WRITE_MIN_PROGRESS_BYTES
        stdin = _stdin_with_buffer(
            [3_000_000, 3_000_000 - floor, 3_000_000 - 2 * floor, 0], drain_after=3
        )
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_a_slow_but_real_reader_is_never_capped(self, short_bound: float) -> None:
        # Ten windows of above-floor progress on a large frame: no flat elapsed
        # ceiling ends the wait -- the frame drains and the write completes.
        floor = acp_client._RESPONSE_WRITE_MIN_PROGRESS_BYTES
        levels = [10 * floor - k * floor for k in range(11)]  # 10f, 9f, ..., 0
        stdin = _stdin_with_buffer(levels, drain_after=10)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is True
        assert stdin.polls["n"] >= 10

    @pytest.mark.asyncio
    async def test_completion_at_the_window_edge_is_success(self, short_bound: float) -> None:
        # The drain resolves a hair before the first window closes, on a flat
        # buffer. Whether the completion or the window's timer is observed
        # first, the answer is "completed" -- never a stall verdict on a write
        # that finished.
        stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        gate = asyncio.Event()

        async def _drain_at_the_edge() -> None:
            await gate.wait()

        stdin.drain = AsyncMock(side_effect=_drain_at_the_edge)
        loop = asyncio.get_running_loop()
        loop.call_later(short_bound * 0.98, gate.set)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is True

    @pytest.mark.asyncio
    async def test_unreadable_buffer_fails_closed_at_the_bound(self, short_bound: float) -> None:
        # No get_write_buffer_size: progress cannot be observed, so the elapsed
        # bound alone is the stall (the behaviour every plain-mock test relies on).
        stdin = MagicMock()
        stdin.transport = object()
        stdin.drain = AsyncMock(side_effect=_never_completes)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is False

    @pytest.mark.asyncio
    async def test_runtime_shrinking_buffer_does_not_mark_the_runtime_dead(
        self, short_bound: float
    ) -> None:
        # The shared-runtime false-kill: another session's large frame draining
        # ahead of this response must not tear down every co-tenant session.
        rt, proc = _runtime_with_stdin(stalled=False)
        proc.stdin = _stdin_with_buffer([3_000_000, 2_000_000, 1_000_000, 0], drain_after=3)
        proc.stdin.write = MagicMock()
        await asyncio.wait_for(
            rt.send_response("req-r3", {"outcome": {"outcome": "cancelled"}}),
            timeout=_OUTER_GUARD,
        )
        assert rt._dead is False
        proc.stdin.write.assert_called_once()

    @pytest.mark.asyncio
    async def test_client_shrinking_buffer_does_not_raise(self, short_bound: float) -> None:
        client, proc = _client_with_stdin(stalled=False)
        proc.stdin = _stdin_with_buffer([3_000_000, 2_000_000, 1_000_000, 0], drain_after=3)
        proc.stdin.write = MagicMock()
        client._last_activity = 0.0
        await asyncio.wait_for(client.reject_tool("req-c3"), timeout=_OUTER_GUARD)
        assert client._last_activity > 0.0


class TestUnobservableProgress:
    """Under the proactor loop (Windows subprocess pipes) the write level is flat
    for the whole in-flight write, so flatness is not a stall. The wait falls
    back to the platform-limited window and elapsed time alone decides -- a live
    reader on a large frame gets its time."""

    @pytest.mark.asyncio
    async def test_the_running_loop_decides_and_matches_the_platform(self) -> None:
        # Real classification, on this platform's real loop: the proactor loop
        # is Windows-only, so the answer must agree with the platform. (The
        # Windows CI shards are where the proactor branch is exercised.)
        loop = asyncio.get_running_loop()
        assert acp_client._is_proactor_loop(loop) == (sys.platform == "win32")
        stdin = MagicMock()
        stdin.transport = MagicMock()
        stdin.transport.get_write_buffer_size = MagicMock(return_value=7)
        expected = None if sys.platform == "win32" else 7
        assert acp_client._pending_write_bytes(stdin) == expected

    @pytest.mark.asyncio
    async def test_flat_level_under_proactor_waits_the_platform_window_not_the_bound(
        self, short_bound: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(acp_client, "_is_proactor_loop", lambda _loop: True)
        monkeypatch.setattr(acp_client, "_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS", short_bound * 3)
        gate = asyncio.Event()
        stdin = MagicMock()
        stdin.transport = MagicMock()
        stdin.transport.get_write_buffer_size = MagicMock(return_value=3_000_000)  # flat

        async def _drain() -> None:
            await gate.wait()

        stdin.drain = AsyncMock(side_effect=_drain)
        loop = asyncio.get_running_loop()
        loop.call_later(short_bound * 2, gate.set)  # a live reader, slower than one bound
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is True, "a flat proactor level was read as a stall"

    @pytest.mark.asyncio
    async def test_proactor_stall_is_still_bounded_by_the_platform_window(
        self, short_bound: float, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(acp_client, "_is_proactor_loop", lambda _loop: True)
        monkeypatch.setattr(acp_client, "_RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS", short_bound * 2)
        stdin = MagicMock()
        stdin.transport = MagicMock()
        stdin.transport.get_write_buffer_size = MagicMock(return_value=3_000_000)
        stdin.drain = AsyncMock(side_effect=_never_completes)
        started = time.monotonic()
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is False
        assert time.monotonic() - started >= short_bound * 1.5, "gave up before the window"


class TestBoundedByConstruction:
    """The total wait is bounded without a flat ceiling: a finite, non-negative
    level minus at least one floor per continued window ends within
    backlog/floor + 1 windows. No fixed figure can be right when the largest
    frame is unbounded (any number of 5 MiB image blocks)."""

    @pytest.mark.asyncio
    async def test_a_reader_at_the_floor_drains_any_backlog_and_is_never_killed_early(
        self, short_bound: float
    ) -> None:
        # Eight "image blocks" worth of floor units, consumed exactly one floor
        # per window: the frame drains; nothing ends the wait before it does.
        floor = acp_client._RESPONSE_WRITE_MIN_PROGRESS_BYTES
        units = 8
        levels = [units * floor - k * floor for k in range(units + 1)]  # 8f ... 0
        stdin = _stdin_with_buffer(levels, drain_after=units)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is True
        assert stdin.polls["n"] <= units + 2, "more windows than backlog/floor + 1"

    @pytest.mark.asyncio
    async def test_wait_ends_within_backlog_over_floor_plus_one_windows_when_the_reader_stops(
        self, short_bound: float
    ) -> None:
        # The bound is structural: a reader that clears N windows and then stops
        # is ended at window N+1, however large the original backlog was.
        floor = acp_client._RESPONSE_WRITE_MIN_PROGRESS_BYTES
        stdin = _stdin_with_buffer(
            [100 * floor, 99 * floor, 98 * floor, 98 * floor, 98 * floor], drain_after=None
        )
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is False
        assert stdin.polls["n"] == 4  # baseline + 2 accepted windows + the stall window

    @pytest.mark.asyncio
    async def test_a_sibling_frame_landing_mid_wait_adds_only_its_own_backlog(
        self, short_bound: float
    ) -> None:
        # Baseline one floor; a 6-floor sibling frame lands, then drains one
        # floor per window. The wait continues exactly as long as that backlog
        # needs and the response then goes out.
        floor = acp_client._RESPONSE_WRITE_MIN_PROGRESS_BYTES
        levels = [floor, 7 * floor] + [7 * floor - k * floor for k in range(1, 8)]
        stdin = _stdin_with_buffer(levels, drain_after=8)
        ok = await asyncio.wait_for(
            await_under_no_progress_bound(stdin.drain(), stdin, bound_secs=short_bound),
            timeout=_OUTER_GUARD * 2,
        )
        assert ok is True

    def test_unobservable_window_sizing_is_derived_from_the_largest_plausible_frame(
        self,
    ) -> None:
        # With no level to derive from (proactor), one fixed figure remains:
        # ~30 MiB at the slowest healthy local-pipe rate (~40 KiB/s) fits
        # inside it, and it stays far below any turn deadline (hours).
        largest_frame_bytes = 30 * 1024 * 1024
        slowest_healthy_rate = 40 * 1024
        assert (
            acp_client._RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS
            >= largest_frame_bytes / slowest_healthy_rate
        )
        assert acp_client._RESPONSE_WRITE_UNOBSERVABLE_BOUND_SECS <= 3600


class TestCancelIsNeverSwallowedByTheLock:
    """A cancel notification is the one signal that can end a wedged turn."""

    @pytest.mark.asyncio
    async def test_lock_held_by_a_stalled_writer_appends_the_cancel_unlocked(
        self, short_bound: float
    ) -> None:
        stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        stdin.write = MagicMock()
        lock = asyncio.Lock()
        await lock.acquire()  # a prompt frame parked on a reader that stopped
        outcome = await asyncio.wait_for(
            write_notification_best_effort(stdin, lock, b"cancel\n", bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert outcome == "appended_unlocked"
        stdin.write.assert_called_once_with(b"cancel\n")
        assert lock.locked(), "the holder's lock is not stolen"

    @pytest.mark.asyncio
    async def test_free_lock_writes_and_drains_under_it(self, short_bound: float) -> None:
        stdin = _stdin_with_buffer([0, 0], drain_after=0)
        stdin.write = MagicMock()
        lock = asyncio.Lock()
        outcome = await asyncio.wait_for(
            write_notification_best_effort(stdin, lock, b"cancel\n", bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert outcome == "drained"
        stdin.write.assert_called_once()
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_client_cancel_session_is_delivered_past_a_parked_holder(
        self, short_bound: float
    ) -> None:
        client, proc = _client_with_stdin(stalled=False)
        client._session_id = "sid"
        proc.stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        proc.stdin.write = MagicMock()
        await client._stdin_write_lock().acquire()
        await asyncio.wait_for(client.cancel_session(), timeout=_OUTER_GUARD)
        proc.stdin.write.assert_called_once()
        assert client._cancelled is True

    @pytest.mark.asyncio
    async def test_undrained_cancel_does_not_refresh_the_activity_clock(
        self, short_bound: float
    ) -> None:
        # An unlocked append behind a parked holder is not evidence the backend
        # moved; the idle probes must not be deferred by it (client and runtime).
        client, proc = _client_with_stdin(stalled=False)
        client._session_id = "sid"
        client._last_activity = 0.0
        proc.stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        proc.stdin.write = MagicMock()
        await client._stdin_write_lock().acquire()
        await asyncio.wait_for(client.cancel_session(), timeout=_OUTER_GUARD)
        assert client._last_activity == 0.0

        rt, rproc = _runtime_with_stdin(stalled=False)
        rt._last_activity = 0.0
        rproc.stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        rproc.stdin.write = MagicMock()
        await rt._stdin_write_lock().acquire()
        await asyncio.wait_for(
            rt.send_notification("session/cancel", {"sessionId": "s1"}), timeout=_OUTER_GUARD
        )
        assert rt._last_activity == 0.0

    @pytest.mark.asyncio
    async def test_drained_cancel_refreshes_the_activity_clock(self, short_bound: float) -> None:
        rt, rproc = _runtime_with_stdin(stalled=False)
        rt._last_activity = 0.0
        await asyncio.wait_for(
            rt.send_notification("session/cancel", {"sessionId": "s1"}), timeout=_OUTER_GUARD
        )
        assert rt._last_activity > 0.0

    @pytest.mark.asyncio
    async def test_runtime_notification_is_delivered_past_a_parked_holder(
        self, short_bound: float
    ) -> None:
        rt, proc = _runtime_with_stdin(stalled=False)
        proc.stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        proc.stdin.write = MagicMock()
        await rt._stdin_write_lock().acquire()
        await asyncio.wait_for(
            rt.send_notification("session/cancel", {"sessionId": "s1"}), timeout=_OUTER_GUARD
        )
        proc.stdin.write.assert_called_once()
        assert rt._dead is False


class TestWriteLock:
    """One writer in flight per transport; the response waits for the lock under
    the same no-progress bound, so a live prior frame is waited on and a dead
    one is a stall -- without ever writing behind a dead reader."""

    @pytest.mark.asyncio
    async def test_lock_held_by_a_stalled_writer_is_a_stall_without_writing(
        self, short_bound: float
    ) -> None:
        stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        stdin.write = MagicMock()
        lock = asyncio.Lock()
        await lock.acquire()  # a prior frame's drain is parked on a dead reader
        ok = await asyncio.wait_for(
            write_response_frame_bounded(stdin, lock, b"{}\n", bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        assert ok is False
        stdin.write.assert_not_called(), "nothing may be appended behind a dead reader"
        assert lock.locked(), "the stalled holder's lock is not stolen"

    @pytest.mark.asyncio
    async def test_lock_held_by_a_progressing_writer_is_waited_on_then_written(
        self, short_bound: float
    ) -> None:
        # The holder is a 3 MiB prompt frame being consumed ~1 MiB per window;
        # it releases after three windows. The response must wait, then write.
        stdin = _stdin_with_buffer([3_000_000, 2_000_000, 1_000_000, 0, 0], drain_after=1)
        stdin.write = MagicMock()
        lock = asyncio.Lock()
        await lock.acquire()

        async def _holder_releases_later() -> None:
            await asyncio.sleep(short_bound * 2.5)
            lock.release()

        holder = asyncio.ensure_future(_holder_releases_later())
        started = time.monotonic()
        ok = await asyncio.wait_for(
            write_response_frame_bounded(stdin, lock, b"{}\n", bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        await holder
        assert ok is True
        assert time.monotonic() - started >= short_bound * 2, "did not wait for the live holder"
        stdin.write.assert_called_once_with(b"{}\n")
        assert not lock.locked(), "the response releases the lock after its drain"

    @pytest.mark.asyncio
    async def test_cancellation_landing_as_the_acquire_completes_does_not_orphan_the_lock(
        self, short_bound: float
    ) -> None:
        # The acquire is shielded from the caller's cancellation. If the holder
        # releases and the caller is cancelled in the same loop turn, the
        # acquire completes first and the cancelled caller must give the lock
        # back -- otherwise every later write on the transport reads as dead.
        stdin = _stdin_with_buffer([0, 0, 0], drain_after=0)
        stdin.write = MagicMock()
        lock = asyncio.Lock()
        await lock.acquire()
        writer = asyncio.ensure_future(
            write_response_frame_bounded(stdin, lock, b"{}\n", bound_secs=short_bound)
        )
        await asyncio.sleep(0)  # the writer parks on lock.acquire()
        lock.release()  # wakes the shielded acquire ...
        writer.cancel()  # ... and cancels the caller in the same turn
        with pytest.raises(asyncio.CancelledError):
            await writer
        await asyncio.sleep(0)
        assert not lock.locked(), "the cancelled caller left the stdin lock held"
        stdin.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_stall_verdict_racing_a_late_acquire_releases_the_lock(
        self, short_bound: float
    ) -> None:
        # Flat buffer: the lock wait is judged a stall. If the holder releases
        # right as the verdict lands, the acquire may still complete -- and the
        # lock must not stay held by a caller that already returned False.
        stdin = _stdin_with_buffer([3_000_000, 3_000_000, 3_000_000], drain_after=None)
        stdin.write = MagicMock()
        lock = asyncio.Lock()
        await lock.acquire()

        async def _release_at_the_bound() -> None:
            await asyncio.sleep(short_bound)
            lock.release()

        holder = asyncio.ensure_future(_release_at_the_bound())
        ok = await asyncio.wait_for(
            write_response_frame_bounded(stdin, lock, b"{}\n", bound_secs=short_bound),
            timeout=_OUTER_GUARD,
        )
        await holder
        await asyncio.sleep(0)
        assert not lock.locked()
        if not ok:
            stdin.write.assert_not_called()

    @pytest.mark.asyncio
    async def test_runtime_request_in_flight_does_not_mask_the_response_progress(
        self, short_bound: float
    ) -> None:
        # The masking attack: a concurrent large frame appended while the reader
        # consumes would hide the shrink. Under the lock the prompt frame is the
        # only writer until its drain returns, then the response is the only
        # writer; a healthy shared runtime stays alive.
        rt, proc = _runtime_with_stdin(stalled=False)
        released = asyncio.Event()
        levels = [3_000_000, 2_000_000, 1_000_000, 0]
        polls = {"n": 0}

        def _level() -> int:
            idx = min(polls["n"], len(levels) - 1)
            polls["n"] += 1
            return levels[idx]

        async def _slow_prompt_drain() -> None:
            await asyncio.sleep(short_bound * 2.5)  # a live reader on a big frame
            released.set()

        drains = {"n": 0}

        async def _drain() -> None:
            drains["n"] += 1
            if drains["n"] == 1:
                await _slow_prompt_drain()

        proc.stdin.drain = AsyncMock(side_effect=_drain)
        proc.stdin.transport = MagicMock()
        proc.stdin.transport.get_write_buffer_size = MagicMock(side_effect=_level)

        prompt = asyncio.ensure_future(rt.send_request("session/prompt", {"sessionId": "a"}))
        await asyncio.sleep(0)  # the prompt takes the lock and parks on its drain
        await asyncio.wait_for(
            rt.send_response("req-r4", {"outcome": {"outcome": "cancelled"}}),
            timeout=_OUTER_GUARD,
        )
        prompt_drained_first = released.is_set()
        await prompt
        assert rt._dead is False
        assert proc.stdin.write.call_count == 2
        assert prompt_drained_first, "the response was appended before the prompt frame drained"

    @pytest.mark.asyncio
    async def test_every_runtime_stdin_writer_takes_the_lock(self) -> None:
        rt, proc = _runtime_with_stdin(stalled=False)
        lock = rt._stdin_write_lock()
        held_during_write: list[bool] = []
        proc.stdin.write = MagicMock(side_effect=lambda _d: held_during_write.append(lock.locked()))
        await rt.send_notification("session/cancel", {"sessionId": "a"})
        await rt.send_request("x/y", {})
        await rt.send_response(1, {"ok": True})
        await rt.send_error(2, -32601, "nope")
        assert held_during_write == [True, True, True, True]
        assert not lock.locked()

    @pytest.mark.asyncio
    async def test_every_client_stdin_writer_takes_the_lock(self) -> None:
        client, proc = _client_with_stdin(stalled=False)
        client._next_req_id = MagicMock(return_value=1)
        client._session_id = "sid"
        lock = client._stdin_write_lock()
        held_during_write: list[bool] = []
        proc.stdin.write = MagicMock(side_effect=lambda _d: held_during_write.append(lock.locked()))
        await client._send_request("x/y", {})
        await client._send_response(1, {"ok": True})
        await client._send_error(2, -32601, "nope")
        await client.cancel_session()
        assert held_during_write == [True, True, True, True]
        assert not lock.locked()


class TestQueuedWriterAfterDeath:
    """A writer that queued for the lock while a sibling's stall marked the
    runtime dead must not write afterwards: the recovery path requeues the turn,
    so a prompt written now would run twice."""

    async def _hold_then_die(self, rt: AcpRuntime, short_bound: float) -> None:
        lock = rt._stdin_write_lock()
        await lock.acquire()
        await asyncio.sleep(short_bound)
        rt._mark_dead("sibling stall")
        lock.release()

    @pytest.mark.asyncio
    async def test_queued_send_request_refuses_and_unregisters(self, short_bound: float) -> None:
        rt, proc = _runtime_with_stdin(stalled=False)
        rt._session_queues["s1"] = asyncio.Queue()
        holder = asyncio.ensure_future(self._hold_then_die(rt, short_bound))
        await asyncio.sleep(0)
        with pytest.raises(AcpRuntimeDead):
            await asyncio.wait_for(
                rt.send_request("session/prompt", {"sessionId": "s1"}), timeout=_OUTER_GUARD
            )
        await holder
        proc.stdin.write.assert_not_called()
        assert rt._routed_requests == {}

    @pytest.mark.asyncio
    async def test_queued_send_and_await_refuses_and_unregisters(self, short_bound: float) -> None:
        rt, proc = _runtime_with_stdin(stalled=False)
        holder = asyncio.ensure_future(self._hold_then_die(rt, short_bound))
        await asyncio.sleep(0)
        unretrieved: list[str] = []
        loop = asyncio.get_running_loop()
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _l, ctx: unretrieved.append(str(ctx.get("message"))))
        try:
            with pytest.raises(AcpRuntimeDead):
                await asyncio.wait_for(
                    rt._send_and_await("initialize", {}, timeout=1.0), timeout=_OUTER_GUARD
                )
            await holder
            proc.stdin.write.assert_not_called()
            assert rt._pending_requests == {}
            # The future _mark_dead failed is retrieved by the refused writer, so
            # asyncio does not log a handled death as "never retrieved".
            gc.collect()
            await asyncio.sleep(0)
        finally:
            loop.set_exception_handler(previous)
        assert not any("never retrieved" in m for m in unretrieved), unretrieved

    @pytest.mark.asyncio
    async def test_queued_notification_and_response_refuse(self, short_bound: float) -> None:
        for coro_name in ("send_notification", "send_response"):
            rt, proc = _runtime_with_stdin(stalled=False)
            holder = asyncio.ensure_future(self._hold_then_die(rt, short_bound))
            await asyncio.sleep(0)
            if coro_name == "send_notification":
                coro = rt.send_notification("session/cancel", {"sessionId": "s1"})
            else:
                coro = rt.send_response(7, {"outcome": {"outcome": "cancelled"}})
            with pytest.raises(AcpRuntimeDead):
                await asyncio.wait_for(coro, timeout=_OUTER_GUARD)
            await holder
            proc.stdin.write.assert_not_called(), coro_name
            assert not rt._stdin_write_lock().locked()


class TestOffLoopAutoAnswerKeepsItsAuditReason:
    """The runtime's unroutable-permission auto-answer already had a 30s outer
    guard whose audit reason is ``send_stalled_runtime_dead``. The inner bound
    fires first now; the record must not change shape because of which bound won."""

    @pytest.mark.asyncio
    async def test_inner_stall_is_audited_as_send_stalled(self, short_bound: float) -> None:
        rt, _proc = _runtime_with_stdin(stalled=True)
        audited: list[str] = []
        rt._audit_denied_off_loop = (  # type: ignore[method-assign]
            lambda msg, session_id, reason, title=None: audited.append(reason)
        )
        msg = JsonRpcMessage.from_dict(
            {
                "jsonrpc": "2.0",
                "id": 501,
                "method": "session/request_permission",
                "params": {"sessionId": "child-1", "options": []},
            }
        )
        await asyncio.wait_for(
            rt._answer_unroutable_permission(msg, "child-1", reason="x"), timeout=_OUTER_GUARD
        )
        assert audited == ["x:send_stalled_runtime_dead"]
        assert rt._dead is True

    def test_stall_exception_is_a_runtime_death(self) -> None:
        # Every existing `except AcpRuntimeDead` keeps catching it.
        assert issubclass(AcpRuntimeStdinStalled, AcpRuntimeDead)


class TestLoggableRequestId:
    def test_url_with_credential_like_query_is_redacted_before_credentials(self) -> None:
        hostile = "https://attacker.example/collect?data=" + "A" * 120
        out = _loggable_request_id(hostile)
        assert "attacker.example/collect" not in out and "A" * 40 not in out
        assert "[REDACTED" in out

    def test_output_is_length_capped(self) -> None:
        # Under the input cap, redactor-neutral: exactly the display cap.
        assert len(_loggable_request_id("x" * 3000)) == acp_dispatch._REQUEST_ID_LOG_CAP
        # Over the input cap: the length-only marker, far under the cap.
        out = _loggable_request_id("x" * 10_000)
        assert out.startswith("<id too long: ") and len(out) < acp_dispatch._REQUEST_ID_LOG_CAP

    def test_a_huge_id_is_capped_before_the_redactor_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A multi-MB base64-alphabet run must not hold the loop in the
        # credential scan for a value cut to 256 chars anyway: the redactor
        # sees at most the input cap.
        seen: list[int] = []
        real = acp_dispatch.redact_text

        def _spy(text: str) -> str:
            seen.append(len(text))
            return real(text)

        monkeypatch.setattr(acp_dispatch, "redact_text", _spy)
        out = _loggable_request_id("Ab9" * 1_000_000)
        assert seen == [], "an over-cap id must never reach the redactor"
        assert len(out) <= acp_dispatch._REQUEST_ID_LOG_CAP

    def test_an_over_cap_id_becomes_a_length_only_marker(self) -> None:
        # Truncating would hand the redactor a severed secret; nothing of an
        # over-cap id may survive but its size.
        cap_in = acp_dispatch._REQUEST_ID_REDACT_INPUT_CAP
        out = _loggable_request_id("x" * (cap_in + 5))
        assert out.startswith("<id too long: ") and "x" not in out

    def test_a_collapsed_url_cannot_pull_a_severed_token_inside_the_cap(self) -> None:
        # A 4 KiB suspicious URL followed by a token straddling the input cap:
        # a truncation would leave `ghp_` + 29 chars (below every pattern's
        # floor) and the URL redaction would then collapse the prefix, dragging
        # the fragment inside the display cap. The marker path leaks nothing.
        cap_in = acp_dispatch._REQUEST_ID_REDACT_INPUT_CAP
        token = "ghp_" + "A" * 36
        prefix = "https://attacker.example/collect?data=" + "B" * (cap_in - 45)
        hostile = prefix + " " + token
        assert cap_in - 10 < repr(hostile).index(token) + 7 < cap_in + 40  # straddles
        out = _loggable_request_id(hostile)
        assert "ghp_" not in out and "attacker.example" not in out

    def test_a_credential_straddling_the_display_cap_leaks_no_fragment(self) -> None:
        token = "AKIA" + "STRADDLE0123456A"
        cap = acp_dispatch._REQUEST_ID_LOG_CAP
        hostile = "x" * (cap - 9) + token + " tail"
        out = _loggable_request_id(hostile)
        assert token not in out and token[: cap - (cap - 9) - 1] not in out

    def test_a_credential_straddling_the_cap_leaks_no_fragment(self) -> None:
        # Redact-before-bound: the cap falls inside the token; a slice taken
        # before redaction would leave the token's head in the output.
        token = "AKIA" + "STRADDLE0123456A"
        cap = acp_dispatch._REQUEST_ID_LOG_CAP
        hostile = "x" * (cap - 9) + token + " tail"
        start = repr(hostile).index(token)
        assert start < cap < start + len(token), "premise: the cap must cut the token"
        out = _loggable_request_id(hostile)
        assert token not in out
        assert token[: cap - start] not in out

    def test_plain_ids_pass_through_as_repr(self) -> None:
        assert _loggable_request_id("req-1") == "'req-1'"
        assert _loggable_request_id(42) == "42"


class TestEveryBackendIdLogSiteIsSanitized:
    """Every log line under ``acp/`` that carries a backend-authored JSON-RPC id
    must go through ``_loggable_request_id`` -- the deny-path warnings on both
    transports, the unknown-method warning, the runtime's off-loop
    permission-answer lines, and the read-loop/permission bookkeeping lines."""

    SITES = {
        "src/kiro_crew/acp/client.py": [
            "to req=%s; treating the backend as dead",
            "reject_tool: no deny option advertised for req=%s",
            "ACP: rejecting unknown server request: method=%s id=%s",
            "Deferring inbound server request: method=%s id=%s (waiting for %d)",
            "Deferring non-matching response: id=%s (waiting for %d)",
            "ACP event: method=%s id=%s action=%s",
            "Permission requested for tool: %s (req=%s)",
        ],
        "src/kiro_crew/acp/session_handle.py": [
            "reject_tool: no deny option advertised for req=%s",
            "rejected permission request id=%s stranded in the ",
            "Dropping stray response frame id=%s (no waiter)",
            "id=%s for fidelity-unaware consumer (child=%s)",
        ],
        "src/kiro_crew/acp/runtime.py": [
            "send_notification method=%s: %s; activity clock not refreshed",
            "Dropped %d unroutable frame(s) for session %s (method=%s)",
            "response to req=%s; marking runtime dead",
            "Ownerless server request answered -32601 — method=%s id=%s",
            "answer-task cap (%d) reached at %s request id=%s%s and no ",
            "auto-rejected permission request id=%s for session %s ",
            "answer for permission request id=%s could not be written in ",
            "failed to answer unroutable permission request id=%s",
        ],
        "src/kiro_crew/acp/_dispatch.py": [
            "(req=%s tool_call_id=%s)",
        ],
    }

    def test_no_raw_backend_id_format_slot_is_left_unlisted(self) -> None:
        # Every `id=%s` / `req=%s` log format under acp/ must be one this scan
        # names, so a new site cannot slip in unsanitized.
        root = pathlib.Path(acp_client.__file__).resolve().parents[3]
        pattern = re.compile(r"\b(?:req|id|method)=%[sr]")
        listed = {needle for needles in self.SITES.values() for needle in needles}
        unlisted: list[str] = []
        for rel in self.SITES:
            for i, line in enumerate((root / rel).read_text(encoding="utf-8").splitlines()):
                if pattern.search(line) and not any(n in line for n in listed):
                    unlisted.append(f"{rel}:{i + 1}: {line.strip()}")
        assert unlisted == [], unlisted

    def test_every_backend_id_log_site_passes_the_sanitized_id(self) -> None:
        root = pathlib.Path(acp_client.__file__).resolve().parents[3]
        for rel, needles in self.SITES.items():
            lines = (root / rel).read_text(encoding="utf-8").splitlines()
            for needle in needles:
                hits = [i for i, line in enumerate(lines) if needle in line]
                assert hits, f"{rel}: log site {needle!r} vanished -- update this scan"
                for i in hits:
                    # Every %-slot on a statement that names a backend id is fed
                    # a frame value (ids, session ids, tool-call ids, methods)
                    # unless it carries a value we produce (OWN_SLOTS); each
                    # frame-fed slot must be sanitized. The sanitized value may
                    # be bound a few lines above the format string.
                    stmt = self._statement(lines, i)
                    window = "\n".join(lines[max(0, i - 10) : i + 12])
                    frame_slots = len(re.findall(r"%[sr]", stmt)) - self.OWN_SLOTS.get(needle, 0)
                    assert window.count("_loggable_request_id(") >= frame_slots, (
                        f"{rel}:{i + 1} logs a raw backend value "
                        f"({frame_slots} frame-fed slot(s) on the statement)"
                    )

    # %-slots on a listed statement that carry a value WE produce (a redacted
    # title, a reason string, an outcome literal, an action name), not a frame
    # value. %d slots are counts and are never frame text.
    OWN_SLOTS = {
        "auto-rejected permission request id=%s for session %s ": 3,  # title, reason, outcome
        "answer-task cap (%d) reached at %s request id=%s%s and no ": 2,  # kind, suffix
        "ACP event: method=%s id=%s action=%s": 1,  # action
        "send_notification method=%s: %s; activity clock not refreshed": 1,  # outcome
        "Permission requested for tool: %s (req=%s)": 1,  # title (redacted upstream)
    }

    @staticmethod
    def _statement(lines: list[str], i: int) -> str:
        """The whole format-string literal group around line ``i`` (adjacent
        string-literal lines), so multi-line messages count all their slots."""
        j = i
        while j > 0 and lines[j - 1].strip().startswith('"'):
            j -= 1
        k = i
        while k + 1 < len(lines) and lines[k + 1].strip().startswith('"'):
            k += 1
        return "\n".join(lines[j : k + 1])


class TestBoundSizing:
    def test_response_bound_is_a_pipe_write_with_margin(self) -> None:
        # A response frame is a few hundred bytes; 5s of backpressure on it is a
        # gone reader, not a slow one, and sits far below any turn deadline.
        assert acp_client._RESPONSE_WRITE_BOUND_SECS == 5.0
