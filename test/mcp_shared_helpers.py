"""The stdio-loop harness, request builders and framing reset the mcp_shared tests share.

``test_mcp_shared.py`` and its ``_stdio_loop`` and ``_caller_identity`` siblings each
import the ones they use: ``_framing_undetected``, an autouse fixture every module imports
so each of its tests starts on a stream whose framing is not yet detected;
``_LoopHarness``, which runs ``run_mcp_stdio_loop`` in a thread over a pipe-backed stdin
and records what it answers; ``_tools_call``, ``_tools_call_with_caller`` and
``_initialize``, the requests the loop reads; and ``_slow_then_echo``, which returns a tool
whose ``slow`` call blocks until the test sets the returned release event, with that event
and the one the call sets when it starts. ``test_mcp_shared.py`` re-exports all but the
fixture for the test modules that import them from it.
"""

from __future__ import annotations

import io
import json

import pytest

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.testing.wait import wait_until


@pytest.fixture(autouse=True)
# flake-ok: pre-existing autouse reset of one mcp_shared global through monkeypatch, moved verbatim; no importing module calls monkeypatch.undo()
# nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- pre-existing autouse fixture, moved verbatim; no test in a module that imports it calls monkeypatch.undo()
def _framing_undetected(monkeypatch):
    """Every test starts on a stream whose framing is not yet detected.

    Through ``monkeypatch`` so a test that fails mid-way, or a loop thread that
    detected a framing, cannot leak it into the next test.
    """
    monkeypatch.setattr(mcp_shared, "_framing", None)


class _LoopHarness:
    """Run run_mcp_stdio_loop in a thread against a pipe-backed stdin.

    Responses are captured by patching mcp_shared.respond; SEL and tool-policy
    resolution are stubbed out so the loop needs no gateway environment.
    """

    def __init__(
        self,
        monkeypatch,
        call_tool_fn,
        loop_kwargs: dict | None = None,
        list_tools_fn=None,
        *,
        managed: bool = True,
    ):
        import os
        import sys
        import threading
        from unittest.mock import MagicMock

        self.responses: list = []  # (req_id, result, error)
        rfd, self._wfd = os.pipe()
        self._stdin = io.TextIOWrapper(io.open(rfd, "rb"))
        monkeypatch.setattr(sys, "stdin", self._stdin)
        monkeypatch.setattr(mcp_shared, "respond", self._record)
        monkeypatch.setattr(
            mcp_shared,
            "_resolve_tool_policy",
            lambda *a, **k: mcp_shared.ToolPolicy(frozenset(), ""),
        )
        # The test's handlers stand in for a managed server's own code, so the
        # tool policy applies to them as it does to Kiro Crew's servers.
        # ``managed=False`` keeps the real classifier, for a handler the test
        # deliberately places outside the package.
        if managed:
            monkeypatch.setattr(mcp_shared, "serves_managed_tools", lambda _fn: True)
        self.sel_mock = MagicMock()
        monkeypatch.setattr(mcp_shared, "sel", lambda: self.sel_mock)
        self._os = os
        self._thread = threading.Thread(
            target=mcp_shared.run_mcp_stdio_loop,
            args=("test-server", "0.0.0", list_tools_fn or (lambda: []), call_tool_fn),
            kwargs=loop_kwargs or {},
            daemon=True,
        )
        self._thread.start()

    def _record(self, req_id, result, error=None) -> None:
        self.responses.append((req_id, result, error))

    def send(self, msg: dict) -> None:
        self.send_raw((json.dumps(msg) + "\n").encode("utf-8"))

    def send_raw(self, data: bytes, timeout: float = 5.0) -> None:
        """Write *data* from a helper thread, joined with a bound.

        A write larger than the pipe buffer (about 4 KiB on Windows) blocks
        until the loop reads it, so a loop that died would hang the test here;
        the bound turns that into a failure that names the cause.
        """
        import threading

        def _write_all() -> None:
            view = memoryview(data)
            while view:
                view = view[self._os.write(self._wfd, view) :]

        writer = threading.Thread(target=_write_all, daemon=True)
        writer.start()
        writer.join(timeout)
        assert not writer.is_alive(), "the loop stopped reading stdin"

    def wait_for(self, predicate, timeout: float | None = None) -> bool:
        """Wait until ``predicate`` holds; raises by name, with the responses read so
        far, when it never does. Returns ``True`` so an ``assert`` at the caller reads
        as the barrier it is."""
        wait_until(
            predicate, timeout=timeout, describe=lambda: f"responses so far: {self.responses!r}"
        )
        return True

    def close(self) -> None:
        self._os.close(self._wfd)
        self._thread.join(timeout=5.0)


def _tools_call(req_id, tool_name: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": req_id,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": {}},
    }


def _slow_then_echo():
    """Return (call_tool_fn, started_event, release_event) for a blockable tool."""
    import threading

    started = threading.Event()
    release = threading.Event()

    def call_tool(name, args):
        if name == "slow":
            started.set()
            release.wait(timeout=10.0)
        return f"done:{name}"

    return call_tool, started, release


def _initialize(req_id) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "initialize", "params": {}}


def _tools_call_with_caller(req_id, tool_name: str, session_key: str) -> dict:
    from kiro_crew.mcp_caller import CallerContext, build_caller_meta

    msg = _tools_call(req_id, tool_name)
    msg["params"]["_meta"] = build_caller_meta(
        CallerContext(session_key=session_key, from_gateway=True)
    )
    return msg
