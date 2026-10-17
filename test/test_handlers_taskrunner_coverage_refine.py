"""Coverage of the task-runner refine handlers.

Starting a refine (validation, the background task, cancelling the previous
one), its status and cancel, answering the agent's question, and
``_run_refine`` itself: streaming, the throttled progress pushes, the final
status and the redaction of what it reports. Shared helpers live in
``handlers_taskrunner_coverage_helpers.py``.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from handlers_taskrunner_coverage_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _EXFIL_URL,
    _body,
    _request,
    _stub_sel,
)

from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    STOP_REASON_STALE_RECOVER,
    AcpEvent,
)
from kiro_crew.dashboard.handlers.taskrunner import (
    _run_refine,
    api_taskrunner_refine,
    api_taskrunner_refine_answer,
    api_taskrunner_refine_cancel,
    api_taskrunner_refine_status,
)

# ── refine ──


def _refine_state() -> Any:
    """A DashboardState stand-in carrying only the refine-related attributes."""
    return SimpleNamespace(
        task_runner=None,
        sessions=MagicMock(),
        _refine_task=None,
        _refine_text="",
        _refine_error="",
        _refine_status="idle",
        _refine_input="",
        _refine_session_key="",
        _refine_answer_future=None,
        _background_tasks=set(),
        broadcast_ws=MagicMock(),
        push_refresh=MagicMock(),
        push_slots_update=MagicMock(),
    )


class TestRefineStart:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self) -> None:
        resp = await api_taskrunner_refine(_request(_refine_state(), raw_json_error=True))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_blank_input_is_400(self) -> None:
        resp = await api_taskrunner_refine(_request(_refine_state(), json_body={"input": "  "}))
        assert resp.status == 400
        assert _body(resp)["error"] == "input is required"

    @pytest.mark.asyncio
    async def test_starts_background_task_and_cancels_previous(self) -> None:
        state = _refine_state()
        started = asyncio.Event()

        async def _never() -> None:
            started.set()
            # flake-ok: the stand-in previous refine parks here until the handler cancels it; the gather below reaps it
            await asyncio.Event().wait()

        previous = asyncio.create_task(_never())
        # flake-ok: set by the stand-in task's first statement, which runs as soon as this test yields
        await started.wait()
        state._refine_task = previous

        async def _noop(*_a: Any, **_kw: Any) -> None:
            return None

        with patch("kiro_crew.dashboard.handlers.taskrunner._run_refine", new=_noop):
            resp = await api_taskrunner_refine(
                _request(state, json_body={"input": " build a thing "})
            )
        assert _body(resp) == {"ok": True}
        assert state._refine_status == "running"
        assert state._refine_input == "build a thing"
        # flake-ok: the handler cancelled previous, so the gather collects its cancellation
        await asyncio.gather(previous, return_exceptions=True)
        assert previous.cancelled()
        # flake-ok: the background task runs the patched no-op _run_refine and ends on its first pass
        await asyncio.gather(*list(state._background_tasks), return_exceptions=True)
        assert state._background_tasks == set()


class TestRefineStatusAndCancel:
    @pytest.mark.asyncio
    async def test_status_echoes_state(self) -> None:
        state = _refine_state()
        state._refine_status = "done"
        state._refine_text = "spec"
        state._refine_input = "in"
        resp = await api_taskrunner_refine_status(_request(state, "GET"))
        assert _body(resp) == {
            "status": "done",
            "text": "spec",
            "error": "",
            "input": "in",
            "waiting": False,
        }

    @pytest.mark.asyncio
    async def test_cancel_without_task_is_ok(self) -> None:
        resp = await api_taskrunner_refine_cancel(_request(_refine_state()))
        assert _body(resp) == {"ok": True}

    @pytest.mark.asyncio
    async def test_cancel_cancels_running_task(self) -> None:
        state = _refine_state()
        started = asyncio.Event()

        async def _never() -> None:
            started.set()
            # flake-ok: the stand-in refine parks here until the cancel handler cancels it; the gather below reaps it
            await asyncio.Event().wait()

        task = asyncio.create_task(_never())
        # flake-ok: set by the stand-in task's first statement, which runs as soon as this test yields
        await started.wait()
        state._refine_task = task
        resp = await api_taskrunner_refine_cancel(_request(state))
        assert _body(resp) == {"ok": True}
        # flake-ok: the cancel handler cancelled task, so the gather collects its cancellation
        await asyncio.gather(task, return_exceptions=True)
        assert task.cancelled()


class TestRefineAnswer:
    @pytest.mark.asyncio
    async def test_invalid_json_is_400(self) -> None:
        resp = await api_taskrunner_refine_answer(_request(_refine_state(), raw_json_error=True))
        assert resp.status == 400

    @pytest.mark.asyncio
    async def test_blank_answer_is_400(self) -> None:
        resp = await api_taskrunner_refine_answer(
            _request(_refine_state(), json_body={"answer": " "})
        )
        assert resp.status == 400
        assert _body(resp)["error"] == "answer required"

    @pytest.mark.asyncio
    async def test_no_pending_question_is_409(self) -> None:
        resp = await api_taskrunner_refine_answer(
            _request(_refine_state(), json_body={"answer": "yes"})
        )
        assert resp.status == 409
        assert _body(resp)["error"] == "no pending question"

    @pytest.mark.asyncio
    async def test_already_resolved_future_is_409(self) -> None:
        state = _refine_state()
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        future.set_result("earlier")
        state._refine_answer_future = future
        resp = await api_taskrunner_refine_answer(_request(state, json_body={"answer": "yes"}))
        assert resp.status == 409
        assert _body(resp)["error"] == "no pending question"

    @pytest.mark.asyncio
    async def test_concurrent_set_result_is_409(self) -> None:
        """A racing answer that lands between the ``done()`` check and
        ``set_result`` surfaces as 409, not a 500."""

        class _RacingFuture:
            def done(self) -> bool:
                return False

            def set_result(self, _value: str) -> None:
                raise asyncio.InvalidStateError("already resolved")

        state = _refine_state()
        state._refine_answer_future = _RacingFuture()
        resp = await api_taskrunner_refine_answer(_request(state, json_body={"answer": "yes"}))
        assert resp.status == 409
        assert _body(resp)["error"] == "question already resolved"

    @pytest.mark.asyncio
    async def test_answer_resolves_future(self) -> None:
        state = _refine_state()
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        state._refine_answer_future = future
        resp = await api_taskrunner_refine_answer(_request(state, json_body={"answer": "  yes  "}))
        assert _body(resp) == {"ok": True}
        assert future.result() == "yes"
        # `waiting` flips to False once the question is answered.
        status = await api_taskrunner_refine_status(_request(state, "GET"))
        assert _body(status)["waiting"] is True  # future object still attached


class TestRunRefine:
    """The background refine coroutine itself (no real LLM, no real session)."""

    @staticmethod
    def _sessions(events: list[AcpEvent]) -> MagicMock:
        client = MagicMock()

        async def _stream(_msg: str) -> Any:
            for ev in events:
                yield ev

        client.stream = _stream
        client.reject_tool = AsyncMock()
        sessions = MagicMock()
        sessions.get_or_create = AsyncMock(return_value=(client, True, False))
        sessions.release = MagicMock()
        sessions.reset = AsyncMock()
        return sessions

    @pytest.mark.asyncio
    async def test_happy_path_redacts_and_broadcasts_done(self) -> None:
        state = _refine_state()
        state.sessions = self._sessions(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="# Task: leak "),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text=_EXFIL_URL),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        await _run_refine(state, "build a thing")
        assert state._refine_status == "done"
        assert state._refine_task is None
        assert state._refine_session_key == ""
        final = state.broadcast_ws.call_args.args[1]
        assert final["status"] == "done"
        assert "[REDACTED" in final["text"]
        assert _EXFIL_URL not in final["text"]
        state.sessions.release.assert_called_once()
        state.sessions.reset.assert_awaited_once()
        state.push_refresh.assert_called_once_with("taskrunner")

    @pytest.mark.asyncio
    async def test_streaming_chunks_are_throttle_pushed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Interim progress is broadcast once the throttle window has elapsed.

        The clock is stubbed (monotonic returns a fixed, increasing sequence) so
        the test never sleeps and never depends on wall-clock timing.
        """
        import time as real_time

        clock = {"t": 0.0}

        def _monotonic() -> float:
            clock["t"] += 1.0
            return clock["t"]

        # flake-ok: the injected clock, a fixed increasing sequence, so the throttle never reads wall-clock time; pre-existing, moved verbatim
        # nosemgrep: kirocrew.test-stdlib-clock-rebound -- pre-existing stub clock the docstring describes, moved verbatim; tracked for a de-flake pass
        monkeypatch.setattr(real_time, "monotonic", _monotonic)
        state = _refine_state()
        state.sessions = self._sessions(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="a"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="b"),
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="c"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        await _run_refine(state, "x")
        assert state._refine_text == "abc"
        # opening push + one push per chunk (clock always past the throttle
        # window) + the post-loop push, then the final "done" broadcast.
        statuses = [c.args[1]["status"] for c in state.broadcast_ws.call_args_list]
        assert statuses.count("running") == 5
        assert statuses[-1] == "done"

    @pytest.mark.asyncio
    async def test_permission_requests_are_rejected(self) -> None:
        state = _refine_state()
        sessions = self._sessions(
            [
                AcpEvent(kind=EVENT_PERMISSION_REQUEST, title="write_file", request_id="r1"),
                AcpEvent(kind=EVENT_COMPLETE),
            ]
        )
        state.sessions = sessions
        await _run_refine(state, "x")
        client = sessions.get_or_create.return_value[0]
        client.reject_tool.assert_awaited_once_with("r1")
        assert state._refine_status == "done"

    @pytest.mark.asyncio
    async def test_stream_error_is_captured_not_raised(self) -> None:
        state = _refine_state()
        sessions = self._sessions([])
        sessions.get_or_create = AsyncMock(side_effect=RuntimeError("provider down"))
        state.sessions = sessions
        await _run_refine(state, "x")
        assert state._refine_status == "error"
        assert state._refine_error == "provider down"
        assert state.broadcast_ws.call_args.args[1]["error"] == "provider down"

    @pytest.mark.asyncio
    async def test_teardown_failures_are_swallowed(self) -> None:
        state = _refine_state()
        sessions = self._sessions([AcpEvent(kind=EVENT_COMPLETE)])
        sessions.release = MagicMock(side_effect=RuntimeError("release boom"))
        sessions.reset = AsyncMock(side_effect=RuntimeError("reset boom"))
        state.sessions = sessions
        await _run_refine(state, "x")
        # A failing teardown must not mask the completed refine.
        assert state._refine_status == "done"
        assert state.broadcast_ws.call_args.args[1]["status"] == "done"

    @pytest.mark.asyncio
    async def test_cancellation_marks_cancelled(self) -> None:
        state = _refine_state()
        sessions = self._sessions([])
        sessions.get_or_create = AsyncMock(side_effect=asyncio.CancelledError())
        state.sessions = sessions
        await _run_refine(state, "x")
        assert state._refine_status == "cancelled"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["timeout", STOP_REASON_STALE_RECOVER])
    async def test_a_refine_the_backend_never_finished_is_an_error(self, stop_reason) -> None:
        """A cut-off refine is reported as an error that names the stop reason, and
        its partial spec is still broadcast, so it cannot pass for a finished one."""
        state = _refine_state()
        state.sessions = self._sessions(
            [
                AcpEvent(
                    kind=EVENT_TEXT_CHUNK, text="## Spec\n1. Build the parser\n2. Wire the CL"
                ),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason),
            ]
        )
        await _run_refine(state, "build a thing")
        final = state.broadcast_ws.call_args.args[1]
        assert final["status"] == "error"
        assert "did not finish the turn" in final["error"]
        assert stop_reason in final["error"]
        assert final["text"].endswith("Wire the CL")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("stop_reason", ["end_turn", "cancelled"])
    async def test_a_refine_that_ends_its_turn_is_done(self, stop_reason) -> None:
        """A finished turn and a user-requested stop keep the refined spec."""
        state = _refine_state()
        state.sessions = self._sessions(
            [
                AcpEvent(kind=EVENT_TEXT_CHUNK, text="## Spec\n1. Build the parser"),
                AcpEvent(kind=EVENT_COMPLETE, stop_reason=stop_reason),
            ]
        )
        await _run_refine(state, "build a thing")
        final = state.broadcast_ws.call_args.args[1]
        assert final["status"] == "done"
        assert final["error"] == ""
