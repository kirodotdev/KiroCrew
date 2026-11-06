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


# ── A step is claimed only while its chat can answer it ──
#
# The REAL router's sink on the REAL reader loop: with no mode reader registered for
# the run's chat (note_chat_session), nothing would ever answer a held request, so the
# reader loop's own fail-closed reply must go out on the wire instead.


async def _router_sink(rt: AcpRuntime, tmp_path, *, reader: bool):
    from kas_workflow_helpers import make_router

    from kiro_crew.kas_workflows import wire

    router = make_router(ledger_path=tmp_path / "runs.json")
    await router.ensure_ledger()
    router._ledger["w1"] = {"slot": "chat-1", "cwd": str(tmp_path), "createdAt": 1.0}
    sink = router.sink_for(rt)
    rt.set_workflow_sink(sink)
    sink.on_notification(wire.NOTIFY_RUN_START, {"workflowId": "w1", "workflowName": "x"})
    sink.on_notification(
        wire.NOTIFY_NODE_START,
        {"workflowId": "w1", "nodeId": "a", "type": "step", "sessionId": "step-1"},
    )
    if reader:
        router.note_chat_session("chat-1", None, mode=lambda: ("normal", "normal"))
    return router


def _answer_to(proc, request_id: int) -> dict | None:
    """The reply written for *request_id*; the router's own requests go out too."""
    return next(
        (m for m in _written(proc) if m.get("id") == request_id and "method" not in m), None
    )


def _step_permission(request_id: int) -> dict:
    return {
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": "step-1",
            "toolCall": {"title": "ls"},
            "options": [
                {"optionId": "yes", "kind": "allow_once"},
                {"optionId": "no", "kind": "reject_once"},
            ],
        },
    }


@pytest.mark.asyncio
async def test_a_step_permission_with_no_chat_reader_is_rejected_on_the_wire(tmp_path):
    rt, reader, proc = _make_runtime()
    router = await _router_sink(rt, tmp_path, reader=False)
    task = await _start_reader(rt)
    try:
        _feed(reader, _step_permission(21))
        await async_wait_until(lambda: _answer_to(proc, 21))
        answer = _answer_to(proc, 21)
        assert answer["result"]["outcome"] == {"outcome": "selected", "optionId": "no"}
        assert router.approvals() == [], "nothing is held that nobody could answer"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_step_question_with_no_chat_reader_gets_no_answer_on_the_wire(
    tmp_path, monkeypatch
):
    from kiro_crew.kas_workflows import chat as kas_chat
    from kiro_crew.kas_workflows.asks import user_input_no_answer

    monkeypatch.setattr(kas_chat, "chat_workflows_enabled", lambda: True)
    rt, reader, proc = _make_runtime()
    await _router_sink(rt, tmp_path, reader=False)
    task = await _start_reader(rt)
    try:
        _feed(
            reader,
            {
                "id": 22,
                "method": "_kiro/userInput",
                "params": {"sessionId": "step-1", "question": "Proceed?"},
            },
        )
        await async_wait_until(lambda: _answer_to(proc, 22))
        answer = _answer_to(proc, 22)
        assert answer["result"] == user_input_no_answer(person=False)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_step_permission_is_claimed_and_held_while_its_chat_reads_it(tmp_path):
    rt, reader, proc = _make_runtime()
    router = await _router_sink(rt, tmp_path, reader=True)
    task = await _start_reader(rt)
    try:
        _feed(reader, _step_permission(23))
        await async_wait_until(lambda: router.approvals())
        assert not [m for m in _written(proc) if m.get("id") == 23], "held, not auto-answered"
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_a_chat_reader_evicted_after_the_run_started_no_longer_claims(tmp_path):
    from kiro_crew.kas_workflows.router import MAX_PARENT_SESSIONS

    rt, reader, proc = _make_runtime()
    router = await _router_sink(rt, tmp_path, reader=True)
    assert rt._workflow_sink.claims_step_request("step-1", "session/request_permission")
    # Newer chats push chat-1's registration out of the bounded map; the claim is
    # read at request time, so it follows.
    for n in range(MAX_PARENT_SESSIONS):
        router.note_chat_session(f"other-{n}", None, mode=lambda: ("normal", "normal"))
    task = await _start_reader(rt)
    try:
        _feed(reader, _step_permission(24))
        await async_wait_until(lambda: _answer_to(proc, 24))
        assert _answer_to(proc, 24)["result"]["outcome"]["optionId"] == "no"
        assert router.approvals() == []
    finally:
        await _stop_reader(task)


# ── An unread step question at the answer cap still gets its one answer ──


def _unread_question(request_id: int) -> dict:
    return {
        "id": request_id,
        "method": "_kiro/userInput",
        "params": {"sessionId": "nobody-reads-this", "question": "Proceed?"},
    }


@pytest.mark.asyncio
async def test_an_unread_question_at_the_answer_cap_is_answered_once_a_slot_frees(monkeypatch):
    from kiro_crew.kas_workflows import chat as kas_chat
    from kiro_crew.kas_workflows.asks import user_input_no_answer

    monkeypatch.setattr(kas_chat, "chat_workflows_enabled", lambda: True)
    rt, reader, proc = _make_runtime()
    rt._max_answer_tasks = 1
    blocker = asyncio.get_running_loop().create_future()
    rt._answer_tasks.add(blocker)
    task = await _start_reader(rt)
    try:
        _feed(reader, _unread_question(31))
        await asyncio.sleep(0)
        assert _written(proc) == [], "the cap is full: nothing is sent yet"
        blocker.set_result(None)
        await async_wait_until(lambda: _written(proc))
        [answer] = _written(proc)
        assert answer["id"] == 31
        assert answer["result"] == user_input_no_answer(person=False)
    finally:
        await _stop_reader(task)


@pytest.mark.asyncio
async def test_an_unread_question_at_a_cap_that_never_frees_condemns_the_runtime(monkeypatch):
    from kiro_crew.kas_workflows import chat as kas_chat

    monkeypatch.setattr(kas_chat, "chat_workflows_enabled", lambda: True)
    rt, reader, proc = _make_runtime()
    rt._max_answer_tasks = 1
    rt._answer_cap_wait_secs = 0.05
    blocker = asyncio.get_running_loop().create_future()
    rt._answer_tasks.add(blocker)
    task = await _start_reader(rt)
    try:
        _feed(reader, _unread_question(32))
        # A wedged pipe is not left holding the question: the runtime is marked
        # dead, which ends the asking step with it.
        await async_wait_until(lambda: rt._dead)
    finally:
        blocker.cancel()
        await _stop_reader(task)
