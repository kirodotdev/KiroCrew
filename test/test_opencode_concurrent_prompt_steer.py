"""opencode user steer as a second ``session/prompt`` (``ACP_BACKENDS_STEER_CONCURRENT_PROMPT``).

opencode 1.19.0 has no steer verb. A second ``session/prompt`` sent while a turn
runs writes the user message and joins the running loop, which reads it before
its next step; both requests are answered at the same idle. These tests drive a
real ``AcpClient`` read loop over a scripted stdout and pin what the client may
report: consumed only when the steer's answer comes with the turn's own clean
end, and otherwise nothing, so the caller queues the text.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp import client as acp_client
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_STEER_CONSUMED,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    METHOD_CANCEL,
    METHOD_PROMPT,
    AcpEvent,
)
from kiro_crew.agent_sdk import backend_cards
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_STEER,
    ACP_BACKENDS_STEER_CONCURRENT_PROMPT,
)
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.testing.wait import await_until, until_parked

_SID = "ses_probe"
_STEER = "end your answer with PINEAPPLE"
_WRAPPED = f"<user_message>\n{_STEER}\n</user_message>"


def _frame(payload: dict[str, Any]) -> bytes:
    return (json.dumps({"jsonrpc": "2.0", **payload}) + "\n").encode()


def _update(update: dict[str, Any]) -> bytes:
    return _frame({"method": "session/update", "params": {"sessionId": _SID, "update": update}})


def _chunk(text: str) -> bytes:
    return _update(
        {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


def _tool_call() -> bytes:
    return _update(
        {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "bash", "status": "pending"}
    )


def _answer(rid: int, stop: str = "end_turn") -> bytes:
    return _frame({"id": rid, "result": {"stopReason": stop, "usage": {"totalTokens": 7}}})


class _Harness:
    """An opencode ``AcpClient`` whose stdout is scripted and whose writes are captured.

    ``on_prompt`` is called with the id of each ``session/prompt`` written and
    returns the frames the fake harness answers with.
    """

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
        self.client._session_id = _SID
        self.client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
        self.client._send_response = AsyncMock()  # type: ignore[method-assign]
        self.reader = asyncio.StreamReader()
        proc = MagicMock()
        proc.stdout = self.reader
        proc.stdin = MagicMock()
        proc.returncode = None
        self.client._process = proc
        self.prompts: list[tuple[int, dict]] = []
        self.cancels = 0
        self.on_prompt: Callable[[int], list[bytes]] = lambda rid: []
        # When set, a steer's write (any prompt after the first) parks on it after
        # its frames are fed, the way a real write parks on ``drain()``.
        self.write_gate: asyncio.Event | None = None

        async def _write(data: bytes, request_id: int, method: str) -> None:
            frame = json.loads(data)
            if frame["method"] == METHOD_PROMPT:
                self.prompts.append((request_id, frame["params"]))
                for raw in self.on_prompt(request_id):
                    self.reader.feed_data(raw)
                if self.write_gate is not None and len(self.prompts) > 1:
                    await await_until(self.write_gate, what="the parked steer write's release")

        async def _cancel() -> None:
            self.cancels += 1
            for raw in self.on_cancel():
                self.reader.feed_data(raw)

        self.on_cancel: Callable[[], list[bytes]] = lambda: []
        self.client._write_request_bounded = _write  # type: ignore[method-assign]
        self.client._write_steer_cancel = _cancel  # type: ignore[method-assign]
        monkeypatch.setattr(acp_client, "_CONCURRENT_STEER_GRACE_SECS", 0.2)
        monkeypatch.setattr(acp_client, "_CONCURRENT_STEER_CANCEL_SECS", 0.5)

    async def run_turn(self, on_event: Callable[[Any], Any] | None = None) -> list[Any]:
        events = []
        async for event in self.client.stream_events("first", timeout=30.0):
            events.append(event)
            if on_event is not None:
                await on_event(event)
            if event.kind == EVENT_COMPLETE:
                break
        return events


def _kinds(events: list[Any]) -> list[str]:
    return [e.kind for e in events]


# ── membership and what each driver advertises ──


def test_opencode_is_the_only_member() -> None:
    assert ACP_BACKENDS_STEER_CONCURRENT_PROMPT == frozenset({ACP_BACKEND_OPENCODE})
    assert not ACP_BACKENDS_STEER_CONCURRENT_PROMPT & ACP_BACKENDS_STEER


@pytest.mark.parametrize(
    "backend,steer,refusal,loss",
    [
        (ACP_BACKEND_OPENCODE, True, False, True),
        (ACP_BACKEND_KIRO, True, True, False),
        (ACP_BACKEND_CLAUDE, False, False, True),
    ],
)
def test_the_client_answers_steer_from_the_tables(
    tmp_path: Path, backend: str, steer: bool, refusal: bool, loss: bool
) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    assert client.supports_steer is steer
    assert client.supports_refusal_steer is refusal
    assert client.steer_needs_loss_recovery is loss


def test_the_provider_wrapper_refuses_an_opencode_steer(tmp_path: Path) -> None:
    """Only the dashboard composer keeps the pending entry a lost steer needs."""
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    assert provider.supports_steer is False
    assert asyncio.run(provider.steer("hello")) is False


def test_the_card_line_is_on_for_opencode() -> None:
    for backend in (ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX, ACP_BACKEND_KIRO):
        card = backend_cards.card_for(backend)
        line = {entry.id: entry.available for entry in card.capabilities}
        assert line[backend_cards.LINE_MID_TURN_STEER] is True, backend


# ── a steer the running turn read ──


@pytest.mark.asyncio
async def test_a_steer_answered_with_the_turn_is_reported_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    turn_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        # The steer joined the running loop: one answer to both, at one idle.
        return [_chunk("done PINEAPPLE"), _answer(turn_id[0]), _answer(rid)]

    h.on_prompt = _on_prompt
    steered: list[bool] = []

    async def _steer_on_tool(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL and not steered:
            steered.append(await h.client.steer(_STEER))

    events = await h.run_turn(_steer_on_tool)

    assert steered == [True]
    steer_params = h.prompts[1][1]
    assert steer_params == {"sessionId": _SID, "prompt": [{"type": "text", "text": _STEER}]}
    consumed = [e for e in events if e.kind == EVENT_STEER_CONSUMED]
    assert [e.text for e in consumed] == [_WRAPPED]
    assert _kinds(events).index(EVENT_STEER_CONSUMED) < _kinds(events).index(EVENT_COMPLETE)
    assert h.cancels == 0


@pytest.mark.asyncio
async def test_a_steer_answer_read_before_the_turn_terminal_still_counts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode orders the two answers of one idle in no particular way."""
    h = _Harness(tmp_path, monkeypatch)
    turn_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        return [_chunk("ok"), _answer(rid), _answer(turn_id[0])]

    h.on_prompt = _on_prompt

    async def _steer(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL:
            await h.client.steer(_STEER)

    events = await h.run_turn(_steer)
    assert [e.text for e in events if e.kind == EVENT_STEER_CONSUMED] == [_WRAPPED]


# ── a steer the turn did not read ──


@pytest.mark.asyncio
async def test_a_steer_whose_turn_ends_first_is_cancelled_and_not_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn ended before the steer landed: it runs a loop of its own.

    That loop's output comes before the steer's answer. Nothing is reported
    consumed, the loop is cancelled, and none of its text reaches this turn.
    """
    h = _Harness(tmp_path, monkeypatch)
    turn_id: list[int] = []
    steer_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        steer_id.append(rid)
        return [_answer(turn_id[0]), _chunk("its own loop")]

    h.on_prompt = _on_prompt
    h.on_cancel = lambda: [_answer(steer_id[0], "cancelled")]

    async def _steer(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL:
            await h.client.steer(_STEER)

    events = await h.run_turn(_steer)

    assert EVENT_STEER_CONSUMED not in _kinds(events)
    assert h.cancels == 1
    assert all(e.text != "its own loop" for e in events if e.kind == EVENT_TEXT_CHUNK)
    assert h.client._orphan_steers == set()


@pytest.mark.asyncio
async def test_a_steer_answered_after_another_loops_output_is_not_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    turn_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        return [_answer(turn_id[0]), _chunk("its own loop"), _answer(rid)]

    h.on_prompt = _on_prompt

    async def _steer(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL:
            await h.client.steer(_STEER)

    events = await h.run_turn(_steer)
    assert EVENT_STEER_CONSUMED not in _kinds(events)
    assert h.cancels == 0


@pytest.mark.asyncio
async def test_a_turn_with_a_denied_approval_reports_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """opencode's loop stops on a denial, before it reads what came after it."""
    h = _Harness(tmp_path, monkeypatch)
    turn_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        return [_answer(turn_id[0]), _answer(rid)]

    h.on_prompt = _on_prompt

    async def _steer_then_deny(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL:
            await h.client.steer(_STEER)
            await h.client.reject_tool("perm-1")

    events = await h.run_turn(_steer_then_deny)
    assert EVENT_STEER_CONSUMED not in _kinds(events)


@pytest.mark.asyncio
async def test_a_refused_steer_is_not_consumed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A build that refuses a second prompt answers it with an error at once."""
    h = _Harness(tmp_path, monkeypatch)
    turn_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        refusal = _frame(
            {"id": rid, "error": {"code": -32603, "message": "already has an active prompt"}}
        )
        return [refusal, _chunk("ok"), _answer(turn_id[0])]

    h.on_prompt = _on_prompt

    async def _steer(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL:
            await h.client.steer(_STEER)

    events = await h.run_turn(_steer)
    assert EVENT_STEER_CONSUMED not in _kinds(events)
    assert _kinds(events)[-1] == EVENT_COMPLETE


# ── when nothing is sent ──


@pytest.mark.asyncio
async def test_no_steer_is_sent_without_a_running_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    assert await h.client.steer(_STEER) is False
    assert h.prompts == []


@pytest.mark.asyncio
async def test_a_steer_past_the_bound_is_not_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = _Harness(tmp_path, monkeypatch)
    turn_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        turn_id.append(rid)
        return [_tool_call()] if len(turn_id) == 1 else []

    h.on_prompt = _on_prompt
    results: list[bool] = []

    async def _steer(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL:
            results.append(await h.client.steer("x" * (acp_client._MAX_CONCURRENT_STEER_CHARS + 1)))
            h.reader.feed_data(_answer(turn_id[0]))

    await h.run_turn(_steer)
    assert results == [False]
    assert len(h.prompts) == 1


@pytest.mark.asyncio
async def test_a_steer_settled_while_its_write_was_parked_is_owed_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The turn ends and is settled while the steer's write still waits on drain.

    The settle read the steer's answer, so nothing is owed for it: no orphan, and
    the next prompt neither cancels nor kills the process. The steer is not
    reported consumed either, because its ``steer()`` had not returned True yet,
    and that call now returns False, so the caller queues it.
    """
    h = _Harness(tmp_path, monkeypatch)
    h.write_gate = asyncio.Event()
    h.client._kill_process = AsyncMock()  # type: ignore[method-assign]
    turn_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        if len(turn_id) == 1:
            turn_id.append(rid)
            return [_chunk("ok"), _answer(turn_id[0]), _answer(rid)]
        return [_chunk("second answer"), _answer(rid)]

    h.on_prompt = _on_prompt
    steers: list[asyncio.Task] = []

    async def _steer_from_another_task(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL:
            steers.append(asyncio.ensure_future(h.client.steer(_STEER)))
            await until_parked(h.write_gate)

    try:
        events = await h.run_turn(_steer_from_another_task)
    finally:
        h.write_gate.set()
    assert EVENT_STEER_CONSUMED not in _kinds(events)
    assert await await_until(steers[0], what="the parked steer call") is False
    assert h.client._orphan_steers == set()

    second = await h.run_turn()
    assert [e.text for e in second if e.kind == EVENT_TEXT_CHUNK] == ["second answer"]
    assert h.cancels == 0
    h.client._kill_process.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_settle_cut_short_still_owes_the_unanswered_steer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stop or the turn ceiling landing in the grace window keeps the steer owed."""
    h = _Harness(tmp_path, monkeypatch)
    monkeypatch.setattr(acp_client, "_CONCURRENT_STEER_GRACE_SECS", 30.0)
    turn_id: list[int] = []
    steer_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        steer_id.append(rid)
        return [_answer(turn_id[0])]

    h.on_prompt = _on_prompt

    async def _steer(event: Any) -> None:
        if event.kind == EVENT_TOOL_CALL:
            await h.client.steer(_STEER)

    in_grace = asyncio.Event()
    read_answers = h.client._read_steer_answers

    async def _read_and_signal(*args: Any, **kwargs: Any) -> None:
        in_grace.set()
        await read_answers(*args, **kwargs)

    h.client._read_steer_answers = _read_and_signal  # type: ignore[method-assign]
    task = asyncio.ensure_future(h.run_turn(_steer))
    await await_until(in_grace, what="the settle's grace read")
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await await_until(task, what="the cancelled turn")
    assert h.client._orphan_steers == {steer_id[0]}


@pytest.mark.asyncio
async def test_closing_the_stream_early_owes_the_unanswered_steer_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A consumer that closes ``stream_events`` mid-turn records the steer before the next prompt."""
    h = _Harness(tmp_path, monkeypatch)
    turn_id: list[int] = []
    steer_id: list[int] = []

    def _on_prompt(rid: int) -> list[bytes]:
        if not turn_id:
            turn_id.append(rid)
            return [_tool_call()]
        steer_id.append(rid)
        return [_chunk("still working")]

    h.on_prompt = _on_prompt
    stream = h.client.stream_events("first", timeout=30.0)
    async for event in stream:
        if event.kind == EVENT_TOOL_CALL:
            assert await h.client.steer(_STEER) is True
        elif event.kind == EVENT_TEXT_CHUNK:
            break
    await stream.aclose()
    assert h.client._orphan_steers == {steer_id[0]}


@pytest.mark.asyncio
async def test_orphans_owed_by_a_retired_process_are_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh process never answers the old one's ids, so it is not killed for them."""
    h = _Harness(tmp_path, monkeypatch)
    h.client._add_orphan_steers({41})
    fresh = MagicMock()
    fresh.stdout = h.reader
    fresh.stdin = MagicMock()
    fresh.returncode = None
    h.client._process = fresh
    h.client._kill_process = AsyncMock()  # type: ignore[method-assign]
    h.on_prompt = lambda rid: [_answer(rid)]

    await h.run_turn()

    assert h.cancels == 0
    h.client._kill_process.assert_not_awaited()
    assert h.client._orphan_steers == set()


@pytest.mark.asyncio
async def test_the_steer_cancel_is_a_session_cancel_notification(tmp_path: Path) -> None:
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    client._session_id = _SID
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    proc.stdin.transport.get_write_buffer_size.return_value = 0
    client._process = proc

    await client._write_steer_cancel()

    written = b"".join(call.args[0] for call in proc.stdin.write.call_args_list)
    assert json.loads(written) == {
        "jsonrpc": "2.0",
        "method": METHOD_CANCEL,
        "params": {"sessionId": _SID},
    }


@pytest.mark.asyncio
async def test_a_steer_loop_that_ignores_cancel_retires_the_process_before_the_next_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Its late frames would read as the next turn's own, so no prompt is written."""
    h = _Harness(tmp_path, monkeypatch)
    h.client._add_orphan_steers({41})
    h.reader.feed_data(_chunk("its own loop, still running"))
    h.client._kill_process = AsyncMock()  # type: ignore[method-assign]

    with pytest.raises(acp_client.AcpProcessDied):
        await h.run_turn()

    assert h.cancels == 1
    h.client._kill_process.assert_awaited_once_with(force=True)
    assert h.prompts == []
    assert h.client._orphan_steers == set()


@pytest.mark.asyncio
async def test_an_approval_a_steer_loop_asks_for_is_audited_then_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled approval is a denial Crew made, so it gets its SEL record first."""
    h = _Harness(tmp_path, monkeypatch)
    order: list[str] = []
    records: list[dict[str, Any]] = []

    class _Log:
        def log_tool_invocation(self, **kwargs: Any) -> None:
            order.append("audit")
            records.append(kwargs)

    monkeypatch.setattr(acp_client.sel_module, "sel", lambda: _Log())

    async def _respond(request_id: Any, result: Any) -> None:
        order.append("answer")
        assert result == {"outcome": {"outcome": "cancelled"}}

    h.client._send_response = _respond  # type: ignore[method-assign]
    h.client._add_orphan_steers({41})
    permission = _frame(
        {
            "id": "perm-9",
            "method": "session/request_permission",
            "params": {"sessionId": _SID, "toolCall": {"toolCallId": "t9"}, "options": []},
        }
    )
    h.reader.feed_data(permission)
    h.on_cancel = lambda: [_answer(41, "cancelled")]
    h.on_prompt = lambda rid: [_answer(rid)]

    await h.run_turn()

    assert order == ["audit", "answer"]
    assert records[0]["outcome"] == "rejected_on_cancel"
    assert records[0]["request_id"] == "perm-9"
    assert records[0]["metadata"] == {"backend": ACP_BACKEND_OPENCODE, "cause": "steer_loop"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal",
    [
        AcpEvent(kind=EVENT_COMPLETE, stop_reason="end_turn", synthetic_completion=True),
        AcpEvent(kind=EVENT_COMPLETE, stop_reason=acp_client.STOP_REASON_COMPACTION_FAILED),
    ],
    ids=["stale-turn", "compaction-failed"],
)
async def test_a_synthesized_terminal_leaves_its_steers_owed(
    tmp_path: Path, terminal: AcpEvent
) -> None:
    """A terminal made after the read loop has no read lock to settle under."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_OPENCODE)
    client._process = MagicMock()

    async def _events(req_id: int, timeout: float) -> Any:
        client._steer_turn.steers[7] = _WRAPPED
        client._steer_turn.accepted.add(7)
        yield terminal

    client._dispatch_events = _events  # type: ignore[method-assign,assignment]
    settle = AsyncMock(return_value=[_WRAPPED])
    client._settle_concurrent_steers = settle  # type: ignore[method-assign]

    events = [e async for e in client._dispatch_prompt_events(1, 1.0)]

    settle.assert_not_awaited()
    assert _kinds(events) == [EVENT_COMPLETE]
    assert client._orphan_steers == {7}
