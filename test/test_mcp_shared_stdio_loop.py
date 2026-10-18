"""What ``run_mcp_stdio_loop`` does with a call it cannot run at once, or at all.

A call that arrives while the worker is busy is queued and answered in order, a
cancelled queued call gets no response, an overflow is refused as busy and
audited, and a ping or an unknown method is still answered while busy. One frame
the loop cannot use costs that frame, never the server process. A pooled backend
whose install an update pruned refuses the call, answers what is queued behind it
and exits so the pool respawns it; with no respawner to replace it, it refuses but
keeps its transport. The shared loop harness, request builders and framing reset
live in ``mcp_shared_helpers.py``.
"""

from __future__ import annotations

import json
import sys

import pytest
from mcp_shared_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _framing_undetected,
    _LoopHarness,
    _slow_then_echo,
    _tools_call,
)
from stray_line_helpers import STRAY_LINES

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.mcp_shared import SKIP

# --- run_mcp_stdio_loop busy-queue behavior ----------------------------------
#
# A tools/call arriving while a worker is busy must not be silently dropped:
# with no response ever written, the client waits forever. These tests
# drive the real loop over a pipe-backed stdin (select() needs a real fd;
# on Windows the loop's stdin pump thread reads the same pipe) and assert
# queued calls are answered FIFO once the worker frees.


class TestStdioLoopBusyQueue:
    def test_tools_call_while_busy_is_queued_and_answered_fifo(self, monkeypatch):
        import time

        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(201, "slow"))
            assert started.wait(timeout=5.0)
            harness.send(_tools_call(202, "fast"))
            harness.send(_tools_call(203, "fast"))
            # Give the busy read loop a beat to buffer both calls
            # flake-ok: pre-existing 300 ms beat for the busy read loop to buffer both calls, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing beat before the busy check, moved verbatim; tracked for a de-flake pass
            time.sleep(0.3)
            assert harness.responses == []  # nothing answered while busy
            release.set()
            assert harness.wait_for(lambda: len(harness.responses) >= 3)
            assert [r[0] for r in harness.responses] == [201, 202, 203]
            assert all(r[2] is None for r in harness.responses)
        finally:
            release.set()
            harness.close()

    def test_cancelled_queued_call_gets_no_response_and_loop_continues(self, monkeypatch):
        import time

        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send(_tools_call(302, "fast"))
            # Let the read loop consume 302 before the cancel arrives: two
            # back-to-back pipe writes can coalesce into one buffered read,
            # in which case cancel-of-queued is best-effort (same as the
            # pre-existing in-flight cancel race).
            # flake-ok: pre-existing 300 ms beat so the read loop consumes 302 before the cancel, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing beat before the cancel, moved verbatim; tracked for a de-flake pass
            time.sleep(0.3)
            harness.send(
                {
                    "jsonrpc": "2.0",
                    "method": "notifications/cancelled",
                    "params": {"requestId": 302},
                }
            )
            # flake-ok: pre-existing 300 ms beat so the cancel is read before the release, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing beat after the cancel, moved verbatim; tracked for a de-flake pass
            time.sleep(0.3)
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 301 for r in harness.responses))
            # Loop must still serve new calls after skipping the cancelled one
            harness.send(_tools_call(303, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 303 for r in harness.responses))
            assert not any(r[0] == 302 for r in harness.responses)
        finally:
            release.set()
            harness.close()

    def test_queue_overflow_returns_busy_error(self, monkeypatch):
        import time

        monkeypatch.setattr(mcp_shared, "PENDING_CALLS_MAX", 1)
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(401, "slow"))
            assert started.wait(timeout=5.0)
            harness.send(_tools_call(402, "fast"))  # fills the queue
            # flake-ok: pre-existing 200 ms beat so the first queued call fills the queue, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing beat before the overflow call, moved verbatim; tracked for a de-flake pass
            time.sleep(0.2)
            harness.send(_tools_call(403, "fast"))  # overflow
            assert harness.wait_for(lambda: any(r[0] == 403 for r in harness.responses))
            overflow = next(r for r in harness.responses if r[0] == 403)
            assert overflow[2] is not None and overflow[2]["code"] == -32000
            # The rejection is a tool-invocation decision and must be SEL-audited
            assert any(
                call.kwargs.get("outcome") == "rejected_busy"
                for call in harness.sel_mock.log_tool_invocation.call_args_list
            )
            release.set()
            assert harness.wait_for(lambda: {401, 402} <= {r[0] for r in harness.responses})
        finally:
            release.set()
            harness.close()

    def test_ping_still_answered_while_busy(self, monkeypatch):
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(501, "slow"))
            assert started.wait(timeout=5.0)
            harness.send({"jsonrpc": "2.0", "id": 599, "method": "ping"})
            assert harness.wait_for(lambda: any(r[0] == 599 for r in harness.responses))
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 501 for r in harness.responses))
        finally:
            release.set()
            harness.close()

    @pytest.mark.parametrize("method", ["server/discover", "resources/list"])
    def test_unknown_method_gets_method_not_found_while_busy(self, monkeypatch, method):
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(601, "slow"))
            assert started.wait(timeout=5.0)
            harness.send({"jsonrpc": "2.0", "id": 699, "method": method, "params": {}})
            # Answered while the slow call still holds the worker, not after it.
            assert harness.wait_for(lambda: any(r[0] == 699 for r in harness.responses))
            assert not any(r[0] == 601 for r in harness.responses)
            answer = next(r for r in harness.responses if r[0] == 699)
            assert answer[2] is not None
            assert answer[2]["code"] == mcp_shared.JSONRPC_METHOD_NOT_FOUND
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 601 for r in harness.responses))
        finally:
            release.set()
            harness.close()


class _ReadSpy:
    """Wraps ``_read_message`` and records what each call returned.

    The interleaving handshake for the busy-loop tests: a test waits until the
    loop has CONSUMED the frame it sent before it lets the tool finish, so the
    frame is read on the busy path rather than wherever timing puts it.
    """

    def __init__(self, monkeypatch) -> None:
        import threading

        self.returned: list = []
        self._lock = threading.Lock()
        real = mcp_shared._read_message

        def _spy(stdin):
            value = real(stdin)
            with self._lock:
                self.returned.append(value)
            return value

        monkeypatch.setattr(mcp_shared, "_read_message", _spy)

    def saw(self, predicate) -> bool:
        with self._lock:
            return any(predicate(v) for v in self.returned)


_PING = {"jsonrpc": "2.0", "id": 7, "method": "ping"}


def _answered(harness, req_id):
    return lambda: any(r[0] == req_id for r in harness.responses)


def _assert_one_refusal_audited(harness) -> None:
    refusals = [
        c
        for c in harness.sel_mock.log_api_access.mock_calls
        if c.kwargs.get("operation") == "tool_call.invalid_params"
    ]
    assert len(refusals) == 1
    assert refusals[0].kwargs["outcome"] == "rejected"


class TestStdioLoopStrayFrames:
    """One frame the loop cannot use costs that frame, never the server process."""

    @pytest.mark.parametrize("stray", sorted(STRAY_LINES))
    def test_the_idle_loop_answers_the_next_request(self, monkeypatch, stray):
        harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
        try:
            harness.send_raw(STRAY_LINES[stray]())
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
        finally:
            harness.close()

    def test_a_cancel_with_non_object_params_is_ignored(self, monkeypatch):
        harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
        try:
            harness.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": [1]})
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
        finally:
            harness.close()

    @pytest.mark.parametrize(
        "policy",
        [
            mcp_shared.ToolPolicy(frozenset(), ""),
            mcp_shared.ToolPolicy(frozenset({"echo"}), ""),
            mcp_shared.ToolPolicy(frozenset(), "resolution_failed"),
        ],
        ids=["empty", "excluded", "unresolved"],
    )
    @pytest.mark.parametrize(
        "params",
        [[1], "x", None, {}, {"name": ""}, {"name": ["echo"]}, {"name": {"n": 1}}, {"name": 5}],
        ids=[
            "list-params",
            "string-params",
            "null-params",
            "no-name",
            "empty-name",
            "list-name",
            "dict-name",
            "int-name",
        ],
    )
    def test_a_tools_call_without_an_object_params_and_string_name_is_invalid_params(
        self, monkeypatch, policy, params
    ):
        """Answered ``-32602`` before the policy check: never dispatched, never
        audited as a call to a tool with no usable name, and audited as the
        refusal it is."""
        ran: list = []
        harness = _LoopHarness(monkeypatch, lambda name, args: ran.append(name) or "ok")
        monkeypatch.setattr(mcp_shared, "_resolve_tool_policy", lambda *a, **k: policy)
        try:
            harness.send({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": params})
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
            reply = next(r for r in harness.responses if r[0] == 9)
            assert reply[2] is not None and reply[2]["code"] == mcp_shared.JSONRPC_INVALID_PARAMS
            assert ran == []
            assert not harness.sel_mock.log_tool_invocation.called
            _assert_one_refusal_audited(harness)
        finally:
            harness.close()

    def test_an_envelope_error_on_a_request_is_answered(self, monkeypatch):
        """Answered, so the caller (the gateway's pending table) does not wait
        for the stub to detach or the hard ceiling."""
        harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
        try:
            harness.send({"jsonrpc": "2.0", "id": 5, "method": 7})
            harness.send({"jsonrpc": "1.0", "id": 6, "method": "ping"})
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
            codes = {r[0]: r[2] and r[2]["code"] for r in harness.responses}
            assert codes[5] == codes[6] == mcp_shared.JSONRPC_INVALID_REQUEST
        finally:
            harness.close()

    def test_a_worker_that_cannot_start_costs_that_call(self, monkeypatch):
        """``Thread.start`` raises at the scope's task ceiling. Nothing holds the
        unstarted thread afterwards: joining it raises, and would end the server
        for every session sharing it."""
        import threading

        harness = _LoopHarness(monkeypatch, lambda name, args: "ok")
        real = threading.Thread

        class _NoWorker(real):  # type: ignore[misc, valid-type]
            def start(self) -> None:
                if getattr(self, "_target", None).__name__ == "_run_tool":
                    raise RuntimeError("can't start new thread")
                super().start()

        monkeypatch.setattr(threading, "Thread", _NoWorker)
        try:
            harness.send(_tools_call(301, "echo"))
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
            reply = next(r for r in harness.responses if r[0] == 301)
            assert reply[2]["code"] == mcp_shared.JSONRPC_INTERNAL_ERROR
            # The refused start is still an audited invocation decision.
            assert [
                (c.kwargs["request_id"], c.kwargs["tool_name"])
                for c in harness.sel_mock.log_tool_invocation.call_args_list
                if c.kwargs.get("outcome") == "failed"
            ] == [("301", "echo")]
        finally:
            monkeypatch.setattr(threading, "Thread", real)
            harness.close()

    def test_a_dispatch_that_raises_is_answered_internal_error(self, monkeypatch):
        def _boom():
            raise RuntimeError("listing failed")

        harness = _LoopHarness(monkeypatch, lambda name, args: "ok", list_tools_fn=_boom)
        try:
            harness.send({"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
            reply = next(r for r in harness.responses if r[0] == 3)
            assert reply[2] == {
                "code": mcp_shared.JSONRPC_INTERNAL_ERROR,
                "message": "Internal error",
            }
        finally:
            harness.close()

    @pytest.mark.parametrize("stray", ["undecodable", "null", "list"])
    def test_a_finished_tool_is_answered_after_a_stray_frame_with_nothing_after_it(
        self, monkeypatch, stray
    ):
        """The busy loop gets back to the worker after each dropped frame. Had it
        skipped internally, it would sit in ``readline`` until the client wrote
        again -- with no heartbeat, forever -- holding the result back."""
        spy = _ReadSpy(monkeypatch)
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send_raw(STRAY_LINES[stray]())
            assert harness.wait_for(lambda: spy.saw(lambda v: v is SKIP))
            release.set()
            assert harness.wait_for(_answered(harness, 301))
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
        finally:
            release.set()
            harness.close()

    @pytest.mark.parametrize(
        "before", ["stray", "request"], ids=["after-a-stray-line", "after-a-request"]
    )
    def test_a_busy_loop_reads_a_frame_already_buffered(self, monkeypatch, before):
        """``select`` polls the descriptor and cannot see what the reader already
        buffered: a ping that comes in the same write as the line before it is
        answered without waiting for the client's next write or the tool."""
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        first = b"null\n" if before == "stray" else (json.dumps(_PING) + "\n").encode()
        second = {"jsonrpc": "2.0", "id": 8, "method": "ping"}
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send_raw(first + (json.dumps(second) + "\n").encode())
            assert harness.wait_for(_answered(harness, 8)), "the buffered ping was stranded"
            assert not _answered(harness, 301)(), "answered only because the tool finished"
        finally:
            release.set()
            harness.close()

    def test_a_busy_tools_call_without_a_usable_name_is_refused_not_queued(self, monkeypatch):
        """The busy path refuses it exactly as the idle one, so it is never
        queued, busy-rejected, or audited as a call to a tool named by whatever
        it sent."""
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send(
                {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": [1]}}
            )
            assert harness.wait_for(_answered(harness, 9))
            reply = next(r for r in harness.responses if r[0] == 9)
            assert reply[2]["code"] == mcp_shared.JSONRPC_INVALID_PARAMS
            assert not _answered(harness, 301)()
            _assert_one_refusal_audited(harness)
            release.set()
            assert harness.wait_for(_answered(harness, 301))
            names = [c.kwargs["tool_name"] for c in harness.sel_mock.log_tool_invocation.mock_calls]
            assert [1] not in names
        finally:
            release.set()
            harness.close()

    def test_a_busy_cancel_with_non_object_params_cancels_nothing(self, monkeypatch):
        spy = _ReadSpy(monkeypatch)
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(301, "slow"))
            assert started.wait(timeout=5.0)
            harness.send({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": [301]})
            assert harness.wait_for(
                lambda: spy.saw(
                    lambda v: isinstance(v, dict) and v.get("method") == "notifications/cancelled"
                )
            )
            release.set()
            # Delivered: a list ``params`` names no request, so 301 is not cancelled.
            assert harness.wait_for(_answered(harness, 301))
            harness.send(_PING)
            assert harness.wait_for(_answered(harness, 7))
        finally:
            release.set()
            harness.close()


class TestStdioLoopExitsWhenItsInstallIsPruned:
    """A backend whose install an update removed must be replaced, not limp on.

    Lazy imports fail there with ``No module named 'kiro_crew.<x>'`` (seen live
    on ``kirocrew-core``: every ``monitor_*`` and ``session_ledger_record`` call)
    and keep failing until the process is gone. The loop answers the call with a
    retryable refusal, answers anything queued behind it, never runs the tool,
    and exits non-zero so the pool respawns it from the current install.
    """

    def setup_method(self):
        mcp_shared._use_content_length = False

    def test_a_call_after_the_prune_is_refused_and_the_process_exits(
        self, monkeypatch, tmp_path
    ) -> None:
        import shutil as _shutil

        from kiro_crew import install_liveness

        package = tmp_path / "0.8.0.4" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.setenv(
            install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE
        )
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, sys.executable)
        exits: list = []
        monkeypatch.setattr(mcp_shared.sys, "exit", exits.append)

        calls: list[str] = []

        def call_tool(name, args):
            calls.append(name)
            return f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "before"))
            assert harness.wait_for(lambda: len(harness.responses) == 1)
            _shutil.rmtree(package.parent)  # the update's prune
            harness.send(_tools_call(2, "monitor_start"))
            assert harness.wait_for(lambda: len(harness.responses) == 2)
            harness._thread.join(timeout=5.0)
            assert not harness._thread.is_alive(), "the loop kept serving a pruned install"
        finally:
            harness.close()

        assert calls == ["before"], "a tool ran from the pruned install"
        req_id, result, error = harness.responses[1]
        assert req_id == 2 and result is None
        assert error["code"] == -32000 and "retry" in error["message"]
        assert exits == [install_liveness.INSTALL_PRUNED_EXIT_CODE]

    def test_calls_queued_behind_the_prune_are_answered_too(self, monkeypatch, tmp_path) -> None:
        """The loop exits on the first refused call; anything already queued
        behind it must get the same retryable answer, not silence."""
        import shutil as _shutil
        import time

        from kiro_crew import install_liveness

        package = tmp_path / "0.8.0.4" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.setenv(
            install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE
        )
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, sys.executable)
        monkeypatch.setattr(mcp_shared.sys, "exit", lambda _code: None)
        call_tool, started, release = _slow_then_echo()
        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "slow"))
            assert started.wait(timeout=5.0)
            # One frame per read so the busy loop's select() sees each and
            # moves it into the pending queue (two frames in one write land in
            # the text buffer together, where select() cannot see the second).
            harness.send(_tools_call(2, "queued-a"))
            # flake-ok: pre-existing 300 ms beat so the busy loop queues each frame from its own read, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing beat between queued frames, moved verbatim; tracked for a de-flake pass
            time.sleep(0.3)
            harness.send(_tools_call(3, "queued-b"))
            # flake-ok: pre-existing 300 ms beat so the busy loop queues each frame from its own read, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing beat between queued frames, moved verbatim; tracked for a de-flake pass
            time.sleep(0.3)
            harness.send(_tools_call(4, "queued-c"))
            # flake-ok: pre-existing 300 ms beat so the busy loop queues each frame from its own read, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing beat between queued frames, moved verbatim; tracked for a de-flake pass
            time.sleep(0.3)
            # Cancelled while it waited: must get no response, as on the
            # ordinary dispatch path.
            harness.send(
                {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 4}}
            )
            # flake-ok: pre-existing 300 ms beat so the cancel is read before the prune, moved verbatim; tracked for a de-flake pass
            # nosemgrep: kirocrew.test-sleep-as-barrier -- pre-existing beat before the prune, moved verbatim; tracked for a de-flake pass
            time.sleep(0.3)
            _shutil.rmtree(package.parent)
            release.set()
            assert harness.wait_for(lambda: len(harness.responses) == 3), harness.responses
            harness._thread.join(timeout=5.0)
        finally:
            harness.close()
        by_id = {r[0]: r for r in harness.responses}
        assert sorted(by_id) == [1, 2, 3], "a cancelled queued call was answered"
        assert by_id[1][2] is None  # in flight before the prune: delivered
        assert by_id[2][2]["code"] == -32000
        assert by_id[3][2]["code"] == -32000

        def _audited(outcome: str) -> list:
            return sorted(
                (c.kwargs["request_id"], c.kwargs["tool_name"])
                for c in harness.sel_mock.log_tool_invocation.call_args_list
                if c.kwargs.get("outcome") == outcome
            )

        # Every refusal is its own audited invocation decision, queued or not.
        assert _audited("rejected_install_pruned") == [("2", "queued-a"), ("3", "queued-b")]
        assert _audited("cancelled") == [("4", "queued-c")]

    def test_an_intact_install_keeps_serving(self, monkeypatch, tmp_path) -> None:
        from kiro_crew import install_liveness

        package = tmp_path / "kiro_crew"
        package.mkdir()
        (package / "__init__.py").write_text("")
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.setenv(
            install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE
        )
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, sys.executable)
        exits: list = []
        monkeypatch.setattr(mcp_shared.sys, "exit", exits.append)
        harness = _LoopHarness(monkeypatch, lambda n, a: f"done:{n}")
        try:
            harness.send(_tools_call(1, "a"))
            harness.send(_tools_call(2, "b"))
            assert harness.wait_for(lambda: len(harness.responses) == 2)
        finally:
            harness.close()
        assert [r[2] for r in harness.responses] == [None, None]
        assert exits == []

    def test_a_directly_launched_server_refuses_but_keeps_its_transport(
        self, monkeypatch, tmp_path
    ) -> None:
        """Without the pool there is no respawner: exiting would leave the
        session with no kirocrew-core/kirocrew-cron tools at all. The server
        stays up and refuses every call with the restart that recovers it."""
        import shutil as _shutil

        from kiro_crew import install_liveness

        package = tmp_path / "0.8.0.4" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.delenv(install_liveness.POOLED_BACKEND_ENV, raising=False)
        exits: list = []
        monkeypatch.setattr(mcp_shared.sys, "exit", exits.append)
        calls: list[str] = []

        def call_tool(name, args):
            calls.append(name)
            return f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "before"))
            assert harness.wait_for(lambda: len(harness.responses) == 1)
            _shutil.rmtree(package.parent)
            harness.send(_tools_call(2, "monitor_start"))
            harness.send(_tools_call(3, "cron_list"))
            assert harness.wait_for(lambda: len(harness.responses) == 3), harness.responses
            assert harness._thread.is_alive(), "a directly launched server exited"
        finally:
            harness.close()

        assert calls == ["before"], "a tool ran from the pruned install"
        for req_id, result, error in harness.responses[1:]:
            assert result is None
            assert error["code"] == -32000 and "restart" in error["message"]
        assert exits == []
        audited = sorted(
            c.kwargs["request_id"]
            for c in harness.sel_mock.log_tool_invocation.call_args_list
            if c.kwargs.get("outcome") == "rejected_install_pruned"
        )
        assert audited == ["2", "3"]

    def test_a_pooled_server_whose_respawn_launcher_was_pruned_keeps_its_transport(
        self, monkeypatch, tmp_path
    ) -> None:
        """The pool respawns from the command it spawned this backend with, and
        that launcher can sit inside the pruned tree. Exiting then gets no
        replacement, so the server must stay up and refuse like a direct one."""
        import shutil as _shutil

        from kiro_crew import install_liveness

        package = tmp_path / "0.8.0.4" / "kiro_crew"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        launcher = tmp_path / "0.8.0.4" / "bin" / "kirocrew"
        launcher.parent.mkdir(parents=True)
        launcher.write_text("#!/bin/sh\n")
        launcher.chmod(0o755)
        monkeypatch.setattr(install_liveness, "_PACKAGE_ROOT", package)
        monkeypatch.setenv(
            install_liveness.POOLED_BACKEND_ENV, install_liveness.POOLED_BACKEND_VALUE
        )
        monkeypatch.setenv(install_liveness.POOLED_RESPAWN_COMMAND_ENV, str(launcher))
        exits: list = []
        monkeypatch.setattr(mcp_shared.sys, "exit", exits.append)
        calls: list[str] = []

        def call_tool(name, args):
            calls.append(name)
            return f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call(1, "before"))
            assert harness.wait_for(lambda: len(harness.responses) == 1)
            _shutil.rmtree(package.parent)  # takes the launcher with it
            harness.send(_tools_call(2, "monitor_start"))
            assert harness.wait_for(lambda: len(harness.responses) == 2), harness.responses
            assert harness._thread.is_alive(), "exited with no working respawn"
        finally:
            harness.close()

        assert calls == ["before"], "a tool ran from the pruned install"
        _req_id, result, error = harness.responses[1]
        assert result is None
        assert error["code"] == -32000 and "restart" in error["message"]
        assert exits == []
