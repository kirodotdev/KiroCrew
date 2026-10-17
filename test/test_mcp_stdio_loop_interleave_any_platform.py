"""The stdio loop answers the gateway while a tool call runs, on every platform.

The gateway recycles a pooled backend whose oldest call is past
``HEARTBEAT_TIMEOUT_SECS`` AND whose pings have gone unanswered for
``PING_STALE_SECS`` (``mcp_gateway/backend_liveness.py``). A loop that reads nothing
while a tool runs therefore has every call over five minutes recycled --
``wait`` included -- and every other session's call on the shared
``kirocrew-core`` backend queued behind it.

Windows ``select.select`` cannot poll stdin, so the loop there reads frames on
a pump thread instead. ``reader`` runs each test on the platform's own path and
on the pump path (``IS_POSIX`` patched off), so a POSIX run also covers the
code Windows uses.
"""

from __future__ import annotations

import threading

import pytest
from test_mcp_shared import _initialize, _LoopHarness, _slow_then_echo, _tools_call

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.mcp_caller import INFLIGHT_CAPABILITY_KEY
from kiro_crew.mcp_shared import DeferredTool


@pytest.fixture(autouse=True)
def _framing_undetected(_floor_monkeypatch):
    _floor_monkeypatch.setattr(mcp_shared, "_framing", None)


@pytest.fixture(params=["native", "pump"])
def reader(request, monkeypatch):
    if request.param == "pump":
        monkeypatch.setattr(mcp_shared.platform_compat, "IS_POSIX", False)
    return request.param


def _ping(req_id) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "ping"}


def _answered(harness: _LoopHarness, req_id) -> bool:
    return any(r[0] == req_id for r in harness.responses)


class _Parked(DeferredTool):
    """A deferred call that settles once ``release`` is set."""

    def __init__(self, release: threading.Event) -> None:
        self.release = release

    def due_at(self) -> float:
        return 0.0

    def step(self) -> str | None:
        return "slept" if self.release.is_set() else None


def test_ping_is_answered_while_a_tool_call_runs(monkeypatch, reader):
    call_tool, started, release = _slow_then_echo()
    harness = _LoopHarness(monkeypatch, call_tool)
    try:
        harness.send(_tools_call(1, "slow"))
        assert started.wait(5.0)
        harness.send(_ping(2))
        assert harness.wait_for(
            lambda: _answered(harness, 2)
        ), "a ping sent while a tool ran was not answered until the tool ended"
        assert not _answered(harness, 1)
    finally:
        release.set()
        harness.close()


def test_a_parked_wait_holds_no_other_call(monkeypatch, reader):
    release = threading.Event()
    parked = _Parked(release)
    harness = _LoopHarness(monkeypatch, lambda name, args: parked if name == "wait" else "ok")
    try:
        harness.send(_tools_call(1, "wait"))
        harness.send(_tools_call(2, "other"))
        assert harness.wait_for(
            lambda: _answered(harness, 2)
        ), "a call queued behind a parked wait was not answered"
        assert not _answered(harness, 1)
        release.set()
        assert harness.wait_for(lambda: _answered(harness, 1))
    finally:
        release.set()
        harness.close()


def test_cancel_reaches_a_running_tool(monkeypatch, reader):
    started = threading.Event()
    gate = threading.Event()
    stopped = threading.Event()

    def tool(name, args):
        started.set()
        gate.wait(5.0)
        stopped.set()
        return "late"

    harness = _LoopHarness(monkeypatch, tool)
    try:
        harness.send(_tools_call(1, "slow"))
        assert started.wait(5.0)
        harness.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        )
        harness.send(_ping(2))
        assert harness.wait_for(lambda: _answered(harness, 2))
        gate.set()
        assert stopped.wait(5.0)
        harness.send(_ping(3))
        assert harness.wait_for(lambda: _answered(harness, 3))
        assert not _answered(harness, 1), "a cancelled call was still answered"
    finally:
        gate.set()
        harness.close()


def test_initialize_advertises_inflight_reports_when_asked(monkeypatch, reader):
    harness = _LoopHarness(
        monkeypatch, lambda name, args: "ok", loop_kwargs={"reports_inflight": True}
    )
    try:
        harness.send(_initialize(1))
        assert harness.wait_for(lambda: _answered(harness, 1))
        result = next(r[1] for r in harness.responses if r[0] == 1)
        assert INFLIGHT_CAPABILITY_KEY in result["capabilities"].get("experimental", {})
    finally:
        harness.close()


def test_pump_reads_ahead_only_up_to_its_bound(monkeypatch):
    """Frames past the bound stay in the pipe until the loop takes one."""
    import io
    import os

    monkeypatch.setattr(mcp_shared, "STDIN_PUMP_MAX_FRAMES", 2)
    rfd, wfd = os.pipe()
    stdin = io.TextIOWrapper(io.open(rfd, "rb"))
    try:
        frames = mcp_shared._start_stdin_pump(stdin)
        os.write(
            wfd, b"".join(b'{"jsonrpc":"2.0","id":%d,"method":"ping"}\n' % i for i in range(5))
        )
        got = [frames.get(timeout=5.0)["id"] for _ in range(5)]
        assert got == [0, 1, 2, 3, 4]
        assert frames.maxsize == 2
    finally:
        os.close(wfd)
        assert frames.get(timeout=5.0) is None


def test_end_of_stream_ends_the_loop(monkeypatch, reader):
    harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
    harness.close()
    assert not harness._thread.is_alive()
