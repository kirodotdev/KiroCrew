"""The anomaly-only turn timing log: what writes a line, and what bounds the lines."""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    EVENT_TOOL_RESULT,
    AcpEvent,
)
from kiro_crew.tracing import turn_anomalies

PROMPT_TEXT = "please summarise my private notes"


class _Source:
    backend = "kas"

    def __init__(self, session_key="dashboard_chat-7"):
        self._session_key = session_key
        self._model = "claude-sonnet"
        self._resolved_model_id = ""


class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class _Captured:
    """The anomaly lines this test logged, read like the file they land in."""

    def __init__(self, caplog):
        self._caplog = caplog

    def records(self):
        return [r for r in self._caplog.records if r.name == turn_anomalies.__name__]

    def exists(self):
        return bool(self.records())

    def read_text(self):
        return "\n".join(r.getMessage() for r in self.records())


@pytest.fixture
def log(caplog, monkeypatch):
    caplog.set_level(logging.WARNING, logger=turn_anomalies.__name__)
    clock = _Clock()
    monkeypatch.setattr(turn_anomalies, "_clock", clock)
    turn_anomalies.reset_for_testing()
    yield _Captured(caplog), clock
    turn_anomalies.reset_for_testing()


def _lines(path):
    prefix = turn_anomalies.TAG + " "
    out = []
    for line in path.read_text().splitlines():
        assert line.startswith(prefix), line
        out.append(json.loads(line[len(prefix) :]))
    return out


async def _timed(clock, steps, raise_exc=None):
    """Yield each ``(seconds_later, event)`` after moving the fake clock on."""
    for delay, event in steps:
        clock.now += delay
        yield event
    if raise_exc is not None:
        raise raise_exc


def _run(source, clock, steps, raise_exc=None):
    async def go():
        seen = []
        async for event in turn_anomalies.watched(source, _timed(clock, steps, raise_exc)):
            seen.append(event)
        return seen

    return asyncio.run(go())


def _text(text=PROMPT_TEXT):
    return AcpEvent(kind=EVENT_TEXT_CHUNK, text=text)


def _done(reason="end_turn"):
    return AcpEvent(kind=EVENT_COMPLETE, stop_reason=reason)


def test_a_normal_turn_writes_nothing(log):
    path, clock = log
    events = [(2, _text()), (1, _text()), (1, _done())]
    seen = _run(_Source(), clock, events)
    assert [e for _, e in events] == seen  # handed on unchanged
    assert not path.exists()


def test_a_slow_first_output_writes_one_line_without_content(log):
    path, clock = log
    _run(_Source(), clock, [(45, _text()), (1, _done())])
    (line,) = _lines(path)
    assert line["reasons"] == ["slow_first_output"]
    assert line["first_output_s"] == 45.0
    assert line["total_s"] == 46.0
    assert line["session_key"] == "dashboard_chat-7"
    assert line["backend"] == "kas"
    assert line["model"] == "claude-sonnet"
    assert line["stop_reason"] == "end_turn"
    assert PROMPT_TEXT not in path.read_text()


def test_a_long_tool_or_user_wait_is_not_a_model_gap(log):
    path, clock = log
    call = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1")
    result = AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t1", tool_final=True)
    ask = AcpEvent(kind=EVENT_PERMISSION_REQUEST, tool_call_id="t2")
    _run(
        _Source(),
        clock,
        [(1, call), (300, result), (2, ask), (500, _text()), (1, _done())],
    )
    assert not path.exists()


def test_a_long_model_silence_mid_turn_is_logged(log):
    path, clock = log
    call = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1")
    result = AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t1", tool_final=True)
    _run(_Source(), clock, [(1, call), (5, result), (240, _text()), (1, _done())])
    (line,) = _lines(path)
    assert line["reasons"] == ["slow_model_gap"]
    assert line["max_model_gap_s"] == 240.0
    assert line["tool_calls"] == 1


def test_an_error_stop_and_a_raised_stream_are_logged_by_class_name(log):
    path, clock = log
    _run(_Source("a"), clock, [(1, _text()), (1, _done("error: boom"))])
    with pytest.raises(RuntimeError):
        _run(_Source("b"), clock, [(1, _text())], raise_exc=RuntimeError("secret detail"))
    first, second = _lines(path)
    assert first["reasons"] == ["bad_stop"]
    assert second["reasons"] == ["exception"]
    assert second["error"] == "RuntimeError"
    assert "secret detail" not in path.read_text()


def test_the_terminal_tool_statuses_match_the_acp_set():
    from kiro_crew.acp.types import TERMINAL_TOOL_STATUSES

    assert turn_anomalies._TOOL_TERMINAL == TERMINAL_TOOL_STATUSES


def test_a_refused_tool_ends_the_tool_wait(log):
    path, clock = log
    call = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id="t1")
    refused = AcpEvent(kind=EVENT_TOOL_RESULT, tool_call_id="t1", tool_status="refused")
    _run(_Source(), clock, [(1, call), (1, refused), (120, _text()), (1, _done())])
    (line,) = _lines(path)
    assert line["reasons"] == ["slow_model_gap"]


def test_a_status_frame_does_not_end_a_pending_user_ask(log):
    path, clock = log
    ask = AcpEvent(kind=EVENT_PERMISSION_REQUEST, tool_call_id="t2")
    status = AcpEvent(kind="structured_status")
    _run(_Source(), clock, [(1, _text()), (1, ask), (1, status), (300, _text()), (1, _done())])
    assert not path.exists()


def test_an_mcp_server_start_ends_the_sign_in_wait(log):
    path, clock = log
    ask = AcpEvent(kind="mcp_oauth_request")
    started = AcpEvent(kind="mcp_server_initialized")
    _run(_Source(), clock, [(1, _text()), (1, ask), (300, started), (180, _text()), (1, _done())])
    (line,) = _lines(path)
    assert line["reasons"] == ["slow_model_gap"]


def test_an_oversized_tool_call_id_is_not_tracked(log):
    path, clock = log
    huge = "x" * 10_000
    call = AcpEvent(kind=EVENT_TOOL_CALL, tool_call_id=huge)
    timer = turn_anomalies.TurnTimer(_Source())
    timer.observe(call)
    assert timer.tool_calls == 1 and not timer._open_tools


def test_a_line_is_one_tagged_warning(log):
    path, clock = log
    turn_anomalies.write({"session_key": "s", "reasons": ["x"]})
    (record,) = path.records()
    assert record.levelno == logging.WARNING
    assert record.getMessage() == 'turn-anomaly {"reasons": ["x"], "session_key": "s"}'


def test_the_rate_limit_drops_extra_lines_then_counts_them(log):
    path, clock = log
    record = {"session_key": "s", "reasons": ["x"]}
    for _ in range(turn_anomalies.RATE_LIMIT_LINES + 3):
        turn_anomalies.write(dict(record))
    assert len(_lines(path)) == turn_anomalies.RATE_LIMIT_LINES
    turn_anomalies.write({"session_key": "other", "reasons": ["x"]})  # its own budget
    clock.now += turn_anomalies.RATE_LIMIT_WINDOW_SECS
    turn_anomalies.write(dict(record))
    lines = _lines(path)
    assert len(lines) == turn_anomalies.RATE_LIMIT_LINES + 2
    assert lines[-1]["suppressed"] == 3


def _run_breaking_on_complete(source, clock, steps, after_complete):
    """Consume like the dashboard runner: break on complete, close later."""

    async def go():
        stream = turn_anomalies.watched(source, _timed(clock, steps))
        async for event in stream:
            if event.kind == EVENT_COMPLETE:
                break
        clock.now += after_complete  # settle, persist, Stop hook ...
        await stream.aclose()

    asyncio.run(go())


def test_post_turn_work_after_complete_is_not_counted(log):
    path, clock = log
    after = turn_anomalies.MODEL_GAP_SLOW_SECS + 30
    _run_breaking_on_complete(_Source(), clock, [(2, _text()), (1, _done())], after)
    assert not path.exists()


def test_a_slow_turn_broken_on_complete_ends_at_complete(log):
    path, clock = log
    _run_breaking_on_complete(_Source(), clock, [(45, _text()), (1, _done())], 90)
    (line,) = _lines(path)
    assert line["reasons"] == ["slow_first_output"]
    assert line["total_s"] == 46.0
    assert line["error"] == ""


def test_a_stream_closed_before_complete_is_not_an_error(log):
    path, clock = log

    async def go():
        stream = turn_anomalies.watched(_Source(), _timed(clock, [(45, _text()), (1, _done())]))
        async for _ in stream:
            break
        await stream.aclose()

    asyncio.run(go())
    (line,) = _lines(path)
    assert line["reasons"] == ["slow_first_output"]
    assert line["error"] == ""
