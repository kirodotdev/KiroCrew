"""``DeferredTool.on_answered``: a parked call hears whether its answer was written.

The driver calls it once, after it answered the call with the settling text:
True only once the whole response frame reached the client, False when the
write tore or the loop ended with the call still parked. A cancelled call
hears ``cancel`` instead. ``spawn_sub_agents`` commits its inline collection on
this hook, so a result is marked collected only when the reply carrying it was
written (``test_spawn_sub_agents_inline_collection``).
"""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
from test_mcp_shared import _LoopHarness, _tools_call

import kiro_crew.mcp_shared as mcp_shared
from kiro_crew import platform_compat
from kiro_crew.mcp_caller import current_caller
from kiro_crew.mcp_shared import DeferredTool, drive_deferred, map_deferred


class _Answered(DeferredTool):
    """Settles with ``text`` once ``release`` is set; records every hook."""

    def __init__(self, text: str = "done", release: threading.Event | None = None) -> None:
        self.text = text
        self.release = release
        self.events: list[tuple[str, Any]] = []
        self.answered = threading.Event()
        self.responses_at_answer: list = []
        self.harness: Any = None
        self.steps = 0

    def due_at(self) -> float:
        return 0.0

    def step(self) -> str | None:
        self.steps += 1
        if self.release is not None and not self.release.is_set():
            return None
        return self.text

    def cancel(self) -> None:
        self.events.append(("cancel", None))

    def on_settled(self, text: str) -> None:
        self.events.append(("settled", text))

    def on_answered(self, written: bool) -> None:
        caller = current_caller()
        self.events.append(("answered", (written, caller.session_key if caller else None)))
        if self.harness is not None:
            self.responses_at_answer = list(self.harness.responses)
        self.answered.set()


def _loop(monkeypatch, tool: _Answered, *, written: bool = True) -> _LoopHarness:
    harness = _LoopHarness(monkeypatch, lambda name, args: tool)
    tool.harness = harness
    if not written:
        # A torn frame: the client holds no complete answer.
        def _torn(req_id, result, error=None) -> bool:
            harness.responses.append((req_id, result, error))
            return False

        monkeypatch.setattr(mcp_shared, "respond", _torn)
    return harness


def test_a_written_answer_is_reported_after_the_frame_is_written(monkeypatch):
    tool = _Answered()
    harness = _loop(monkeypatch, tool)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.answered.wait(5.0)
    finally:
        harness.close()
    assert [e[0] for e in tool.events] == ["settled", "answered"]
    assert tool.events[-1][1][0] is True
    # The hook ran only once the response had been written.
    assert [r[0] for r in tool.responses_at_answer] == [1]


def test_a_torn_answer_is_reported_as_not_written(monkeypatch):
    tool = _Answered()
    harness = _loop(monkeypatch, tool, written=False)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.answered.wait(5.0)
    finally:
        harness.close()
    assert tool.events[-1] == ("answered", (False, None))


def test_a_cancelled_call_hears_cancel_and_never_an_answer(monkeypatch):
    release = threading.Event()
    tool = _Answered(release=release)
    harness = _loop(monkeypatch, tool)
    try:
        harness.send(_tools_call(1, "parked"))
        assert harness.wait_for(lambda: tool.steps >= 1)
        harness.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        )
        assert harness.wait_for(lambda: ("cancel", None) in tool.events)
        release.set()
    finally:
        harness.close()
    assert not any(e[0] == "answered" for e in tool.events)
    assert not harness.responses


def test_a_call_still_parked_when_the_loop_ends_hears_not_written(monkeypatch):
    tool = _Answered(release=threading.Event())  # never settles
    harness = _loop(monkeypatch, tool)
    harness.send(_tools_call(1, "parked"))
    assert harness.wait_for(lambda: tool.steps >= 1)
    harness.close()  # EOF
    assert tool.answered.wait(5.0)
    assert ("answered", (False, None)) in tool.events
    assert ("cancel", None) not in tool.events


def test_the_pump_loop_reports_the_write(monkeypatch):
    """Windows reads stdin on the pump thread and parks the call the same way."""
    monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    tool = _Answered()
    harness = _loop(monkeypatch, tool)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.answered.wait(5.0)
    finally:
        harness.close()
    assert [e[0] for e in tool.events] == ["settled", "answered"]
    assert tool.events[-1][1][0] is True
    assert [r[0] for r in tool.responses_at_answer] == [1]


def test_a_direct_drive_is_answered_at_once():
    direct = _Answered()
    assert drive_deferred(direct) == "done"
    assert [e[0] for e in direct.events] == ["settled", "answered"]


def test_wrappers_forward_the_answer():
    inner = _Answered()
    map_deferred(inner, str.upper).on_answered(True)
    assert inner.events == [("answered", (True, None))]


class _Raising(_Answered):
    """``_Answered`` with a chosen hook that raises."""

    def __init__(self, *, in_step: bool = False, in_settled: bool = False, **kw: Any) -> None:
        super().__init__(**kw)
        self.in_step = in_step
        self.in_settled = in_settled

    def step(self) -> str | None:
        if self.in_step:
            self.steps += 1
            raise RuntimeError("step failed after it acted")
        return super().step()

    def on_settled(self, text: str) -> None:
        super().on_settled(text)
        if self.in_settled:
            raise OSError("audit write failed")


def test_a_direct_drive_whose_settle_raises_hears_not_written():
    """The caller gets the exception, not the text, so the call hears False."""
    tool = _Raising(in_settled=True)
    with pytest.raises(OSError):
        drive_deferred(tool)
    assert tool.events == [("settled", "done"), ("answered", (False, None))]


def test_a_parked_step_that_raises_hears_not_written(monkeypatch):
    tool = _Raising(in_step=True)
    harness = _loop(monkeypatch, tool)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.answered.wait(5.0)
    finally:
        harness.close()
    assert harness.responses[0][1]["content"][0]["text"].startswith("Error:")
    assert tool.events == [("answered", (False, None))]


class _DueAtRaises(_Answered):
    def due_at(self) -> float:
        raise RuntimeError("bookkeeping failed")


def test_a_call_the_loop_fails_to_service_hears_not_written(monkeypatch):
    """The catch-all answers -32603 and drops the entry: the call is told."""
    tool = _DueAtRaises()
    harness = _loop(monkeypatch, tool)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.answered.wait(5.0)
    finally:
        harness.close()
    assert harness.responses[0][2]["code"] == mcp_shared.JSONRPC_INTERNAL_ERROR
    assert tool.events == [("answered", (False, None))]


def test_a_call_displaced_by_a_reused_request_id_hears_not_written(monkeypatch):
    first = _Answered(release=threading.Event())  # stays parked
    second = _Answered()
    tools = iter([first, second])
    harness = _LoopHarness(monkeypatch, lambda name, args: next(tools))
    try:
        harness.send(_tools_call(1, "parked"))
        assert harness.wait_for(lambda: first.steps >= 1)
        harness.send(_tools_call(1, "parked"))  # the same id, still parked
        assert first.answered.wait(5.0)
        assert second.answered.wait(5.0)
    finally:
        harness.close()
    assert first.events == [("answered", (False, None))]
    assert [e[0] for e in second.events] == ["settled", "answered"]
    assert [r[0] for r in harness.responses] == [1]


def test_an_abandoned_call_with_text_hears_settled_then_answered(monkeypatch):
    """``abandon`` text is the answer, so it is settled and reported like one."""

    class _Abandons(_Answered):
        def abandon(self) -> str | None:
            self.events.append(("abandon", None))
            return "children still running"

    monkeypatch.setattr(mcp_shared, "DEFERRED_PARKED_MAX", 0)  # the table is full
    tool = _Abandons()
    harness = _loop(monkeypatch, tool)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.answered.wait(5.0)
    finally:
        harness.close()
    assert tool.events == [
        ("abandon", None),
        ("settled", "children still running"),
        ("answered", (True, None)),
    ]


def test_a_tool_without_the_hook_is_answered_as_before(monkeypatch):
    """Every new report is the default no-op for a tool that does not listen."""

    class _Plain(DeferredTool):
        def due_at(self) -> float:
            raise RuntimeError("bookkeeping failed")

        def step(self) -> str | None:
            return "never"

    harness = _LoopHarness(monkeypatch, lambda name, args: _Plain())
    try:
        harness.send(_tools_call(1, "parked"))
        assert harness.wait_for(lambda: bool(harness.responses))
    finally:
        harness.close()
    assert [(r[0], r[2]["code"]) for r in harness.responses] == [
        (1, mcp_shared.JSONRPC_INTERNAL_ERROR)
    ]


def test_a_direct_drive_propagates_the_settle_failure_when_the_report_also_fails():
    """A failing ``on_answered(False)`` is logged: the settle error propagates."""

    class _BothRaise(_Raising):
        def on_answered(self, written: bool) -> None:
            super().on_answered(written)
            raise RuntimeError("report failed")

    tool = _BothRaise(in_settled=True)
    with pytest.raises(OSError, match="audit write failed"):
        drive_deferred(tool)
    assert tool.events == [("settled", "done"), ("answered", (False, None))]


def _respond_raises_once(monkeypatch, harness: _LoopHarness) -> None:
    """The next response write raises, as a failing stdout fallback does."""
    real = mcp_shared.respond
    calls: list[int] = []

    def _raising(req_id, result, error=None) -> bool:
        calls.append(1)
        if len(calls) == 1:
            raise OSError("stdout is gone")
        return real(req_id, result, error)

    monkeypatch.setattr(mcp_shared, "respond", _raising)


def test_a_parked_step_that_raises_hears_not_written_when_the_error_write_raises(monkeypatch):
    tool = _Raising(in_step=True)
    harness = _loop(monkeypatch, tool)
    _respond_raises_once(monkeypatch, harness)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.answered.wait(5.0)
    finally:
        harness.close()
    assert tool.events == [("answered", (False, None))]


def test_a_call_the_loop_fails_to_service_hears_not_written_when_the_error_write_raises(
    monkeypatch,
):
    escaped: list[BaseException | None] = []
    monkeypatch.setattr(threading, "excepthook", lambda args: escaped.append(args.exc_value))
    tool = _DueAtRaises()
    harness = _loop(monkeypatch, tool)
    _respond_raises_once(monkeypatch, harness)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.answered.wait(5.0)
    finally:
        harness.close()
    assert tool.events == [("answered", (False, None))]
    assert any(isinstance(exc, OSError) for exc in escaped)


@pytest.mark.parametrize("pump", [False, True], ids=["platform", "pump"])
def test_both_readers_log_a_raising_settle_at_warning_naming_the_request(monkeypatch, caplog, pump):
    """However stdin is read, a raising settle is logged once at WARNING
    naming the server and request id, and the call is still answered with
    its text (``_commit_settled``, as on main)."""
    if pump:
        monkeypatch.setattr(platform_compat, "IS_POSIX", False)
    tool = _Raising(in_settled=True)
    harness = _loop(monkeypatch, tool)
    with caplog.at_level("WARNING", logger=mcp_shared.logger.name):
        try:
            harness.send(_tools_call(7, "parked"))
            assert tool.answered.wait(5.0)
        finally:
            harness.close()
    assert harness.responses[0][1]["content"][0]["text"] == "done"
    assert tool.events[-1] == ("answered", (True, None))
    lines = [r for r in caplog.records if "on_settled failed for" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].levelno >= logging.WARNING
    assert lines[0].getMessage() == "test-server: on_settled failed for request 7"
    assert lines[0].exc_info is not None


class _HooksRaise(_Answered):
    """Every best-effort hook raises, so each one's WARNING line is observable."""

    def cancel(self) -> None:
        raise RuntimeError("cancel failed")

    def abandon(self) -> str | None:
        raise RuntimeError("abandon failed")

    def on_answered(self, written: bool) -> None:
        super().on_answered(written)
        raise RuntimeError("report failed")


def _hook_lines(caplog: pytest.LogCaptureFixture, prefix: str) -> list[str]:
    return [r.getMessage() for r in caplog.records if f": {prefix} failed" in r.getMessage()]


@pytest.mark.parametrize(
    "hook",
    ["on_answered", "cancel", "abandon"],
)
def test_every_hook_failure_names_the_server_and_request(monkeypatch, caplog, hook):
    """The cancel, abandon and on_answered lines name the call the same way
    the on_settled line does: server and request id."""
    release = threading.Event() if hook == "cancel" else None
    tool = _HooksRaise(release=release)
    if hook == "abandon":
        monkeypatch.setattr(mcp_shared, "DEFERRED_PARKED_MAX", 0)  # the table is full
    harness = _loop(monkeypatch, tool)
    with caplog.at_level("WARNING", logger=mcp_shared.logger.name):
        try:
            harness.send(_tools_call(9, "parked"))
            if hook == "cancel":
                assert harness.wait_for(lambda: tool.steps >= 1)
                harness.send(
                    {
                        "jsonrpc": "2.0",
                        "method": "notifications/cancelled",
                        "params": {"requestId": 9},
                    }
                )
                assert harness.wait_for(lambda: bool(_hook_lines(caplog, "cancel hook")))
                release.set()  # type: ignore[union-attr]
            elif hook == "abandon":
                assert harness.wait_for(lambda: bool(harness.responses))
            else:
                assert tool.answered.wait(5.0)
                assert harness.wait_for(lambda: bool(_hook_lines(caplog, "on_answered")))
        finally:
            harness.close()
    prefix = {"cancel": "cancel hook", "abandon": "abandon hook", "on_answered": "on_answered"}
    assert (
        _hook_lines(caplog, prefix[hook])[0] == f"test-server: {prefix[hook]} failed for request 9"
    )


class _SlowCommit(DeferredTool):
    """Settles at once; ``on_answered`` holds the dispatch thread like a commit.

    The hook waits until the test has written its next frames, then holds for
    ``hold`` seconds (the commit is bounded at 5 s) and records which responses
    exist as it returns: the frames sent inside the window are read only after.
    """

    def __init__(self, harness_ref: list, hold: float) -> None:
        self.harness_ref = harness_ref
        self.hold = hold
        self.entered = threading.Event()
        self.sent = threading.Event()
        self.answered_calls = 0
        self.responses_at_return: list = []

    def due_at(self) -> float:
        return 0.0

    def step(self) -> str | None:
        return "parked-done"

    def on_answered(self, written: bool) -> None:
        self.answered_calls += 1
        self.entered.set()
        assert self.sent.wait(10.0)
        # Models the commit's own duration; nothing sets this event.
        threading.Event().wait(self.hold)
        self.responses_at_return = [r[0] for r in self.harness_ref[0].responses]


@pytest.mark.parametrize("hold", [1.5, 5.0])
def test_frames_sent_while_a_commit_holds_the_loop_are_answered_once_after_it(monkeypatch, hold):
    """``spawn_sub_agents`` commits inside ``on_answered``, on the dispatch
    thread, so the loop reads nothing until the commit returns. A second call,
    a ping and a cancel for the answered call, sent inside that window, wait in
    the pipe: each is answered exactly once, in order, after the hook returns,
    and the late cancel is a no-op."""
    ref: list = []
    tool = _SlowCommit(ref, hold)
    harness = _LoopHarness(
        monkeypatch, lambda name, args: tool if name == "parked" else f"plain:{name}"
    )
    ref.append(harness)
    try:
        harness.send(_tools_call(1, "parked"))
        assert tool.entered.wait(5.0)
        harness.send(_tools_call(2, "other"))
        harness.send({"jsonrpc": "2.0", "id": 3, "method": "ping"})
        harness.send(
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 1}}
        )
        tool.sent.set()
        assert harness.wait_for(lambda: len(harness.responses) >= 3, timeout=hold + 10)
    finally:
        harness.close()
    assert tool.responses_at_return == [1]  # nothing read while the hook held
    assert [r[0] for r in harness.responses] == [1, 2, 3]  # once each, FIFO
    assert tool.answered_calls == 1
