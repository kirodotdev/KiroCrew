"""A tool that only WAITS must not hold the stdio loop's single worker.

``run_mcp_stdio_loop`` runs one worker thread per server, so a ``wait`` that
slept its whole duration on it parked every other caller's call behind it --
on the pooled ``kirocrew-core`` backend, every dashboard session at once.
A handler that only waits returns a :class:`DeferredTool`; the loop times its
short steps from its own tick and the worker is free the moment the handler
returns. These tests pin that contract at the loop, plus the in-flight report
the gateway's wedge detector reads.

No test here waits on a fixed sleep: ordering is observed through the loop
itself (a frame answered after another proves the earlier one was consumed,
since one thread processes them in order), and a call's end through the SEL
row the loop writes for it.
"""

from __future__ import annotations

import contextlib
import json
import threading
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from test_mcp_shared import _initialize
from test_mcp_shared import _LoopHarness as _PlainLoopHarness
from test_mcp_shared import _tools_call, _tools_call_with_caller

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew.mcp_caller import (
    INFLIGHT_CAPABILITY_KEY,
    INFLIGHT_NOTIFICATION,
    build_tenant_meta,
    current_caller,
    current_tenant_nonce,
)
from kiro_crew.mcp_shared import DeferredTool, ToolCancelled, drive_deferred

pytestmark = pytest.mark.skipif(
    not mcp_shared.platform_compat.IS_POSIX,
    reason="the deferred timer is the POSIX select path; Windows drives deferreds inline",
)


class _Parked(DeferredTool):
    """A deferred call that settles when ``release`` is set, stepping on each tick."""

    def __init__(
        self, text: str, *, release: threading.Event, hold_step: threading.Event | None = None
    ):
        self.text = text
        self.release = release
        self.hold_step = hold_step
        self.steps = 0
        self.cancelled = False
        self.settled: list[str] = []

    def on_settled(self, text: str) -> None:
        self.settled.append(text)

    def due_at(self) -> float:
        return 0.0  # always due

    def step(self) -> str | None:
        self.steps += 1
        if self.hold_step is not None:
            self.hold_step.wait(timeout=5.0)
        return self.text if self.release.is_set() else None

    def cancel(self) -> None:
        self.cancelled = True


class _IdentityParked(_Parked):
    """Record the identity cleanup receives, including a hook that raises."""

    def __init__(self, *args, cancel_raises: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.cancel_identity: list = []
        self.cancel_raises = cancel_raises

    def cancel(self) -> None:
        self.cancel_identity.append((current_caller(), current_tenant_nonce()))
        super().cancel()
        if self.cancel_raises:
            raise RuntimeError("cleanup failed")


def _identity_call(req_id: Any, tool_name: str, session_key: str, nonce: str) -> dict:
    msg = _tools_call_with_caller(req_id, tool_name, session_key)
    msg["params"]["_meta"].update(build_tenant_meta(nonce))
    return msg


def _tools_call_named(req_id: Any, tool_name: str) -> dict:
    return _tools_call(req_id, tool_name)


def contextlib_suppress():
    """The harness's close writes to a loop that may already be gone."""
    return contextlib.suppress(Exception)


def _ping(req_id: Any) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "method": "ping"}


def _cancel(req_id: Any) -> dict:
    return {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": req_id}}


def _audits(harness: _LoopHarness, req_id: Any, outcome: str) -> list:
    return [
        c
        for c in harness.sel_mock.log_tool_invocation.call_args_list
        if c.kwargs.get("outcome") == outcome and c.kwargs.get("request_id") == str(req_id)
    ]


def _text(harness: _LoopHarness, req_id: Any) -> str:
    return next(r for r in harness.responses if r[0] == req_id)[1]["content"][0]["text"]


class _LoopHarness(_PlainLoopHarness):
    """The shared harness, started the way ``kirocrew-core`` starts its loop:
    with the in-flight report opted in, so these tests exercise that path."""

    def __init__(self, monkeypatch, call_tool_fn, loop_kwargs=None, list_tools_fn=None):
        super().__init__(
            monkeypatch,
            call_tool_fn,
            {"reports_inflight": True, **(loop_kwargs or {})},
            list_tools_fn,
        )


def _initialize_asking_for_inflight(req_id: Any) -> dict:
    msg = _initialize(req_id)
    msg["params"] = {"capabilities": {"experimental": {INFLIGHT_CAPABILITY_KEY: {}}}}
    return msg


class TestDeferredToolFreesTheWorker:
    def test_a_parked_call_does_not_block_the_next_call(self, monkeypatch):
        """The whole point: with one call parked, a second call is answered at
        once instead of queueing behind it."""
        release = threading.Event()
        parked = _Parked("slept", release=release)

        def call_tool(name, args):
            return parked if name == "wait" else f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(
                lambda: parked.steps >= 1
            ), "the loop never stepped the parked call"
            harness.send(_tools_call_named(2, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            assert not any(
                r[0] == 1 for r in harness.responses
            ), "parked call answered before it settled"
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
            assert _text(harness, 1) == "slept"
            assert _text(harness, 2) == "done:fast"
        finally:
            release.set()
            harness.close()

    def test_many_parked_calls_hold_no_worker_and_all_settle(self, monkeypatch):
        """Nine parked calls -- more than any fixed worker pool would carry --
        are all alive at once, and a tenth ordinary call still runs."""
        release = threading.Event()
        parked = [_Parked(f"slept-{i}", release=release) for i in range(9)]

        def call_tool(name, args):
            if name.startswith("wait-"):
                return parked[int(name.split("-")[1])]
            return f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            for i in range(9):
                harness.send(_tools_call_named(100 + i, f"wait-{i}"))
            assert harness.wait_for(lambda: all(p.steps >= 1 for p in parked))
            harness.send(_tools_call_named(200, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 200 for r in harness.responses))
            assert [r[0] for r in harness.responses] == [200]
            release.set()
            assert harness.wait_for(lambda: len(harness.responses) == 10)
            assert sorted(r[0] for r in harness.responses) == [*range(100, 109), 200]
        finally:
            release.set()
            harness.close()

    def test_a_step_blocked_on_the_gateway_does_not_block_the_loop(self, monkeypatch):
        """Steps run on a pool, not the dispatch thread: a step stuck in its
        HTTP call leaves pings answered and other calls served."""
        release = threading.Event()
        hold_step = threading.Event()
        parked = _Parked("slept", release=release, hold_step=hold_step)

        def call_tool(name, args):
            return parked if name == "wait" else f"done:{name}"

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: parked.steps >= 1)
            harness.send(_ping(7))
            harness.send(_tools_call_named(2, "fast"))
            assert harness.wait_for(lambda: {r[0] for r in harness.responses} >= {7, 2})
            release.set()
            hold_step.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        finally:
            release.set()
            hold_step.set()
            harness.close()


class TestDeferredAdmission:
    @staticmethod
    def _parked_ids(monkeypatch) -> list[set]:
        """Every in-flight report's ids: the loop names each call it holds in
        its parked table, so these are the table's contents tick by tick."""
        reports: list[set] = []
        monkeypatch.setattr(mcp_shared, "INFLIGHT_REPORT_SECS", 0.0)
        monkeypatch.setattr(
            mcp_shared,
            "notify",
            lambda m, p: (
                reports.append(set(p["requestIds"])) if m == INFLIGHT_NOTIFICATION else None
            ),
        )
        return reports

    def test_parked_overflow_is_cancelled_audited_and_never_retained(self, monkeypatch):
        """A full table refuses only the overflow; admitted calls still settle."""
        monkeypatch.setattr(mcp_shared, "DEFERRED_PARKED_MAX", 2)
        reports = self._parked_ids(monkeypatch)
        release = threading.Event()
        parked = [_IdentityParked(f"slept-{i}", release=release) for i in range(3)]
        harness = _LoopHarness(
            monkeypatch,
            lambda n, a: parked[int(n.split("-")[1])] if n.startswith("wait-") else "ok",
        )
        dispatch_contexts: list = []

        def respond(req_id, result, error=None):
            dispatch_contexts.append((current_caller(), current_tenant_nonce()))
            harness._record(req_id, result, error)

        monkeypatch.setattr(mcp_shared, "respond", respond)
        try:
            harness.send(_initialize_asking_for_inflight(0))
            for i in range(3):
                harness.send(_identity_call(i + 1, f"wait-{i}", f"dashboard:call-{i}", f"n-{i}"))
            assert harness.wait_for(lambda: any(r[0] == 3 for r in harness.responses))
            overflow = next(r for r in harness.responses if r[0] == 3)
            assert overflow[1] is None and overflow[2] == {
                "code": -32000,
                "message": "Server busy: parked-call table is full; retry",
            }
            assert len(_audits(harness, 3, "failed")) == 1
            assert _audits(harness, 3, "failed")[0].kwargs["session_key"] == "dashboard:call-2"
            assert parked[2].cancelled and parked[2].steps == 0 and parked[2].settled == []
            caller, nonce = parked[2].cancel_identity[0]
            assert caller.session_key == "dashboard:call-2" and nonce == "n-2"
            assert harness.wait_for(lambda: all(p.steps >= 1 for p in parked[:2]))
            assert harness.wait_for(lambda: {1, 2} in reports)
            # Only the two admitted calls were ever parked: the overflow never
            # entered the table, so it cannot be retained past its refusal.
            assert all(ids <= {1, 2} for ids in list(reports))
            assert not any(p.cancelled for p in parked[:2])
            harness.send(_tools_call_named(4, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 4 for r in harness.responses))
            release.set()
            assert harness.wait_for(lambda: {1, 2} <= {r[0] for r in harness.responses})
            assert _text(harness, 1) == "slept-0" and _text(harness, 2) == "slept-1"
            # Settling frees capacity for a retry of the refused tool.
            harness.send(_tools_call_named(5, "wait-2"))
            assert harness.wait_for(lambda: any(r[0] == 5 for r in harness.responses))
            assert _text(harness, 5) == "slept-2"
            assert all(ctx == (None, "") for ctx in dispatch_contexts)
        finally:
            release.set()
            harness.close()

    def test_an_overflow_that_already_acted_answers_its_abandon_text(self, monkeypatch):
        """A refused call whose children already run (a spawn) is answered with
        its ``abandon`` text, settled once and never audited failed or
        cancelled, so the caller does not retry and spawn the batch again; one
        whose hook returns None still gets the retryable busy error."""
        monkeypatch.setattr(mcp_shared, "DEFERRED_PARKED_MAX", 1)
        reports = self._parked_ids(monkeypatch)
        release = threading.Event()
        holder = _Parked("slept", release=release)
        spawned = _Abandoning("spawned: a1, a2; do not spawn them again")
        retryable = _Abandoning(None)
        calls = {"hold": holder, "spawn": spawned, "retryable": retryable}
        harness = _LoopHarness(monkeypatch, lambda n, a: calls.get(n, "ok"))
        try:
            harness.send(_initialize_asking_for_inflight(0))
            harness.send(_tools_call_named(1, "hold"))
            harness.send(_identity_call(2, "spawn", "dashboard:spawner", "spawn-nonce"))
            harness.send(_tools_call_named(3, "retryable"))
            assert harness.wait_for(lambda: {2, 3} <= {r[0] for r in harness.responses})
            answered = {r[0]: r for r in harness.responses}
            assert answered[2][2] is None
            assert _text(harness, 2) == "spawned: a1, a2; do not spawn them again"
            assert spawned.settled == [_text(harness, 2)] and not spawned.cancelled
            assert _audits(harness, 2, "failed") == [] and _audits(harness, 2, "cancelled") == []
            caller, nonce = spawned.abandon_identity[0]
            assert caller.session_key == "dashboard:spawner" and nonce == "spawn-nonce"
            assert answered[3][1] is None and answered[3][2] == {
                "code": -32000,
                "message": "Server busy: parked-call table is full; retry",
            }
            assert retryable.cancelled and retryable.settled == []
            assert len(_audits(harness, 3, "failed")) == 1
            assert harness.wait_for(lambda: {1} in reports)
            assert all(ids <= {1} for ids in list(reports))
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
            assert _text(harness, 1) == "slept"
        finally:
            release.set()
            harness.close()


class TestDeferredToolCancellation:
    @pytest.mark.parametrize("phase", ["idle", "step", "step-error", "worker"])
    @pytest.mark.parametrize("cancel_raises", [False, True])
    def test_cancel_uses_its_own_identity_and_clears_it(self, monkeypatch, phase, cancel_raises):
        """Notification and late-worker cleanup keep identity even when cleanup raises."""
        hold = threading.Event()
        started = threading.Event()

        class Identified(_IdentityParked):
            def due_at(self) -> float:
                return float("inf") if phase in ("idle", "worker") else 0.0

            def step(self) -> str | None:
                started.set()
                assert hold.wait(timeout=5.0), "the test did not release the step"
                if phase == "step-error":
                    raise RuntimeError("slice failed")
                return "answer"

        parked = Identified("never", release=threading.Event(), cancel_raises=cancel_raises)
        retained = threading.Event()
        entry_type = mcp_shared._DeferredEntry

        def record_entry(*args):
            entry = entry_type(*args)
            retained.set()
            return entry

        monkeypatch.setattr(mcp_shared, "_DeferredEntry", record_entry)

        def call_tool(name, args):
            if name != "wait":
                return "ok"
            if phase == "worker":
                started.set()
                assert hold.wait(timeout=5.0), "the test did not release the worker"
            return parked

        harness = _LoopHarness(monkeypatch, call_tool)
        dispatch_contexts: list = []
        audit_contexts: list = []
        harness.sel_mock.log_tool_invocation.side_effect = lambda **kw: audit_contexts.append(
            (kw["outcome"], current_caller(), current_tenant_nonce())
        )

        def respond(req_id, result, error=None):
            dispatch_contexts.append((current_caller(), current_tenant_nonce()))
            harness._record(req_id, result, error)

        monkeypatch.setattr(mcp_shared, "respond", respond)
        try:
            harness.send(_identity_call(1, "wait", "dashboard:owner", "owner-nonce"))
            barrier = retained if phase == "idle" else started
            assert barrier.wait(timeout=5.0), "the cancellation phase was not reached"
            harness.send(_identity_call(2, "fast", "dashboard:other", "other-nonce"))
            harness.send(_cancel(1))
            harness.send(_ping(9))
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            hold.set()
            assert harness.wait_for(lambda: len(_audits(harness, 1, "cancelled")) == 1)
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            assert len(parked.cancel_identity) == 1
            caller, nonce = parked.cancel_identity[0]
            assert caller.session_key == "dashboard:owner" and nonce == "owner-nonce"
            assert _audits(harness, 1, "cancelled")[0].kwargs["session_key"] == "dashboard:owner"
            assert audit_contexts == [("cancelled", None, "")]
            assert not any(r[0] == 1 for r in harness.responses)
            harness.send(_ping(10))
            assert harness.wait_for(lambda: any(r[0] == 10 for r in harness.responses))
            assert all(ctx == (None, "") for ctx in dispatch_contexts)
        finally:
            hold.set()
            harness.close()

    def test_cancel_drops_a_parked_call_with_no_response(self, monkeypatch):
        release = threading.Event()
        parked = _Parked("slept", release=release)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: parked.steps >= 1)
            harness.send(_cancel(1))
            # The cancel audit is the observable end of the cancelled call.
            assert harness.wait_for(lambda: len(_audits(harness, 1, "cancelled")) == 1)
            assert parked.cancelled
            steps_at_cancel = parked.steps
            release.set()
            # A later call runs through the same loop, so by the time it is
            # answered every earlier frame -- the cancel included -- was
            # consumed; the cancelled call is never answered and never stepped
            # again (one more step is allowed: the one that was in flight).
            harness.send(_tools_call_named(2, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            assert not any(r[0] == 1 for r in harness.responses)
            assert parked.steps <= steps_at_cancel + 1
            assert len(_audits(harness, 1, "cancelled")) == 1
            assert _audits(harness, 1, "completed") == []
        finally:
            release.set()
            harness.close()

    def test_cancel_during_a_step_drops_that_steps_result(self, monkeypatch):
        """A cancel that lands while a step runs: the step's text is discarded,
        the cancel hook runs, the request gets no response, and exactly ONE
        audit row (``cancelled``) is written -- never a ``completed`` beside it."""
        release = threading.Event()
        hold_step = threading.Event()
        parked = _Parked("slept", release=release, hold_step=hold_step)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: parked.steps >= 1)
            release.set()  # the step in flight WILL return text once unblocked
            harness.send(_cancel(1))
            # A ping answered after the cancel proves the loop consumed the
            # cancel (frames are processed in order) while the step still holds.
            harness.send(_ping(9))
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            hold_step.set()
            assert harness.wait_for(lambda: len(_audits(harness, 1, "cancelled")) == 1)
            assert parked.cancelled
            harness.send(_tools_call_named(2, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            assert not any(r[0] == 1 for r in harness.responses)
            assert _audits(harness, 1, "completed") == []
            assert parked.settled == []
        finally:
            release.set()
            hold_step.set()
            harness.close()

    def test_a_step_raising_tool_cancelled_ends_the_call_silently(self, monkeypatch):
        class _Quits(DeferredTool):
            def due_at(self) -> float:
                return 0.0

            def step(self) -> str | None:
                raise ToolCancelled("done waiting")

        harness = _LoopHarness(monkeypatch, lambda n, a: _Quits() if n == "wait" else "ok")
        try:
            harness.send(_tools_call_named(1, "wait"))
            harness.send(_tools_call_named(2, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            assert harness.wait_for(lambda: len(_audits(harness, 1, "cancelled")) == 1)
            assert not any(r[0] == 1 for r in harness.responses)
        finally:
            harness.close()

    def test_a_step_that_raises_answers_an_error(self, monkeypatch):
        class _Breaks(DeferredTool):
            def due_at(self) -> float:
                return 0.0

            def step(self) -> str | None:
                raise RuntimeError("gateway refused")

        harness = _LoopHarness(monkeypatch, lambda n, a: _Breaks())
        try:
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
            text = _text(harness, 1)
            assert text.startswith("Error:") and "gateway refused" in text
            assert len(_audits(harness, 1, "failed")) == 1
            # The loop keeps serving.
            harness.send(_tools_call_named(2, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
        finally:
            harness.close()


class TestLoopSurvivesStepFailures:
    def test_a_pool_that_cannot_start_fails_only_that_call(self, monkeypatch):
        """Not one thread for the step pool (the scope's task ceiling): the call
        that needed it gets an error, every other parked call and the loop live
        on, and the pool is tried again on the next tick."""
        release = threading.Event()
        parked = {"wait-a": _Parked("a", release=release), "wait-b": _Parked("b", release=release)}
        real_pool = mcp_shared._StepPool
        attempts: list = []

        class _StarvedOnce(real_pool):  # type: ignore[misc,valid-type]
            def __init__(self, workers: int, name: str) -> None:
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("can't start new thread")
                super().__init__(workers, name)

        monkeypatch.setattr(mcp_shared, "_StepPool", _StarvedOnce)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked.get(n, "ok"))
        try:
            harness.send(_tools_call_named(1, "wait-a"))
            harness.send(_tools_call_named(2, "wait-b"))
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
            text = _text(harness, 1)
            assert text.startswith("Error:") and "can't start new thread" in text
            assert len(_audits(harness, 1, "failed")) == 1
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            assert _text(harness, 2) == "b"
            assert len(attempts) == 2, "the pool was not retried after the failed start"
            harness.send(_tools_call_named(3, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 3 for r in harness.responses))
        finally:
            release.set()
            harness.close()

    def test_a_pool_that_cannot_start_answers_a_call_that_acted_with_its_abandon_text(
        self, monkeypatch
    ):
        """The pool failure answers a spawn whose children already run with its
        ``abandon`` text, not an error the caller would retry, and drops the
        call: once the pool starts, it is never stepped."""
        release = threading.Event()
        spawned = _Abandoning("spawned: a1; do not spawn it again", due=0.0)
        parked = {"spawn": spawned, "wait-b": _Parked("b", release=release)}
        real_pool = mcp_shared._StepPool
        attempts: list = []

        class _StarvedOnce(real_pool):  # type: ignore[misc,valid-type]
            def __init__(self, workers: int, name: str) -> None:
                attempts.append(1)
                if len(attempts) == 1:
                    raise RuntimeError("can't start new thread")
                super().__init__(workers, name)

        monkeypatch.setattr(mcp_shared, "_StepPool", _StarvedOnce)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked.get(n, "ok"))
        try:
            harness.send(_identity_call(1, "spawn", "dashboard:spawner", "spawn-nonce"))
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
            assert _text(harness, 1) == "spawned: a1; do not spawn it again"
            assert spawned.settled == [_text(harness, 1)] and not spawned.cancelled
            assert _audits(harness, 1, "failed") == []
            caller, nonce = spawned.abandon_identity[0]
            assert caller.session_key == "dashboard:spawner" and nonce == "spawn-nonce"
            harness.send(_tools_call_named(2, "wait-b"))
            assert harness.wait_for(lambda: parked["wait-b"].steps >= 1)
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            assert len(attempts) == 2
            # Removed from the table on its answer: the started pool never stepped it.
            assert spawned.steps == 0
            assert [r[0] for r in harness.responses].count(1) == 1
        finally:
            release.set()
            harness.close()

    """``_StepPool`` exists because ``ThreadPoolExecutor.submit`` queues the work
    item BEFORE starting a thread: a start that fails there raises out of
    ``submit`` while an idle worker runs the orphan later -- for a deferred step,
    a call just answered with an error consuming an answer or a collect. The
    pool below starts every thread up front, so ``submit`` only enqueues."""

    def test_submit_never_starts_a_thread_so_it_cannot_fail_after_queueing(self, monkeypatch):
        pool = mcp_shared._StepPool(2, "t")
        try:
            assert pool.workers == 2

            def _no_more_threads(self):
                raise RuntimeError("can't start new thread")

            with monkeypatch.context() as mp:
                mp.setattr(threading.Thread, "start", _no_more_threads)
                fut = pool.submit(lambda x: x * 2, 21)  # must not raise
                assert fut.result(timeout=5.0) == 42
        finally:
            pool.shutdown()

    def test_a_pool_that_cannot_start_every_thread_releases_the_rest_and_fails(self, monkeypatch):
        """``DEFERRED_PARKED_MAX`` admits calls assuming ``workers`` threads; a
        pool that came up with two of four would queue an ask's 2s poll behind
        every other parked step, past the coordinator's answer grace. So a start
        that fails partway is a failed pool, not a slower one: the threads that
        did start exit, and construction raises into the caller's refusal path."""
        real_start = threading.Thread.start
        started: list[threading.Thread] = []

        def _third_start_fails(self):
            if len(started) == 2:
                raise RuntimeError("can't start new thread")
            started.append(self)
            real_start(self)

        with monkeypatch.context() as mp:
            mp.setattr(threading.Thread, "start", _third_start_fails)
            with pytest.raises(RuntimeError, match="can't start new thread"):
                mcp_shared._StepPool(4, "t")
        for th in started:
            th.join(timeout=5.0)
        assert len(started) == 2
        assert not any(th.is_alive() for th in started), "started threads were not released"

        def _every_start_fails(self):
            raise RuntimeError("can't start new thread")

        with monkeypatch.context() as mp:
            mp.setattr(threading.Thread, "start", _every_start_fails)
            with pytest.raises(RuntimeError):
                mcp_shared._StepPool(2, "t")

    def test_an_exception_in_a_step_is_delivered_through_its_future(self):
        pool = mcp_shared._StepPool(1, "t")
        try:

            def _boom():
                raise ValueError("step failed")

            fut = pool.submit(_boom)
            with pytest.raises(ValueError, match="step failed"):
                fut.result(timeout=5.0)
        finally:
            pool.shutdown()


class TestPrunedExitDeliversRunningSteps:
    def test_a_step_in_flight_is_finished_and_delivered_not_refused(self, monkeypatch):
        """The install is pruned while a step runs: its result is delivered (the
        step may have consumed something unrepeatable -- an answer, a collect)
        and only a call with no step in flight gets the retry error."""
        release = threading.Event()
        hold_step = threading.Event()
        running = _Parked("answered", release=release, hold_step=hold_step)
        idle = _IdentityParked("never", release=threading.Event())
        idle.due_at = lambda: float("inf")  # type: ignore[method-assign]  # parked, never stepped
        pruned = [False]
        monkeypatch.setattr(mcp_shared, "install_pruned", lambda: pruned[0])
        monkeypatch.setattr(mcp_shared, "respawned_by_pool", lambda: True)
        monkeypatch.setattr(mcp_shared.sys, "exit", lambda code=0: None)

        harness = _LoopHarness(
            monkeypatch, lambda n, a: {"running": running, "idle": idle}.get(n, "ok")
        )
        dispatch_contexts: list = []

        def respond(req_id, result, error=None):
            dispatch_contexts.append((current_caller(), current_tenant_nonce()))
            harness._record(req_id, result, error)

        monkeypatch.setattr(mcp_shared, "respond", respond)
        try:
            harness.send(_tools_call_named(1, "running"))
            harness.send(_identity_call(2, "idle", "dashboard:idle", "idle-nonce"))
            # A ping answered after both calls proves the loop consumed them
            # (frames are processed in order), so the prune below is seen by a
            # LATER call, not by call 2 itself.
            harness.send(_ping(9))
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            assert harness.wait_for(lambda: running.steps >= 1)
            release.set()  # the held step returns text once released
            pruned[0] = True
            harness.send(_tools_call_named(3, "fast"))  # trips the pruned-install exit
            assert harness.wait_for(lambda: any(r[0] == 3 for r in harness.responses))
            # The exit path is now waiting on the running step; let it finish.
            hold_step.set()
            harness._thread.join(timeout=5.0)
            assert not harness._thread.is_alive(), "the loop did not exit on the pruned install"
            answered = {r[0]: r for r in harness.responses}
            assert (
                answered[1][1]["content"][0]["text"] == "answered"
            ), "the finished step was dropped"
            assert answered[2][1] is None and answered[2][2]["code"] == -32000
            assert "retry" in answered[2][2]["message"]
            assert not running.cancelled and idle.cancelled
            assert len(idle.cancel_identity) == 1
            caller, nonce = idle.cancel_identity[0]
            assert caller.session_key == "dashboard:idle" and nonce == "idle-nonce"
            assert all(ctx == (None, "") for ctx in dispatch_contexts)
            assert len(_audits(harness, 2, "rejected_install_pruned")) == 1
            assert (
                _audits(harness, 2, "rejected_install_pruned")[0].kwargs["session_key"]
                == "dashboard:idle"
            )
        finally:
            release.set()
            hold_step.set()
            harness.close()

    def test_a_step_still_queued_at_pruned_exit_never_runs(self, monkeypatch):
        """One worker, held by a stuck step past the bound; a second call's step
        is QUEUED behind it. The pruned exit refuses the queued call with the
        retry error -- and must cancel its future, or the worker would run the
        step after the sentinel-less queue drains, consuming an answer or a
        collect for a caller already told to retry."""
        monkeypatch.setattr(mcp_shared, "DEFERRED_STEP_WORKERS", 1)
        monkeypatch.setattr(
            mcp_shared, "DEFERRED_STEP_STUCK_SECS", 0.0
        )  # no wait for the stuck one
        hold_step = threading.Event()
        stuck = _Parked("stuck", release=threading.Event(), hold_step=hold_step)
        queued = _Parked("queued", release=threading.Event())
        pruned = [False]
        monkeypatch.setattr(mcp_shared, "install_pruned", lambda: pruned[0])
        monkeypatch.setattr(mcp_shared, "respawned_by_pool", lambda: True)
        monkeypatch.setattr(mcp_shared.sys, "exit", lambda code=0: None)
        harness = _LoopHarness(
            monkeypatch, lambda n, a: {"stuck": stuck, "queued": queued}.get(n, "ok")
        )
        try:
            harness.send(_tools_call_named(1, "stuck"))
            assert harness.wait_for(lambda: stuck.steps >= 1)  # the one worker is now held
            harness.send(_tools_call_named(2, "queued"))
            harness.send(_ping(9))
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            # Two more ticks have passed by the time a second ping is answered,
            # so call 2's step has been submitted (and sits queued).
            harness.send(_ping(10))
            assert harness.wait_for(lambda: any(r[0] == 10 for r in harness.responses))
            pruned[0] = True
            harness.send(_tools_call_named(3, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 3 for r in harness.responses))
            harness._thread.join(timeout=5.0)
            assert not harness._thread.is_alive()
            answered = {r[0]: r for r in harness.responses}
            assert answered[2][2]["code"] == -32000 and "retry" in answered[2][2]["message"]
            # Release the worker: it drains the queue, finds the cancelled item
            # and the shutdown sentinel, and exits without running the step.
            hold_step.set()
            assert harness.wait_for(
                lambda: not any(
                    t.name.startswith("test-server-deferred") for t in threading.enumerate()
                )
            )
            assert queued.steps == 0, "a refused call's queued step ran after the exit"
            assert queued.cancelled
        finally:
            hold_step.set()
            harness.close()

    def test_a_step_a_worker_claims_as_it_is_cancelled_is_delivered_not_refused(self, monkeypatch):
        """``Future.cancel`` can lose to a worker that claims the item between
        the ``done()`` check and the cancel. That step IS running and may
        consume something a retry would repeat, so the exit must treat it as
        running -- wait for it and deliver -- not refuse it. The loss is
        forced: the queued future's ``cancel`` first lets the one worker free
        up and claim the step, then runs the real cancel, which returns False.
        """
        monkeypatch.setattr(mcp_shared, "DEFERRED_STEP_WORKERS", 1)
        monkeypatch.setattr(mcp_shared, "DEFERRED_STEP_STUCK_SECS", 2.0)
        hold_stuck = threading.Event()
        hold_claimed = threading.Event()
        claimed = threading.Event()
        stuck = _Parked("stuck", release=threading.Event(), hold_step=hold_stuck)

        class _Claimable(_Parked):
            def step(self) -> str | None:
                claimed.set()
                hold_claimed.wait(timeout=5.0)
                return super().step()

        released = threading.Event()
        released.set()
        queued = _Claimable("answered", release=released)
        futures: dict = {}
        real_submit = mcp_shared._StepPool.submit

        def _recording_submit(self, fn, *args):
            fut = real_submit(self, fn, *args)
            futures[args[0].req_id] = fut
            return fut

        monkeypatch.setattr(mcp_shared._StepPool, "submit", _recording_submit)
        pruned = [False]
        monkeypatch.setattr(mcp_shared, "install_pruned", lambda: pruned[0])
        monkeypatch.setattr(mcp_shared, "respawned_by_pool", lambda: True)
        monkeypatch.setattr(mcp_shared.sys, "exit", lambda code=0: None)
        harness = _LoopHarness(
            monkeypatch, lambda n, a: {"stuck": stuck, "queued": queued}.get(n, "ok")
        )
        try:
            harness.send(_tools_call_named(1, "stuck"))
            assert harness.wait_for(lambda: stuck.steps >= 1)
            harness.send(_tools_call_named(2, "queued"))
            harness.send(_ping(9))
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            harness.send(_ping(10))
            assert harness.wait_for(lambda: any(r[0] == 10 for r in harness.responses))
            assert harness.wait_for(lambda: 2 in futures), "the queued step was not submitted"
            real_cancel = futures[2].cancel

            def _losing_cancel() -> bool:
                hold_stuck.set()  # the worker finishes the stuck step and claims this one
                assert claimed.wait(timeout=5.0)
                hold_claimed.set()  # and finishes it promptly
                return real_cancel()  # False: already running

            futures[2].cancel = _losing_cancel  # type: ignore[method-assign]
            pruned[0] = True
            harness.send(_tools_call_named(3, "fast"))
            harness._thread.join(timeout=15.0)
            assert not harness._thread.is_alive()
            answered = {r[0]: r for r in harness.responses}
            assert (
                answered[2][1]["content"][0]["text"] == "answered"
            ), "a step a worker was running was refused instead of delivered"
            assert queued.steps == 1 and not queued.cancelled
        finally:
            hold_stuck.set()
            hold_claimed.set()
            harness.close()

    def test_an_exceptional_loop_exit_cancels_queued_steps(self, monkeypatch):
        """Whatever ends the loop, a step still queued must not run afterwards
        for a client that will never see the answer: the ``finally`` cancels
        every pending future before the pool's shutdown sentinels queue."""
        monkeypatch.setattr(mcp_shared, "DEFERRED_STEP_WORKERS", 1)
        hold_stuck = threading.Event()
        stuck = _Parked("stuck", release=threading.Event(), hold_step=hold_stuck)
        queued = _Parked("queued", release=threading.Event())
        harness = _LoopHarness(
            monkeypatch, lambda n, a: {"stuck": stuck, "queued": queued}.get(n, "ok")
        )
        try:
            harness.send(_tools_call_named(1, "stuck"))
            assert harness.wait_for(lambda: stuck.steps >= 1)
            harness.send(_tools_call_named(2, "queued"))
            harness.send(_ping(9))
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            harness.send(_ping(10))
            assert harness.wait_for(lambda: any(r[0] == 10 for r in harness.responses))

            def _reader_breaks(stdin):
                raise RuntimeError("stdin exploded")

            # The next tick's read raises OUTSIDE every per-message guard.
            monkeypatch.setattr(mcp_shared, "_stdin_holds_a_line", _reader_breaks)
            harness._thread.join(timeout=5.0)
            assert not harness._thread.is_alive(), "the loop survived a reader failure?"
            hold_stuck.set()
            assert harness.wait_for(
                lambda: not any(
                    t.name.startswith("test-server-deferred") for t in threading.enumerate()
                )
            )
            assert queued.steps == 0, "a queued step ran after the loop died"
        finally:
            hold_stuck.set()
            with contextlib_suppress():
                harness.close()


class _Abandoning(_Parked):
    """Parked and never stepped; its ``abandon`` answers, returns None, or raises."""

    def __init__(
        self, abandon_text: str | None, *, raises: bool = False, due: float = float("inf")
    ):
        super().__init__("never", release=threading.Event())
        self.abandon_text = abandon_text
        self.raises = raises
        self.due = due
        self.abandon_identity: list = []
        self.settled: list[str] = []

    def due_at(self) -> float:
        return self.due

    def abandon(self) -> str | None:
        self.abandon_identity.append((current_caller(), current_tenant_nonce()))
        if self.raises:
            raise RuntimeError("abandon failed")
        return self.abandon_text

    def on_settled(self, text: str) -> None:
        self.settled.append(text)


class TestPrunedExitAbandonsParkedCalls:
    def test_abandon_text_answers_the_call_and_none_or_a_raise_gets_the_retry_error(
        self, monkeypatch
    ):
        """A parked call that already did something a retry would repeat (a
        spawn whose children run) answers with its ``abandon`` text, settled
        through ``on_settled`` and never audited as refused; a call whose hook
        returns None, or raises, gets the retryable error and its cancel hook."""
        answers = _Abandoning("spawned: a1, a2; do not spawn them again")
        retryable = _Abandoning(None)
        broken = _Abandoning("unused", raises=True)
        calls = {"answers": answers, "retryable": retryable, "broken": broken}
        pruned = [False]
        monkeypatch.setattr(mcp_shared, "install_pruned", lambda: pruned[0])
        monkeypatch.setattr(mcp_shared, "respawned_by_pool", lambda: True)
        monkeypatch.setattr(mcp_shared.sys, "exit", lambda code=0: None)
        harness = _LoopHarness(monkeypatch, lambda n, a: calls.get(n, "ok"))
        try:
            harness.send(_identity_call(1, "answers", "dashboard:spawner", "spawn-nonce"))
            harness.send(_tools_call_named(2, "retryable"))
            harness.send(_tools_call_named(4, "broken"))
            harness.send(_ping(9))
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            pruned[0] = True
            harness.send(_tools_call_named(3, "fast"))
            harness._thread.join(timeout=5.0)
            assert not harness._thread.is_alive()
            answered = {r[0]: r for r in harness.responses}
            assert answered[1][2] is None
            assert _text(harness, 1) == "spawned: a1, a2; do not spawn them again"
            assert answers.settled == [_text(harness, 1)] and not answers.cancelled
            assert _audits(harness, 1, "rejected_install_pruned") == []
            caller, nonce = answers.abandon_identity[0]
            assert caller.session_key == "dashboard:spawner" and nonce == "spawn-nonce"
            for rid, deferred in ((2, retryable), (4, broken)):
                assert answered[rid][1] is None and answered[rid][2]["code"] == -32000
                assert "retry" in answered[rid][2]["message"]
                assert deferred.cancelled and deferred.settled == []
                assert len(_audits(harness, rid, "rejected_install_pruned")) == 1
        finally:
            with contextlib_suppress():
                harness.close()


class TestLoopFailuresStayLocal:
    def test_a_failing_inflight_report_does_not_end_the_loop(self, monkeypatch):
        def _broken_notify(method, params):
            raise OSError("stdout gone")

        monkeypatch.setattr(mcp_shared, "notify", _broken_notify)
        monkeypatch.setattr(mcp_shared, "INFLIGHT_REPORT_SECS", 0.0)
        release = threading.Event()
        parked = _Parked("slept", release=release)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_initialize_asking_for_inflight(0))
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: parked.steps >= 3)  # several report attempts
            harness.send(_tools_call_named(2, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        finally:
            release.set()
            harness.close()

    def test_a_failing_on_settled_still_answers_the_call(self, monkeypatch):
        release = threading.Event()
        release.set()
        committed: list = []
        contexts: list = []

        class _CommitFails(_Parked):
            def on_settled(self, text: str) -> None:
                committed.append((current_caller(), current_tenant_nonce()))
                raise RuntimeError("audit store unwritable")

        parked = _CommitFails("slept", release=release)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")

        def respond(req_id, result, error=None):
            contexts.append((current_caller(), current_tenant_nonce()))
            harness._record(req_id, result, error)

        monkeypatch.setattr(mcp_shared, "respond", respond)
        try:
            harness.send(_identity_call(1, "wait", "dashboard:owner", "owner-nonce"))
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
            assert _text(harness, 1) == "slept"
            caller, nonce = committed[0]
            assert caller.session_key == "dashboard:owner" and nonce == "owner-nonce"
            assert contexts == [(None, "")]
            harness.send(_tools_call_named(2, "fast"))
            assert harness.wait_for(lambda: any(r[0] == 2 for r in harness.responses))
        finally:
            harness.close()


class TestInflightReport:
    def _notifications(self, monkeypatch) -> list[tuple[str, dict]]:
        sent: list[tuple[str, dict]] = []
        monkeypatch.setattr(mcp_shared, "notify", lambda m, p: sent.append((m, p)))
        return sent

    def test_parked_ids_are_reported_to_a_client_that_asked(self, monkeypatch):
        sent = self._notifications(monkeypatch)
        monkeypatch.setattr(mcp_shared, "INFLIGHT_REPORT_SECS", 0.0)
        release = threading.Event()
        parked = _Parked("slept", release=release)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_initialize_asking_for_inflight(0))
            assert harness.wait_for(lambda: any(r[0] == 0 for r in harness.responses))
            caps = harness.responses[0][1]["capabilities"]["experimental"]
            assert INFLIGHT_CAPABILITY_KEY in caps, "the loop must advertise what it sends"
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: any(m == INFLIGHT_NOTIFICATION for m, _ in sent))
            method, params = next(x for x in sent if x[0] == INFLIGHT_NOTIFICATION)
            assert params == {"requestIds": [1]}
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        finally:
            release.set()
            harness.close()

    def test_no_report_to_a_client_that_did_not_ask(self, monkeypatch):
        """A direct kiro-cli or an older gateway never declared the capability:
        an unsolicited frame would be dropped as unattributable at best."""
        sent = self._notifications(monkeypatch)
        monkeypatch.setattr(mcp_shared, "INFLIGHT_REPORT_SECS", 0.0)
        release = threading.Event()
        parked = _Parked("slept", release=release)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_initialize(0))
            harness.send(_tools_call_named(1, "wait"))
            # Three ticks of the loop with a parked call, each a report
            # opportunity; none produced one.
            assert harness.wait_for(lambda: parked.steps >= 3)
            assert sent == []
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        finally:
            release.set()
            harness.close()

    def test_a_server_that_did_not_opt_in_neither_advertises_nor_reports(self, monkeypatch):
        """Only ``kirocrew-core`` opts in: a server on the same loop that did not
        must not lower its gateway's recycle ceiling, so a client that declared
        the capability still gets no advertisement and no report from it."""
        sent = self._notifications(monkeypatch)
        monkeypatch.setattr(mcp_shared, "INFLIGHT_REPORT_SECS", 0.0)
        release = threading.Event()
        parked = _Parked("slept", release=release)
        harness = _PlainLoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_initialize_asking_for_inflight(0))
            assert harness.wait_for(lambda: any(r[0] == 0 for r in harness.responses))
            assert "experimental" not in harness.responses[0][1]["capabilities"]
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: parked.steps >= 3)
            assert sent == []
            release.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        finally:
            release.set()
            harness.close()

    def test_a_call_stuck_inside_a_step_is_not_reported_as_progressing(self, monkeypatch):
        """The report is the gateway's evidence that a call is healthy, so a step
        that has held its thread past the stuck bound is left out."""
        sent = self._notifications(monkeypatch)
        monkeypatch.setattr(mcp_shared, "INFLIGHT_REPORT_SECS", 0.0)
        # A negative bound makes ANY step in flight count as stuck, so the
        # assertion needs no wall-clock wait.
        monkeypatch.setattr(mcp_shared, "DEFERRED_STEP_STUCK_SECS", -1.0)
        release = threading.Event()
        hold_step = threading.Event()
        parked = _Parked("slept", release=release, hold_step=hold_step)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_initialize_asking_for_inflight(0))
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: parked.steps >= 1)
            assert harness.wait_for(lambda: any(m == INFLIGHT_NOTIFICATION for m, _ in sent))
            assert all(p == {"requestIds": []} for m, p in sent if m == INFLIGHT_NOTIFICATION)
            release.set()
            hold_step.set()
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        finally:
            release.set()
            hold_step.set()
            harness.close()

    def test_a_queued_call_is_reported_until_it_starts_running(self, monkeypatch):
        """A call waiting behind the busy worker is named (its age is the
        queue's), and stops being named once it runs: the gateway's ceiling
        then bounds how long that one call runs."""
        sent = self._notifications(monkeypatch)
        monkeypatch.setattr(mcp_shared, "INFLIGHT_REPORT_SECS", 0.0)
        started = {n: threading.Event() for n in ("a", "b", "c")}
        release = {n: threading.Event() for n in ("a", "b", "c")}

        def call_tool(name, args):
            started[name].set()
            release[name].wait(timeout=5.0)
            return f"done:{name}"

        def reported(since: int = 0) -> list[list[Any]]:
            return [p["requestIds"] for m, p in sent[since:] if m == INFLIGHT_NOTIFICATION]

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_initialize_asking_for_inflight(0))
            harness.send(_tools_call_named(1, "a"))
            assert started["a"].wait(timeout=5.0)
            harness.send(_tools_call_named(2, "b"))
            assert harness.wait_for(lambda: any(2 in ids for ids in reported()))
            assert all(1 not in ids for ids in reported()), "the running call was named"

            harness.send(_tools_call_named(3, "c"))
            release["a"].set()
            assert started["b"].wait(timeout=5.0)
            mark = len(sent)
            assert harness.wait_for(lambda: any(3 in ids for ids in reported(mark)))
            assert all(2 not in ids for ids in reported(mark)), "a running call was still named"

            release["b"].set()
            release["c"].set()
            assert harness.wait_for(lambda: {1, 2, 3} <= {r[0] for r in harness.responses})
        finally:
            for evt in release.values():
                evt.set()
            harness.close()

    def test_a_queue_behind_a_cancelled_hung_call_is_not_vouched_for(self, monkeypatch):
        """The gateway drops a cancelled request from its table, so a worker hung
        on a cancelled call has no slot left to age past the ceiling -- and the
        loop still answers pings. If the queued calls behind it kept being named
        they would never age either, and the backend would stay wedged for every
        co-tenant. Once the running call is cancelled the queue is not named."""
        sent = self._notifications(monkeypatch)
        monkeypatch.setattr(mcp_shared, "INFLIGHT_REPORT_SECS", 0.0)
        started = threading.Event()
        release = threading.Event()

        def call_tool(name, args):
            started.set()
            release.wait(timeout=10.0)  # a hung synchronous tool
            return f"done:{name}"

        def reported(since: int = 0) -> list[list[Any]]:
            return [p["requestIds"] for m, p in sent[since:] if m == INFLIGHT_NOTIFICATION]

        harness = _LoopHarness(monkeypatch, call_tool)
        try:
            harness.send(_initialize_asking_for_inflight(0))
            harness.send(_tools_call_named(1, "a"))
            assert started.wait(timeout=5.0)
            harness.send(_tools_call_named(2, "b"))
            assert harness.wait_for(lambda: any(2 in ids for ids in reported()))

            harness.send(_cancel(1))
            # Reports after the cancel: the queued call is not vouched for.
            assert harness.wait_for(
                lambda: len(reported()) > 0 and 2 not in reported()[-1]
            ), "the queue behind a cancelled hung call was still named"
            mark = len(sent)
            assert harness.wait_for(lambda: len(reported(mark)) >= 2)
            assert all(2 not in ids for ids in reported(mark))
        finally:
            release.set()
            harness.close()


class TestStuckBoundMeasuresEachPhase:
    def test_a_short_queue_wait_is_progressing_a_long_one_is_not(self):
        """``progressing`` bounds the queue wait and the thread hold separately:
        a step submitted but not started (``step_started`` 0.0) is reported
        while its queue wait is under the bound -- a thread is merely busy --
        and dropped once past it, because a call that cannot get a thread for
        that long is not moving, and the gateway must be allowed to see it."""
        bound = mcp_shared.DEFERRED_STEP_STUCK_SECS
        entry = mcp_shared._DeferredEntry(
            1, "wait", None, "", _Parked("x", release=threading.Event())
        )
        entry.stepping = MagicMock()  # a future that exists but has not run
        entry.step_queued, entry.step_started = 10_000.0 - 1, 0.0
        assert entry.progressing(now=10_000.0)
        entry.step_queued = 10_000.0 - bound - 1
        assert not entry.progressing(now=10_000.0), "queued forever must not read as progressing"
        # Once running, the hold is what counts, whatever the queue wait was.
        entry.step_started = 10_000.0 - 1
        assert entry.progressing(now=10_000.0)
        entry.step_started = 10_000.0 - bound - 1
        assert not entry.progressing(now=10_000.0)
        entry.stepping = None
        assert entry.progressing(now=10_000.0)

    def test_a_cancel_landing_on_a_step_that_raises_still_runs_the_cancel_hook(self, monkeypatch):
        """An ``ask_question`` slice that raises while its cancel is landing must
        still withdraw the card: the hook runs on the step-raised arm too."""
        hold_step = threading.Event()

        class _RaisesWhenReleased(_Parked):
            def step(self) -> str | None:
                self.steps += 1
                self.hold_step.wait(timeout=5.0)  # type: ignore[union-attr]
                raise RuntimeError("slice failed")

        parked = _RaisesWhenReleased("never", release=threading.Event(), hold_step=hold_step)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: parked.steps >= 1)
            harness.send(_cancel(1))
            harness.send(_ping(9))
            assert harness.wait_for(lambda: any(r[0] == 9 for r in harness.responses))
            hold_step.set()
            assert harness.wait_for(lambda: len(_audits(harness, 1, "cancelled")) == 1)
            assert parked.cancelled, "the cancel hook did not run on the step-raised arm"
            assert not any(r[0] == 1 for r in harness.responses)
            assert _audits(harness, 1, "failed") == []
        finally:
            hold_step.set()
            harness.close()


class TestDeferredAudit:
    def test_call_tool_with_logging_commits_at_settle_not_in_a_step(self):
        """``call_tool_with_logging`` writes the completed/failed row for a
        blocking tool at return; for a deferred one it writes it when the
        DRIVER commits the settling text (``on_settled``), so no call is
        audited before it ended and none beside a ``cancelled`` row."""
        sel_mock = MagicMock()
        release = threading.Event()
        parked = _Parked("slept", release=release)
        with patch.object(mcp_shared, "sel", lambda: sel_mock):
            out = mcp_shared.call_tool_with_logging(
                "wait",
                {},
                lambda n, a: a,
                lambda n, a: parked,
                session_key="s",
                downstream_service="d",
            )
            assert isinstance(out, DeferredTool)
            assert out.step() is None
            release.set()
            assert out.step() == "slept"
            assert sel_mock.log_tool_invocation.call_count == 0, "a step must not commit"
            out.on_settled("slept")
            assert sel_mock.log_tool_invocation.call_count == 1
            assert sel_mock.log_tool_invocation.call_args.kwargs["outcome"] == "completed"

    def test_drive_deferred_commits_the_settling_text(self):
        sel_mock = MagicMock()
        release = threading.Event()
        release.set()
        parked = _Parked("Error: nope", release=release)
        with patch.object(mcp_shared, "sel", lambda: sel_mock):
            out = mcp_shared.call_tool_with_logging(
                "wait",
                {},
                lambda n, a: a,
                lambda n, a: parked,
                session_key="s",
                downstream_service="d",
            )
            assert drive_deferred(out) == "Error: nope"
        assert sel_mock.log_tool_invocation.call_count == 1
        assert sel_mock.log_tool_invocation.call_args.kwargs["outcome"] == "failed"

    def test_drive_deferred_runs_steps_inline_sleeping_until_due(self):
        """The inline driver (Windows path, direct ``_call_tool``) sleeps exactly
        to each step's due time and returns the settling text."""
        clock = MagicMock()
        clock.monotonic.return_value = 10.0

        class _Two(DeferredTool):
            def __init__(self) -> None:
                self.n = 0

            def due_at(self) -> float:
                return 12.5

            def step(self) -> str | None:
                self.n += 1
                return "ok" if self.n == 2 else None

        assert drive_deferred(_Two(), clock=clock) == "ok"
        clock.sleep.assert_called_once_with(2.5)


class TestWindowsPathDrivesDeferredInline:
    def test_synchronous_dispatch_collapses_a_deferred(self, monkeypatch):
        """On the non-POSIX path there is no loop timer: a deferred tool is
        driven to completion inline, so the client still gets its text."""
        monkeypatch.setattr(mcp_shared.platform_compat, "IS_POSIX", False)
        release = threading.Event()
        release.set()
        parked = _Parked("slept", release=release)
        harness = _LoopHarness(monkeypatch, lambda n, a: parked if n == "wait" else "ok")
        try:
            harness.send(_tools_call_named(1, "wait"))
            assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
            assert _text(harness, 1) == "slept"
        finally:
            harness.close()


def test_mapped_deferred_applies_fn_to_the_settling_text_only():
    release = threading.Event()
    parked = _Parked("slept", release=release)
    mapped = mcp_shared.map_deferred(parked, lambda t: json.dumps({"wrapped": t}))
    assert mapped.step() is None
    release.set()
    assert mapped.step() == '{"wrapped": "slept"}'
    mapped.cancel()
    assert parked.cancelled


def test_wrappers_forward_abandon_and_the_audit_runs_only_on_settle():
    """``call_tool_with_logging`` and ``mcp_core`` wrap a tool's deferred, so a
    wrapper that dropped ``abandon`` would silently turn a spawn's answer back
    into "retry". The mapped wrapper post-processes the text like a step's."""
    audits: list[str] = []
    answers = _Abandoning("spawned a1")
    audited = mcp_shared._AuditedDeferred(answers, audits.append)
    mapped = mcp_shared.map_deferred(audited, lambda t: f"<{t}>")
    assert mapped.abandon() == "<spawned a1>"
    assert audits == []
    mapped.on_settled("<spawned a1>")
    assert audits == ["<spawned a1>"]
    assert mcp_shared.map_deferred(_Abandoning(None), str.upper).abandon() is None


def test_spawn_collection_commit_installs_its_call_identity(monkeypatch):
    from kiro_crew import mcp_core
    from kiro_crew.mcp_tools.spawn import _SubAgentsStep

    calls: list = []
    contexts: list = []
    monkeypatch.setattr(mcp_core, "_get", lambda path: {"done": True, "result": "ok"})

    def post(path, body, **kwargs):
        calls.append((path, body, current_caller(), current_tenant_nonce()))
        return {}

    monkeypatch.setattr(mcp_core, "_post", post)
    step = _SubAgentsStep(
        sa_ids=["a1"],
        sa_deferred=set(),
        sa_errors=[],
        parent_session="dashboard:owner",
        max_wait=60,
        inline_result=lambda aid, text: text,
    )
    step.phase = "collect"
    harness = _LoopHarness(monkeypatch, lambda n, a: step)

    def respond(req_id, result, error=None):
        contexts.append((current_caller(), current_tenant_nonce()))
        harness._record(req_id, result, error)

    monkeypatch.setattr(mcp_shared, "respond", respond)
    try:
        harness.send(_identity_call(1, "spawn_sub_agents", "dashboard:owner", "owner-nonce"))
        assert harness.wait_for(lambda: any(r[0] == 1 for r in harness.responses))
        assert len(calls) == 1
        path, body, caller, nonce = calls[0]
        assert path == "/api/spawn/mark-collected"
        assert body == {"ids": ["a1"], "parent_session": "dashboard:owner"}
        assert caller.session_key == "dashboard:owner" and nonce == "owner-nonce"
        assert contexts == [(None, "")]
    finally:
        harness.close()


def test_ask_queue_budget_is_inside_the_outcome_retention_grace():
    """Every parked card is polled again before the dashboard drops its answer:
    a full step pool serializes the cards, one server-fixed slice each."""
    import math

    from kiro_crew.dashboard.state import DashboardState
    from kiro_crew.validation import ASK_WAIT_SLICE_SECS

    queue_budget = (
        math.ceil(mcp_shared.DEFERRED_PARKED_MAX / mcp_shared.DEFERRED_STEP_WORKERS)
        * ASK_WAIT_SLICE_SECS
    )
    assert queue_budget < DashboardState._AGENT_ASK_STALE_SECS
