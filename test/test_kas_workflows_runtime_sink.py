"""The runtime's workflow-sink seam: run notifications and step approvals.

Drives the REAL reader loop against a fake stdout, as test_acp_runtime does.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.runtime import AcpRuntime
from kiro_crew.testing.ids import UNALLOCATABLE_PID
from kiro_crew.testing.wait import async_wait_until


def _make_runtime():
    rt = AcpRuntime(work_dir="/tmp")
    reader = asyncio.StreamReader()
    proc = MagicMock()
    proc.stdout = reader
    proc.stdin = MagicMock()
    proc.stdin.write = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.returncode = None
    proc.pid = UNALLOCATABLE_PID
    rt._process = proc
    rt._pid = UNALLOCATABLE_PID
    rt._initialized = True
    return rt, reader, proc


def _feed(reader: asyncio.StreamReader, obj: dict) -> None:
    reader.feed_data((json.dumps(obj) + "\n").encode())


async def _start_reader(rt: AcpRuntime) -> asyncio.Task:
    stdout = rt._process.stdout
    stdout._loop = asyncio.get_running_loop()
    task = asyncio.ensure_future(rt._reader_loop())
    await asyncio.sleep(0)
    return task


async def _stop_reader(task: asyncio.Task) -> None:
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


def _written(proc) -> list[dict[str, Any]]:
    return [json.loads(c.args[0].decode()) for c in proc.stdin.write.call_args_list]


class _Sink:
    def __init__(
        self, *, step_sessions=(), raise_on_request: bool = False, raise_on_watch: bool = False
    ) -> None:
        self.notifications: list[tuple[str, dict]] = []
        self.watched: list[tuple[str, dict]] = []
        self.requests: list[tuple[Any, str, str, dict]] = []
        self.step_sessions = set(step_sessions)
        self.raise_on_request = raise_on_request
        self.raise_on_watch = raise_on_watch

    def claims_notification(self, method: str) -> bool:
        return method.startswith("_kiro/workflow/")

    def on_notification(self, method: str, params: dict) -> None:
        self.notifications.append((method, params))

    def watches_notification(self, method: str) -> bool:
        return method == "_kiro/session/notify"

    def on_watched_notification(self, method: str, params: dict) -> None:
        self.watched.append((method, params))
        if self.raise_on_watch:
            raise RuntimeError("boom")

    def claims_step_request(self, session_id: str, method: str) -> bool:
        return method == "session/request_permission" and session_id in self.step_sessions

    def on_step_request(self, request_id, session_id, method, params) -> None:
        if self.raise_on_request:
            raise RuntimeError("boom")
        self.requests.append((request_id, session_id, method, params))


@pytest.mark.asyncio
async def test_run_notification_goes_to_sink_not_to_sessions():
    rt, reader, _ = _make_runtime()
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["chat"] = queue
    sink = _Sink()
    rt.set_workflow_sink(sink)
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "_kiro/workflow/run_start", "params": {"workflowId": "w1"}})
        # node_start names the STEP's session: it must still reach the sink.
        _feed(
            reader,
            {
                "method": "_kiro/workflow/node_start",
                "params": {"workflowId": "w1", "nodeId": "a", "sessionId": "step-1"},
            },
        )
        await async_wait_until(
            lambda: len(sink.notifications) == 2, describe=lambda: sink.notifications
        )
        assert [m for m, _ in sink.notifications] == [
            "_kiro/workflow/run_start",
            "_kiro/workflow/node_start",
        ]
        assert queue.empty(), "a run's progress must not be broadcast to a chat"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_watched_note_is_seen_by_the_sink_and_still_reaches_its_session():
    for raise_on_watch in (False, True):
        rt, reader, _ = _make_runtime()
        queue: asyncio.Queue = asyncio.Queue()
        rt._session_queues["chat"] = queue
        sink = _Sink(raise_on_watch=raise_on_watch)
        rt.set_workflow_sink(sink)
        task = await _start_reader(rt)
        try:
            params = {"sessionId": "chat", "workflowId": "w1", "severity": "warning"}
            _feed(reader, {"method": "_kiro/session/notify", "params": params})
            # A sink that raises while watching still lets the frame route.
            await async_wait_until(lambda: not queue.empty())
            assert queue.get_nowait().method == "_kiro/session/notify"
            assert sink.watched == [("_kiro/session/notify", params)]
            assert sink.notifications == []
        finally:
            await _stop_reader(task)


@pytest.mark.asyncio
async def test_without_sink_the_demux_is_unchanged():
    rt, reader, _ = _make_runtime()
    assert rt._workflow_sink is None, "a kiro-backend runtime gets no sink"
    queue: asyncio.Queue = asyncio.Queue()
    rt._session_queues["chat"] = queue
    task = await _start_reader(rt)
    try:
        _feed(reader, {"method": "_kiro/workflow/run_start", "params": {"workflowId": "w1"}})
        await async_wait_until(lambda: not queue.empty())
        msg = queue.get_nowait()
        assert msg.method == "_kiro/workflow/run_start"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_claimed_step_permission_is_handed_to_sink_and_not_auto_rejected():
    rt, reader, proc = _make_runtime()
    sink = _Sink(step_sessions={"step-1"})
    rt.set_workflow_sink(sink)
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "id": 7,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "step-1",
                    "options": [{"optionId": "no", "kind": "reject_once"}],
                },
            },
        )
        await async_wait_until(lambda: sink.requests)
        # Barrier: an unclaimed request fed AFTER the claimed one is auto-rejected,
        # so once its answer is written the reader is past request 7. Any answer to
        # 7 would have been written by then, so the only answer must be 8's.
        _feed(
            reader,
            {
                "id": 8,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "someone-else",
                    "options": [{"optionId": "no", "kind": "reject_once"}],
                },
            },
        )
        await async_wait_until(lambda: _written(proc))
        assert sink.requests[0][:3] == (7, "step-1", "session/request_permission")
        assert [frame["id"] for frame in _written(proc)] == [
            8
        ], "the runtime must not also answer a claimed request"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_unclaimed_step_permission_is_still_auto_rejected():
    rt, reader, proc = _make_runtime()
    rt.set_workflow_sink(_Sink(step_sessions={"step-1"}))
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "id": 8,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "someone-else",
                    "options": [
                        {"optionId": "yes", "kind": "allow_once"},
                        {"optionId": "no", "kind": "reject_once"},
                    ],
                },
            },
        )
        await async_wait_until(lambda: _written(proc))
        answer = _written(proc)[0]
        assert answer["id"] == 8
        assert answer["result"]["outcome"] == {"outcome": "selected", "optionId": "no"}
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_sink_failure_on_a_claimed_request_rejects_rather_than_strands():
    rt, reader, proc = _make_runtime()
    rt.set_workflow_sink(_Sink(step_sessions={"step-1"}, raise_on_request=True))
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "id": 9,
                "method": "session/request_permission",
                "params": {
                    "sessionId": "step-1",
                    "options": [
                        {"optionId": "yes", "kind": "allow_once"},
                        {"optionId": "no", "kind": "reject_once"},
                    ],
                },
            },
        )
        await async_wait_until(lambda: _written(proc))
        assert _written(proc)[0]["result"]["outcome"]["optionId"] == "no"
    finally:
        await _stop_reader(task)


def test_a_kas_runtime_is_born_with_the_routers_sink(monkeypatch):
    from kiro_crew.acp.types import ACP_BACKEND_KAS
    from kiro_crew.kas_workflows import router as router_mod

    async def never():  # the query factory is not reached here
        raise AssertionError

    router = router_mod.KasWorkflowRouter(never)
    monkeypatch.setattr(router_mod, "_ROUTER", router)
    rt = AcpRuntime(work_dir="/tmp", acp_backend=ACP_BACKEND_KAS)
    assert rt._workflow_sink is not None
    assert rt._workflow_sink.claims_notification("_kiro/workflow/run_start")
    assert rt in set(router._runtimes)
