"""Claude Code's raw SDK task and retry messages reach the Subagents panel and notice rows.

A claude session asks claude-agent-acp for five ``system`` subtypes
(``_meta.claudeCode.emitRawSDKMessages``), forwarded as ``_claude/sdkMessage``
notifications that ``kiro_crew.acp.raw_sdk_tasks`` reads on ``AcpClient``: a Workflow
run's agents become native cards, a background shell or Monitor task's end a notice row,
and a turn's first API retry a warning row.

``raw-sdk-workflow.jsonl`` and ``raw-sdk-background-shell.jsonl`` are real
claude-agent-acp 0.107.0 frames over Claude Code 2.1.325 (their headers say what was
normalized). The ``api_retry`` frames follow the SDK's own ``SDKAPIRetryMessage``
(``@anthropic-ai/claude-agent-sdk`` 0.3.323 ``sdk.d.ts``): no recorded run retried.

Every frame is read off a stdout stream by the real ``AcpClient._read_message``, and a
turn is the real ``AcpClient._dispatch_events`` of a turn marked as the dashboard's
(``raw_sdk_tasks.expect_dashboard_turn``).
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import json
import types
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from turn_harness import Do, Raise, ScriptedProvider, TurnScript, run_turn

import kiro_crew.acp.client as acp_client
from kiro_crew.acp import raw_sdk_tasks
from kiro_crew.acp import raw_sdk_workflow_cards as cards
from kiro_crew.acp.client import AcpClient, AcpProcessDied
from kiro_crew.acp.client_native_children import NATIVE_CHILD_LABEL_CAP
from kiro_crew.acp.raw_sdk_tasks import (
    END_COMPLETED,
    END_FAILED,
    END_STOPPED,
    RAW_SDK_MESSAGE_FILTER,
    RetryNotice,
    TaskEndNotice,
)
from kiro_crew.acp.session_handle_native_children import (
    NATIVE_CHILD_NOT_RESUMABLE,
    NATIVE_CHILDREN_UNCHARGED_KIND,
)
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.acp.transport_framing import _STDOUT_BUFFER_LIMIT
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_HARNESS_NOTICE,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    METHOD_METADATA,
    STOP_REASON_CANCELLED,
    STOP_REASON_END_TURN,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_RAW_SDK_TASKS,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard import gateway_notice as gn
from kiro_crew.dashboard.chat_runner import (
    NATIVE_SUBAGENT_INTERRUPTED_ERROR,
    _carried_background_native,
    _keep_background_open,
    _native_crew_should_auto_approve,
    _native_subagent_close_all,
    _native_subagent_sync,
    _retain_terminal_native,
)
from kiro_crew.dashboard.harness_notice_rows import notice_row
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

WORKFLOW = CORPUS / "claude" / "raw-sdk-workflow.jsonl"
SHELL = CORPUS / "claude" / "raw-sdk-background-shell.jsonl"
WORKFLOW_SESSION = "claude-session-raw-sdk-workflow"
SHELL_SESSION = "claude-session-raw-sdk-shell"
SESSION = "claude-session-raw-sdk"
TASK = "wy57uompy"
#: A card is the run's task id and the agent's ``agentId``.
CARD_A = f"{TASK}:a03ccb341194641db"
#: Agent b was first reported queued, before it had an ``agentId``.
CARD_B = f"{TASK}:2"
NATIVE_A, NATIVE_B = f"native:{CARD_A}", f"native:{CARD_B}"
PROMPT = "Reply with exactly one word (a greeting). Do not use any tools."
#: The recorded background command.
SLEEP = "Sleep 8 seconds then print a message"


def _frames(path) -> list[dict[str, Any]]:
    meta, frames = read_fixture(path)
    assert meta["backend"] == ACP_BACKEND_CLAUDE and meta["recorded"] == "synthesized"
    return frames


def _split(frames: list[dict[str, Any]]) -> tuple[list[JsonRpcMessage], list[JsonRpcMessage]]:
    """The frames of the prompt's turn, and the ones read after its answer."""
    msgs = [JsonRpcMessage.from_dict(frame) for frame in frames]
    answer = next(i for i, m in enumerate(msgs) if m.method is None and m.id is not None)
    return msgs[:answer], msgs[answer + 1 :]


def _client(session: str = SESSION, backend: str = ACP_BACKEND_CLAUDE) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = session
    return client


def _live_client(tmp_path, session: str = SESSION) -> AcpClient:
    """A claude client whose prompt is sent at once and whose stdout the test feeds."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = session
    client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
    client._send_prompt = AsyncMock(return_value=1)  # type: ignore[method-assign]
    client._is_process_alive = lambda: True  # type: ignore[method-assign]
    return client


#: The prompt's answer, which ends a turn.
_ANSWER = JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN})


def _line(msg: JsonRpcMessage) -> bytes:
    frame: dict[str, Any] = {"jsonrpc": "2.0"}
    for key in ("id", "method", "params", "result", "error"):
        if getattr(msg, key) is not None:
            frame[key] = getattr(msg, key)
    return (json.dumps(frame) + "\n").encode()


def _feed(reader: asyncio.StreamReader, messages: list[JsonRpcMessage]) -> None:
    for msg in messages:
        reader.feed_data(_line(msg))


def _piped(client: AcpClient) -> asyncio.StreamReader:
    """A stdout stream *client* reads, with no idle reader, as large as the spawn's."""
    reader = asyncio.StreamReader(limit=_STDOUT_BUFFER_LIMIT)
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None
    client._process = proc
    return reader


async def _turn(client: AcpClient, messages: list[JsonRpcMessage]) -> list[AcpEvent]:
    """One dashboard ``_dispatch_events`` turn over *messages* and the prompt's answer,
    each read off *client*'s stdout and classified as the prompt loop does."""
    _feed(_piped(client), [*messages, _ANSWER])
    raw_sdk_tasks.expect_dashboard_turn(client)

    async def _loop(req_id, timeout):
        while True:
            msg = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)
            assert msg is not None
            action = client._process_message(msg, 1)
            yield action, msg
            if action == "complete":
                return

    client._prompt_loop = _loop  # type: ignore[assignment]
    return [e async for e in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)]


async def _launched(path=WORKFLOW) -> tuple[AcpClient, list[AcpEvent], list[JsonRpcMessage]]:
    """A client after the recorded prompt's turn, that turn's events, and the frames read
    after its answer."""
    first, after = _split(_frames(path))
    client = _client(SHELL_SESSION if path == SHELL else WORKFLOW_SESSION)
    return client, await _turn(client, first), after


def _rosters(events: list[AcpEvent]) -> list[list[dict[str, Any]]]:
    return [e.subagents or [] for e in events if e.kind == EVENT_SUBAGENT_LIST]


def _statuses(roster: list[dict[str, Any]]) -> dict[str, str]:
    return {row["sessionId"]: row["status"]["type"] for row in roster}


def _notices(events: list[AcpEvent]) -> list[Any]:
    return [e.harness_notice for e in events if e.kind == EVENT_HARNESS_NOTICE]


def _sdk(message: dict[str, Any], *, session: str = SESSION) -> JsonRpcMessage:
    body = {"type": "system", "session_id": session, "uuid": "probe", **message}
    return JsonRpcMessage(
        method="_claude/sdkMessage", params={"sessionId": session, "message": body}
    )


def _started(task_id: str, task_type: str, **fields: Any) -> JsonRpcMessage:
    return _sdk({"subtype": "task_started", "task_id": task_id, "task_type": task_type, **fields})


def _progress(task_id: str, rows: list[dict[str, Any]]) -> JsonRpcMessage:
    return _sdk({"subtype": "task_progress", "task_id": task_id, "workflow_progress": rows})


def _notified(task_id: str, status: str, **fields: Any) -> JsonRpcMessage:
    return _sdk({"subtype": "task_notification", "task_id": task_id, "status": status, **fields})


def _updated(task_id: str, **patch: Any) -> JsonRpcMessage:
    return _sdk({"subtype": "task_updated", "task_id": task_id, "patch": patch})


def _agent_row(index: int, state: str, **fields: Any) -> dict[str, Any]:
    return {"type": "workflow_agent", "index": index, "label": f"x{index}", "state": state} | fields


def _chunk(text: str, session: str = SESSION) -> JsonRpcMessage:
    """A reply chunk: a frame of the session's own work."""
    update = {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    return JsonRpcMessage(method="session/update", params={"sessionId": session, "update": update})


def _retry(attempt: int = 1, max_retries: int = 10, **fields: Any) -> JsonRpcMessage:
    fault = {"retry_delay_ms": 2000, "error_status": 529, "error": "overloaded", **fields}
    return _sdk({"subtype": "api_retry", "attempt": attempt, "max_retries": max_retries, **fault})


_OVERLOADED = RetryNotice(attempt=1, max_retries=10, reason="overloaded", status=529)


# ── 1. The opt-in ──


def test_a_claude_session_asks_for_the_five_subtypes_beside_its_options() -> None:
    assert ACP_BACKENDS_RAW_SDK_TASKS == {ACP_BACKEND_CLAUDE}
    meta = _client()._claude_session_meta()["claudeCode"]
    subtypes = ("task_started", "task_progress", "task_updated", "task_notification", "api_retry")
    assert meta["emitRawSDKMessages"] == [{"type": "system", "subtype": s} for s in subtypes]
    # A sibling of ``options``, never inside it, and a copy the caller may change.
    assert "emitRawSDKMessages" not in meta["options"]
    meta["emitRawSDKMessages"].clear()
    assert len(RAW_SDK_MESSAGE_FILTER) == 5


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN - ACP_BACKENDS_RAW_SDK_TASKS))
def test_no_other_backend_asks_for_raw_messages(backend: str) -> None:
    assert raw_sdk_tasks.session_meta_filter(backend) is None


LIVE = CORPUS / "claude" / "raw-sdk-tasks-live.jsonl"


@pytest.mark.asyncio
async def test_the_live_capture_shows_what_the_filter_asks_for_and_what_it_makes() -> None:
    """claude-agent-acp at the lane's pin, sent the filter on ``session/new``, forwards
    those subtypes and no others; replayed, they make a Workflow agent's card and a
    background command's notice."""
    meta, frames = read_fixture(LIVE)
    assert (meta["backend"], meta["recorded"]) == (ACP_BACKEND_CLAUDE, "live")
    sdk = [frame for frame in frames if frame.get("method") == "_claude/sdkMessage"]
    subtypes = {frame["params"]["message"]["subtype"] for frame in sdk}
    assert subtypes <= {entry["subtype"] for entry in RAW_SDK_MESSAGE_FILTER}
    assert {"task_started", "task_progress", "task_notification"} <= subtypes
    client = _client(sdk[0]["params"]["sessionId"])
    turn: list[JsonRpcMessage] = []
    events: list[AcpEvent] = []
    for frame in frames:
        msg = JsonRpcMessage.from_dict(frame)
        if msg.method is None and isinstance(msg.result, dict) and "stopReason" in msg.result:
            events += await _turn(client, turn)
            turn = []
        elif msg.method is not None:
            turn.append(msg)
    events += await _turn(client, turn)
    assert any(isinstance(notice, TaskEndNotice) for notice in _notices(events))
    rows = [row for roster in _rosters(events) for row in roster]
    assert any(row["background"] and row["status"]["type"] == "completed" for row in rows)


# ── 2. The recorded Workflow run, through the real client ──


@pytest.mark.asyncio
async def test_the_recorded_run_draws_a_card_per_agent_until_its_end() -> None:
    client, launched, after = await _launched()
    # The run's two progress frames arrive together, so the next frame draws one roster.
    assert [_statuses(r) for r in _rosters(launched)] == [{CARD_A: "completed", CARD_B: "working"}]
    a, b = _rosters(launched)[-1]
    assert a == {
        "sessionId": CARD_A,
        "sessionName": PROMPT,
        "agentName": "tiny-greet",
        "initialQuery": PROMPT,
        "status": {"type": "completed", "message": ""},
        "label": "Greet: a",
        "model": "probe-model",
        "result": "Hello",
        "background": True,
    }
    assert (b["label"], b["status"]) == ("Greet: b", {"type": "working", "message": ""})
    # The prompt's turn ends with b still running: the harness says so.
    assert raw_sdk_tasks.live_card_ids(client) == {CARD_B}
    ended = await _turn(client, after)
    # a was shown final, so only b is listed. Its later rows carry agentId
    # a9097f9d7de32956a; its card id stays the one it was first given.
    assert [_statuses(r) for r in _rosters(ended)] == [{CARD_B: "completed"}]
    assert raw_sdk_tasks.live_card_ids(client) == frozenset()
    # The launch and its end make no notice.
    assert _notices(launched) == _notices(ended) == []


# ── 3. Around the recorded shapes ──


@pytest.mark.parametrize(
    ("state", "status"),
    [
        ("start", {"type": "working", "message": ""}),
        ("progress", {"type": "working", "message": "Bash: npm test"}),
        ("done", {"type": "completed", "message": ""}),
        ("error", {"type": "failed", "message": "boom"}),
        ("a-state-this-build-does-not-know", {"type": "working", "message": "Bash: npm test"}),
    ],
)
def test_each_agent_state_maps_onto_a_card_status(state: str, status: dict[str, str]) -> None:
    run = cards.WorkflowRun(task_id=TASK)
    fields = {"lastToolName": "Bash", "lastToolSummary": "npm test", "error": "boom"}
    if state in ("start", "done"):
        fields = {}
    cards.merge_rows(run, [_agent_row(1, state, promptPreview="p", **fields)])
    assert run.agents[1].status == status


@pytest.mark.parametrize(
    ("ended", "status"),
    [
        (END_COMPLETED, {"type": "completed", "message": ""}),
        (END_FAILED, {"type": "failed", "message": "failed"}),
        (END_STOPPED, {"type": "failed", "message": "stopped"}),
    ],
)
@pytest.mark.asyncio
async def test_a_runs_end_gives_every_running_card_its_outcome(
    ended: str, status: dict[str, str]
) -> None:
    client = _client()
    await _turn(client, [_started("t1", "local_workflow", workflow_name="w")])
    rows = [_agent_row(1, "done", promptPreview="p"), _agent_row(2, "start", promptPreview="p")]
    await _turn(client, [_progress("t1", rows)])
    final = _rosters(await _turn(client, [_notified("t1", ended)]))
    assert final == [[{**final[0][0], "status": status}]]
    assert final[0][0]["sessionId"] == "t1:2"


@pytest.mark.asyncio
async def test_a_killed_patch_ends_the_run_and_the_notification_after_it_adds_nothing() -> None:
    client = _client()
    events = await _turn(
        client,
        [
            _started("t1", "local_workflow"),
            _progress("t1", [_agent_row(1, "start", promptPreview="p")]),
            _chunk("launched"),
            _updated("t1", status="killed"),
            _notified("t1", "stopped"),
            _progress("t1", [_agent_row(1, "done", promptPreview="p")]),
        ],
    )
    assert [_statuses(r) for r in _rosters(events)] == [{"t1:1": "working"}, {"t1:1": "failed"}]


@pytest.mark.asyncio
async def test_a_run_whose_start_was_not_read_is_known_by_its_rows() -> None:
    events = await _turn(_client(), [_progress("t9", [_agent_row(1, "start", label="x")])])
    assert [row["sessionId"] for row in _rosters(events)[0]] == ["t9:1"]
    # No prompt preview, so the label is the task.
    assert _rosters(events)[0][0]["initialQuery"] == "x"


@pytest.mark.asyncio
async def test_a_task_that_is_not_a_workflow_draws_no_card() -> None:
    events = await _turn(
        _client(),
        [
            _started("b1", "local_bash", is_backgrounded=True),
            _sdk({"subtype": "task_progress", "task_id": "b1", "description": "x"}),
            _started("s1", "local_agent"),
            _sdk({"subtype": "task_progress", "task_id": "s1", "summary": "reading"}),
        ],
    )
    assert _rosters(events) == []


@pytest.mark.asyncio
async def test_a_runs_agents_and_strings_are_bounded() -> None:
    long_label = "y" * (NATIVE_CHILD_LABEL_CAP * 3)
    rows = [
        _agent_row(i, "start", promptPreview=long_label)
        for i in range(cards.MAX_WORKFLOW_AGENTS + 5)
    ]
    roster = _rosters(await _turn(_client(), [_progress("t1", rows)]))[0]
    assert len(roster) == cards.MAX_WORKFLOW_AGENTS
    assert all(len(row["initialQuery"]) <= NATIVE_CHILD_LABEL_CAP for row in roster)


def test_rows_past_a_runs_bounds_are_counted_and_said_once(caplog) -> None:
    phases = [
        {"type": "workflow_phase", "index": i, "title": f"p{i}"}
        for i in range(cards.MAX_WORKFLOW_PHASES + 2)
    ]
    agents = [_agent_row(i, "start") for i in range(cards.MAX_WORKFLOW_AGENTS + 3)]
    run = cards.WorkflowRun(task_id=TASK)
    with caplog.at_level("WARNING", logger=cards.__name__):
        cards.merge_rows(run, phases + agents)
        cards.merge_rows(run, phases + agents)
    assert len(run.phases) == cards.MAX_WORKFLOW_PHASES
    assert len(run.agents) == cards.MAX_WORKFLOW_AGENTS
    assert run.refused == 5
    assert [r.getMessage().count("not drawn as cards") for r in caplog.records] == [1]


def test_a_full_queue_counts_what_it_drops_and_says_it_at_the_next_drain(caplog) -> None:
    client = _client()
    state = client._raw_sdk
    for i in range(raw_sdk_tasks.MAX_PENDING + 2):
        raw_sdk_tasks._queue(state, cards.WorkflowRun(task_id=f"t{i}"))
    assert (len(state.pending), state.dropped) == (raw_sdk_tasks.MAX_PENDING, 2)
    with caplog.at_level("WARNING", logger=raw_sdk_tasks.__name__):
        raw_sdk_tasks.pending_events(client)
        raw_sdk_tasks.pending_events(client)
    assert [r.getMessage().startswith("raw SDK tasks: 2 queued") for r in caplog.records] == [True]
    assert state.dropped == 0


@pytest.mark.asyncio
async def test_the_oldest_ended_task_makes_room_for_a_new_one() -> None:
    client = _client()
    messages = [_started(f"s{i}", "local_bash") for i in range(raw_sdk_tasks.MAX_TRACKED_TASKS)]
    messages.append(_notified("s3", "completed"))
    messages.append(_started("new", "local_bash", is_backgrounded=True))
    await _turn(client, messages)
    tasks = client._raw_sdk.tasks
    assert len(tasks) == raw_sdk_tasks.MAX_TRACKED_TASKS
    assert "s3" not in tasks and "new" in tasks


@pytest.mark.asyncio
async def test_a_resumed_runs_cached_agent_gets_a_card_of_its_own() -> None:
    """A resumed run's cached agent carries the ``agentId`` of the run it comes from."""
    row = _agent_row(1, "done", promptPreview="p", agentId="a03ccb341194641db")
    events = await _turn(
        _client(),
        [
            _started("t1", "local_workflow"),
            _progress("t1", [row]),
            _started("t2", "local_workflow"),
            _progress("t2", [{**row, "cached": True}]),
        ],
    )
    ids = {r["sessionId"] for roster in _rosters(events) for r in roster}
    assert ids == {"t1:a03ccb341194641db", "t2:a03ccb341194641db"}


# ── 4. The recorded background command ──


@pytest.mark.asyncio
async def test_the_recorded_background_command_ends_in_one_notice() -> None:
    client, launched, after = await _launched(SHELL)
    # The launch makes none: its tool row shows it.
    assert _notices(launched) == []
    # The summary repeats the task's name; only the exit code it adds is kept.
    assert _notices(await _turn(client, after)) == [
        TaskEndNotice(task=SLEEP, status=END_COMPLETED, detail="exit code 0")
    ]


@pytest.mark.parametrize(
    ("fields", "moved", "expected"),
    [
        # A foreground command ends without a notice; moved to the background, it gets one.
        ({}, False, []),
        ({}, True, [TaskEndNotice(task="ls", status=END_COMPLETED)]),
        # A housekeeping task never does.
        ({"is_backgrounded": True, "ambient": True}, False, []),
        ({"is_backgrounded": True, "skip_transcript": True}, False, []),
    ],
)
@pytest.mark.asyncio
async def test_only_a_background_task_of_the_user_ends_in_a_notice(
    fields: dict[str, Any], moved: bool, expected: list[TaskEndNotice]
) -> None:
    frames = [_started("b1", "local_bash", description="ls", **fields)]
    if moved:
        frames.append(_updated("b1", is_backgrounded=True))
    events = await _turn(_client(), [*frames, _notified("b1", "completed", summary="ls")])
    assert _notices(events) == expected


@pytest.mark.asyncio
async def test_a_failed_or_stopped_task_says_so_once_and_a_long_summary_is_left_out() -> None:
    events = await _turn(
        _client(),
        [
            _started("b1", "local_bash", description="npm test", is_backgrounded=True),
            _started("b2", "local_bash", description="tail -f log", is_backgrounded=True),
            _notified("b1", "failed", summary="x" * (raw_sdk_tasks.SUMMARY_MAX + 1)),
            _notified("b1", "failed", summary="again"),
            _notified("b2", "stopped", summary="tail stopped"),
        ],
    )
    assert _notices(events) == [
        TaskEndNotice(task="npm test", status=END_FAILED),
        TaskEndNotice(task="tail -f log", status=END_STOPPED, detail="tail stopped"),
    ]


@pytest.mark.asyncio
async def test_the_task_name_and_summary_are_redacted() -> None:
    secret = f"export AWS_SECRET_ACCESS_KEY={SAMPLE_AWS_SECRET}"
    events = await _turn(
        _client(),
        [
            _started("b1", "local_bash", description=secret, is_backgrounded=True),
            # A bare 40-character run is no credential; under its name it is one.
            _notified(
                "b1", "completed", summary=f"printed AWS_SECRET_ACCESS_KEY={SAMPLE_AWS_SECRET}"
            ),
        ],
    )
    (notice,) = _notices(events)
    assert SAMPLE_AWS_SECRET not in notice.task + notice.detail


@pytest.mark.parametrize(
    ("summary", "detail"),
    [
        ('Background command "npm test" completed (exit code 0)', "exit code 0"),
        ('Background command "npm test" failed with exit code 1', "exit code 1"),
        ('Background command "npm test" was stopped', ""),
        ('Monitor "npm test" script failed (exit 2)', "exit 2"),
        # A summary that does not name the task is kept whole when short.
        ("all 12 suites passed", "all 12 suites passed"),
        ("x" * (raw_sdk_tasks.SUMMARY_MAX + 1), ""),
    ],
)
@pytest.mark.asyncio
async def test_the_notice_keeps_only_what_the_summary_adds_to_the_task(
    summary: str, detail: str
) -> None:
    started = _started("b1", "local_bash", description="npm test", is_backgrounded=True)
    events = await _turn(_client(), [started, _notified("b1", "completed", summary=summary)])
    assert _notices(events) == [TaskEndNotice(task="npm test", status=END_COMPLETED, detail=detail)]


@pytest.mark.parametrize("owner", [{"owned_by_subagent": True}, {"parent_task_id": "a1b2c3"}])
@pytest.mark.asyncio
async def test_a_shell_an_agent_launched_makes_no_notice(owner: dict[str, Any]) -> None:
    """Claude Code reports its end to the agent that launched it (a subagent, or an
    agent inside a Workflow run), and the main model runs no cycle for it."""
    client = _client()
    started = _started("b1", "local_bash", description="npm test", is_backgrounded=True, **owner)
    events = await _turn(client, [started, _notified("b1", "completed", summary="done")])
    assert _notices(events) == []
    assert "b1" not in client._raw_sdk.tasks


# ── 5. Frames read between prompts ──


def _idle_client(client: AcpClient, on_turn=lambda _c: None) -> asyncio.StreamReader:
    """*client* reading a real stdout stream with the idle reader the claude spawn step
    installs, started; the stream it reads."""
    client._install_idle_reader()
    reader = _piped(client)
    client._process.stdin.drain = AsyncMock()
    client.on_agent_turn = on_turn
    client._maybe_start_idle_reader()
    return reader


async def _idle(reader: asyncio.StreamReader) -> None:
    """Wait until *reader*'s reader has read every line and waits for the next."""
    await async_wait_until(
        lambda: reader._waiter is not None and not reader._buffer, timeout=FALLBACK_TIMEOUT_SECS
    )


async def _read_idle(client: AcpClient, frames: list[JsonRpcMessage], on_turn=lambda _c: None):
    """*frames* read between prompts by *client*'s idle reader, which keeps reading and
    holds none of them, then stopped."""
    reader = _idle_client(client, on_turn)
    _feed(reader, frames)
    await _idle(reader)
    assert client._idle_reader is not None and not client._idle_reader.done()
    await client._stop_idle_reader()
    assert not client._buffer


@pytest.mark.asyncio
async def test_an_end_read_between_prompts_reaches_the_turn_claude_runs_for_it() -> None:
    client, _launch, after = await _launched(SHELL)
    turns: list[AcpClient] = []
    del client._prompt_loop  # the prompt's scripted loop; the next turn reads stdout
    _feed(_idle_client(client, turns.append), after)
    # The end itself starts nothing; the cycle's first chunk does.
    await async_wait_until(lambda: turns == [client], timeout=FALLBACK_TIMEOUT_SECS)
    raw_sdk_tasks.expect_dashboard_turn(client)
    async with contextlib.aclosing(
        client.stream_unsolicited(timeout=FALLBACK_TIMEOUT_SECS)
    ) as stream:
        events = [e async for e in stream]
    # Drawn before the turn's first frame, so the row lands above the reply.
    kinds = [e.kind for e in events if e.kind in (EVENT_HARNESS_NOTICE, EVENT_TEXT_CHUNK)]
    assert kinds[0] == EVENT_HARNESS_NOTICE and set(kinds[1:]) == {EVENT_TEXT_CHUNK}
    assert _notices(events)[0].task == SLEEP
    assert events[-1].kind == EVENT_COMPLETE
    await async_wait_until(lambda: client._stdout_claims == 0, timeout=FALLBACK_TIMEOUT_SECS)
    await client._stop_idle_reader()


@pytest.mark.asyncio
async def test_a_task_end_alone_waits_for_the_next_turn() -> None:
    """A stopped task's end is followed by no cycle, so it starts no turn: it waits in
    the task state for the next one."""
    turns: list[AcpClient] = []
    client = _client()
    await _read_idle(
        client,
        [
            _started("b1", "local_bash", description="tail -f log", is_backgrounded=True),
            _updated("b1", status="killed"),
            _notified("b1", "stopped"),
        ],
        turns.append,
    )
    assert turns == [] and client._raw_sdk.tasks["b1"].ended == END_STOPPED
    events = await _turn(client, [])
    assert _notices(events) == [TaskEndNotice(task="tail -f log", status=END_STOPPED)]


@pytest.mark.asyncio
async def test_a_burst_of_task_frames_never_fills_the_idle_readers_buffer() -> None:
    """Claude Code registers every foreground command of two seconds or more as a task,
    and the shells an agent launches too. A burst of them between prompts, past the
    buffer's capacity, is read into the state, and the next turn says only the
    background command's end."""
    turns: list[AcpClient] = []
    client = _client()
    burst = [_started("bg", "local_bash", description="sleep 8", is_backgrounded=True)]
    for i in range(2 * client._buffer.maxlen):
        burst += [
            _started(f"f{i}", "local_bash", description=f"npm test {i}"),
            _updated(f"f{i}", status="completed"),
            _notified(f"f{i}", "completed", summary=f'Background command "npm test {i}" done'),
            _started(f"w{i}", "local_bash", is_backgrounded=True, owned_by_subagent=True),
            _notified(f"w{i}", "completed", summary="agent's command"),
        ]
    burst.append(
        _notified("bg", "completed", summary='Background command "sleep 8" completed (exit code 0)')
    )
    await _read_idle(client, burst, turns.append)
    assert turns == []
    assert len(client._raw_sdk.tasks) <= raw_sdk_tasks.MAX_TRACKED_TASKS
    assert not any(task_id.startswith("w") for task_id in client._raw_sdk.tasks)
    events = await _turn(client, [])
    assert _notices(events) == [
        TaskEndNotice(task="sleep 8", status=END_COMPLETED, detail="exit code 0")
    ]


@pytest.mark.asyncio
async def test_retries_between_prompts_make_one_notice_in_the_next_turn() -> None:
    """The first retry waits; a later one and a sign-in failure make none."""
    client = _client()
    await _read_idle(
        client, [_retry(1, error="authentication_failed"), _retry(1), _retry(2), _retry(3)]
    )
    assert _notices(await _turn(client, [])) == [_OVERLOADED]


@pytest.mark.parametrize(
    "end",
    [
        pytest.param(lambda: _notified("t1", "completed"), id="notification"),
        pytest.param(lambda: _updated("t1", status="completed"), id="final patch"),
    ],
)
@pytest.mark.asyncio
async def test_a_run_read_between_prompts_reaches_the_next_turn_as_it_ended(end) -> None:
    """Every frame of the run is read into its cards as it arrives: a beat without rows
    (Claude Code throttles a progress-only change) leaves the rows before it, and rows
    after the run's end move no card, so the next turn draws the run as it ended."""
    client = _client()
    await _read_idle(
        client,
        [
            _started("t1", "local_workflow"),
            _progress("t1", [_agent_row(1, "start", promptPreview="p")]),
            _progress("t1", [_agent_row(1, "done", promptPreview="p", resultPreview="Hello")]),
            _sdk({"subtype": "task_progress", "task_id": "t1", "summary": "working"}),
            end(),
            _progress("t1", [_agent_row(1, "error", promptPreview="p", error="late")]),
        ],
    )
    rosters = _rosters(await _turn(client, []))
    assert [[(row["status"]["type"], row["result"]) for row in r] for r in rosters] == [
        [("completed", "Hello")]
    ]


#: Background work read between prompts: a background command's end, a Workflow run's
#: end with its closing rows, and a retry.
_BETWEEN = [
    _started("b1", "local_bash", description="npm test", is_backgrounded=True),
    _notified("b1", "completed", summary='Background command "npm test" completed (exit code 0)'),
    _notified(
        "t1",
        "completed",
        workflow_progress=[_agent_row(1, "done", promptPreview="p", resultPreview="Hello")],
    ),
    _retry(1),
]


async def _start_run(client: AcpClient) -> None:
    """A dashboard turn that starts Workflow run t1, which ``_BETWEEN`` ends."""
    row = _agent_row(1, "start", promptPreview="p")
    await _turn(client, [_started("t1", "local_workflow"), _progress("t1", [row])])


async def _next_turn_draws_between(client: AcpClient) -> None:
    """The next dashboard turn draws ``_BETWEEN`` above its first frame, in order."""
    events = await _turn(client, [_chunk("ok")])
    first_chunk = next(i for i, e in enumerate(events) if e.kind == EVENT_TEXT_CHUNK)
    assert [e.kind for e in events[:first_chunk]] == [
        EVENT_HARNESS_NOTICE,
        EVENT_SUBAGENT_LIST,
        EVENT_HARNESS_NOTICE,
    ]
    assert _notices(events) == [
        TaskEndNotice(task="npm test", status=END_COMPLETED, detail="exit code 0"),
        _OVERLOADED,
    ]
    assert [(row["status"]["type"], row["result"]) for row in _rosters(events)[0]] == [
        ("completed", "Hello")
    ]


async def _request(
    client: AcpClient, reader: asyncio.StreamReader, frames: list[JsonRpcMessage] = _BETWEEN
) -> None:
    """A model, mode or option switch between prompts, which reads stdout until its answer."""
    _feed(reader, [*frames, JsonRpcMessage(id=7, result={})])
    assert await client._wait_for_response(7, timeout=FALLBACK_TIMEOUT_SECS) == {}


async def _compaction_drain(client: AcpClient, reader: asyncio.StreamReader) -> None:
    """The drain after a compaction, which reads stdout until fresh usage arrives."""
    usage = JsonRpcMessage(
        method=METHOD_METADATA, params={"sessionId": SESSION, "contextUsagePercentage": 12.5}
    )
    _feed(reader, [*_BETWEEN, usage])
    await client._drain_post_compaction_metadata(grace=FALLBACK_TIMEOUT_SECS)


async def _cancelled_wait(client: AcpClient, reader: asyncio.StreamReader) -> None:
    """A wait for an answer that is cancelled once it has read the frames."""
    wait = asyncio.ensure_future(client._wait_for_response(9, timeout=FALLBACK_TIMEOUT_SECS))
    _feed(reader, _BETWEEN)
    await _idle(reader)
    wait.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await wait


@pytest.mark.parametrize(
    "read",
    [
        pytest.param(_request, id="request"),
        pytest.param(_compaction_drain, id="compaction drain"),
        pytest.param(_cancelled_wait, id="cancelled wait"),
    ],
)
@pytest.mark.asyncio
async def test_what_another_reader_reads_between_turns_reaches_the_next_turn(read) -> None:
    """Whichever reader reads Claude Code's background work, it is read into the task
    state and handed to that reader as nothing, so none reaches the buffer no turn reads."""
    client = _client()
    await _start_run(client)
    await read(client, _piped(client))
    assert client._mcp_notifications == [] and not client._buffer
    await _next_turn_draws_between(client)


# ── 6. Retries ──


@pytest.mark.asyncio
async def test_a_turn_says_its_first_retry_once() -> None:
    client = _client()
    assert _notices(await _turn(client, [_retry(1), _retry(2), _retry(3)])) == [_OVERLOADED]
    # The next turn says its own, the category as Claude Code names it.
    assert _notices(await _turn(client, [_retry(1, 10, error="rate_limit", error_status=429)])) == [
        RetryNotice(attempt=1, max_retries=10, reason="rate_limit", status=429)
    ]


@pytest.mark.parametrize(
    ("fields", "reason", "status"),
    [
        ({"error": "unknown"}, "", 529),
        ({"error": None, "error_status": None}, "", None),
        ({"error": "Not A Category!", "error_status": 503}, "", 503),
        ({"error": "server_error", "error_status": 99}, "server_error", None),
    ],
)
@pytest.mark.asyncio
async def test_the_notice_carries_the_bare_category_and_status(
    fields: dict, reason: str, status: int | None
) -> None:
    (notice,) = _notices(await _turn(_client(), [_retry(**fields)]))
    assert (notice.reason, notice.status) == (reason, status)


@pytest.mark.parametrize(
    "silent",
    [
        pytest.param([_retry(attempt=True), _retry(attempt="2")], id="malformed"),
        pytest.param([_retry(1, error="authentication_failed", error_status=401)], id="sign-in"),
        pytest.param([_retry(1, error="oauth_org_not_allowed", error_status=401)], id="org"),
    ],
)
@pytest.mark.asyncio
async def test_a_retry_that_says_nothing_leaves_the_turns_notice(silent) -> None:
    """A malformed retry says nothing, and so does a sign-in failure: Claude Code gives
    up after its first attempt and reports the sign-out, and the adapter shows no retry
    for it either. A later retry of the turn says its own."""
    (notice,) = _notices(await _turn(_client(), [*silent, _retry(2)]))
    assert notice == RetryNotice(attempt=2, max_retries=10, reason="overloaded", status=529)


# ── 7. The gate and malformed messages ──


def _system(subtype: str, **fields: Any) -> dict[str, Any]:
    return {"sessionId": SESSION, "message": {"type": "system", "subtype": subtype, **fields}}


@pytest.mark.parametrize(
    "params",
    [
        None,
        "not a dict",
        {"sessionId": SESSION},
        {"sessionId": SESSION, "message": "x"},
        {"sessionId": SESSION, "message": {"type": "assistant", "subtype": "task_started"}},
        _system("init"),
        _system("task_started"),
        _system("task_progress", task_id="../x"),
        _system("task_progress", task_id="t1", workflow_progress="rows"),
        _system(
            "task_progress",
            task_id="t1",
            workflow_progress=[None, 3, {"type": "workflow_agent", "index": True}],
        ),
        _system("task_updated", task_id="t1", patch=1),
    ],
)
@pytest.mark.asyncio
async def test_a_malformed_message_is_read_as_nothing(params: Any) -> None:
    msg = JsonRpcMessage(method="_claude/sdkMessage", params=params)
    client = _client()
    assert raw_sdk_tasks.take(client, msg)
    events = await _turn(client, [msg])
    assert [e.kind for e in events] == [EVENT_COMPLETE]
    assert not client._raw_sdk.pending


@pytest.mark.asyncio
async def test_a_message_of_another_session_is_read_as_nothing() -> None:
    client = _client()
    ours = [_started("t1", "local_workflow"), _progress("t1", [_agent_row(1, "start")]), _retry()]
    other = [_sdk(msg.params["message"], session="another-session") for msg in ours]
    events = await _turn(client, other)
    assert [e.kind for e in events] == [EVENT_COMPLETE]
    assert not client._raw_sdk.tasks


@pytest.mark.parametrize("backend", sorted(ACP_BACKENDS_KNOWN - ACP_BACKENDS_RAW_SDK_TASKS))
@pytest.mark.asyncio
async def test_another_backend_reads_the_notification_as_before(backend: str) -> None:
    """Its reader returns the frame to its caller, whose prompt loop skips it."""
    client = _client(backend=backend)
    msg = _progress("t1", [_agent_row(1, "start", promptPreview="p")])
    assert not raw_sdk_tasks.take(client, msg)
    _feed(_piped(client), [msg])
    read = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)
    assert read is not None and read.method == raw_sdk_tasks.METHOD_CLAUDE_SDK_MESSAGE
    assert client._process_message(read, 1) == "skip"
    assert raw_sdk_tasks.pending_events(client) == []
    assert raw_sdk_tasks.live_card_ids(client) == frozenset()


def test_a_request_with_the_method_is_not_read_as_a_notification() -> None:
    request = JsonRpcMessage(id=9, method="_claude/sdkMessage", params={})
    client = _client()
    assert not raw_sdk_tasks.take(client, request)
    assert client._process_message(request, 1) == "server_request_unknown"


def test_a_new_session_forgets_the_last_ones_tasks() -> None:
    client = _client()
    client._raw_sdk.tasks["t1"] = raw_sdk_tasks.TaskRecord(task_type="local_bash")
    client._store_session_config({})
    assert not client._raw_sdk.tasks


# ── 8. Dashboard turns and the card tracker ──


@pytest.fixture
def claude_provider() -> type[ScriptedProvider]:
    """A scripted provider with claude's capabilities, whose ``client`` is the harness:
    the backend and its task state."""
    harness = types.SimpleNamespace(
        backend=ACP_BACKEND_CLAUDE, _raw_sdk=raw_sdk_tasks.RawSdkState()
    )
    attrs = {"capabilities": capabilities_for(ACP_BACKEND_CLAUDE), "client": harness}
    return type("ClaudeProvider", (ScriptedProvider,), attrs)


def _now(provider: Any, state: raw_sdk_tasks.RawSdkState) -> Do:
    """At this point of the stream the harness's task state is *state*."""
    return Do(lambda _ctx: setattr(provider.client, "_raw_sdk", state))


_DONE = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
_MAX_TOKENS = AcpEvent(kind=EVENT_COMPLETE, stop_reason="max_tokens")


def _shown(events: list[AcpEvent]) -> list[AcpEvent]:
    return [e for e in events if e.kind != EVENT_COMPLETE]


def _payloads(record: Any, kind: str) -> list[dict[str, Any]]:
    return [frame.payload for frame in record.frames(kind)]


@pytest.mark.asyncio
async def test_a_dashboard_turn_keeps_the_running_agent_open_until_the_run_ends(
    claude_provider,
) -> None:
    client, launched, after = await _launched()
    running = copy.deepcopy(client._raw_sdk)
    ended = _shown(await _turn(client, after))
    finished = copy.deepcopy(client._raw_sdk)
    record = await run_turn(
        TurnScript(
            events=[_now(claude_provider, running), *_shown(launched), _DONE],
            provider=claude_provider,
            then=TurnScript(
                events=[_now(claude_provider, finished), *ended, _DONE], provider=claude_provider
            ),
        )
    )
    spawned = _payloads(record, "subagent_spawn")
    assert [(p["id"], p["label"], p["model"], p["agent"]) for p in spawned] == [
        (NATIVE_A, "Greet: a", "probe-model", "tiny-greet"),
        (NATIVE_B, "Greet: b", "probe-model", "tiny-greet"),
    ]
    # a finished in the turn; b still runs, so the turn's end leaves its card open.
    done = _payloads(record, "subagent_done")
    assert [(p["id"], p["error"], p["result"]) for p in done] == [(NATIVE_A, None, "Hello")]
    # The next turn carries b's card and closes it when the run says it ended.
    assert _payloads(record.then, "subagent_spawn") == []
    later = _payloads(record.then, "subagent_done")
    assert [(p["id"], p["error"], p["result"], p["label"]) for p in later] == [
        (NATIVE_B, None, "Hello", "Greet: b")
    ]


@pytest.mark.asyncio
async def test_a_dashboard_turn_draws_the_task_end_and_one_retry_as_notice_rows(
    claude_provider,
) -> None:
    client, _launch, after = await _launched(SHELL)
    ended = _shown(await _turn(client, after))
    retried = _shown(await _turn(_client(), [_retry(2), _retry(3)]))
    record = await run_turn(TurnScript(events=[*retried, *ended, _DONE], provider=claude_provider))
    rows = [(row["content"], row.get("meta") or {}) for row in record.rows("notice")]
    assert [content for content, _meta in rows] == [
        "⚠️ Claude is retrying a failed request (overloaded), attempt 2 of 10.",
        f"ℹ️ Background task completed: {SLEEP} — exit code 0",
    ]
    assert [meta.get(gn.GATEWAY_NOTICE_META_KEY) for _c, meta in rows] == [
        gn.GATEWAY_NOTICE_HARNESS_API_RETRY,
        gn.GATEWAY_NOTICE_HARNESS_TASK_COMPLETED,
    ]
    # The bare category and status, for the dashboard to word in the reader's language.
    assert [meta[gn.GATEWAY_NOTICE_ARGS_KEY] for _c, meta in rows] == [
        {"attempt": 2, "max": 10, "reason": "overloaded", "status": 529},
        {"task": SLEEP, "detail": "exit code 0"},
    ]
    # The cycle's own reply is still the reply.
    assert record.rows("assistant")


@pytest.mark.parametrize(
    ("status", "token", "glyph"),
    [
        (END_COMPLETED, gn.GATEWAY_NOTICE_HARNESS_TASK_COMPLETED, "ℹ️"),
        (END_FAILED, gn.GATEWAY_NOTICE_HARNESS_TASK_FAILED, "⚠️"),
        (END_STOPPED, gn.GATEWAY_NOTICE_HARNESS_TASK_STOPPED, "ℹ️"),
    ],
)
def test_each_outcome_has_its_own_token(status: str, token: str, glyph: str) -> None:
    content, meta = notice_row(TaskEndNotice(task="t", status=status))
    assert content.startswith(f"{glyph} Background task ")
    assert meta[gn.GATEWAY_NOTICE_META_KEY] == token
    assert meta[gn.GATEWAY_NOTICE_ARGS_KEY] == {"task": "t", "detail": ""}
    assert notice_row(TaskEndNotice(task="", status=status)) is None
    assert notice_row(TaskEndNotice(task="t", status="unknown")) is None


@pytest.mark.parametrize(
    ("reason", "status", "because"),
    [("rate_limit", 429, " (rate limit)"), ("", 503, " (HTTP 503)"), ("", None, "")],
)
def test_the_retry_row_words_the_category_else_the_status(reason, status, because) -> None:
    """The English row spells the category in words, else names the HTTP status; the
    arguments keep both bare for the dashboard to word."""
    content, meta = notice_row(RetryNotice(attempt=2, max_retries=9, reason=reason, status=status))
    assert content == f"⚠️ Claude is retrying a failed request{because}, attempt 2 of 9."
    assert meta[gn.GATEWAY_NOTICE_META_KEY] == gn.GATEWAY_NOTICE_HARNESS_API_RETRY
    bare = {} if status is None else {"status": status}
    assert meta[gn.GATEWAY_NOTICE_ARGS_KEY] == {"attempt": 2, "max": 9, "reason": reason, **bare}


def _state() -> MagicMock:
    return MagicMock()


def _slot() -> types.SimpleNamespace:
    return types.SimpleNamespace(
        key="slot-1", _native_subagent_tracker={}, _native_subagent_output={}, _acp_client=None
    )


def _background_row(sid: str, status: str = "working") -> cards.BackgroundCardRow:
    """A Workflow agent's roster row, as ``raw_sdk_workflow_cards.roster_row`` builds it."""
    return cards.BackgroundCardRow(
        sessionId=sid,
        sessionName="p",
        agentName="w",
        initialQuery="p",
        status={"type": status, "message": ""},
        label="Greet: a",
        model="probe-model",
        result="Hello",
        background=True,
    )


def _turn_of(state: Any, slot: Any, tracker: dict, client: Any) -> types.SimpleNamespace:
    """The parts of a dashboard turn ``_keep_background_open`` reads."""
    slot._acp_client = client
    slot._native_subagent_tracker = tracker
    return types.SimpleNamespace(state=state, slot=slot, _native_tracker=tracker)


def _running(card_id: str) -> raw_sdk_tasks.RawSdkState:
    """Task state in which one Workflow agent, *card_id*, still runs."""
    state = raw_sdk_tasks.RawSdkState()
    run = cards.WorkflowRun(task_id="t1")
    run.agents[1] = cards.WorkflowAgent(card_id=card_id, task="p")
    state.tasks["t1"] = raw_sdk_tasks.TaskRecord(task_type="local_workflow", workflow=run)
    return state


def _harness_running(card_id: str) -> types.SimpleNamespace:
    return types.SimpleNamespace(backend=ACP_BACKEND_CLAUDE, _raw_sdk=_running(card_id))


def test_a_background_card_is_never_closed_as_stale() -> None:
    state, slot, tracker = _state(), _slot(), {}
    _native_subagent_sync(state, slot, [_background_row("w1")], tracker)
    tracker["w1"]["last_activity"] = 0.0
    _native_subagent_sync(state, slot, [], tracker)
    assert not tracker["w1"]["done"]


def test_a_row_off_the_wire_carries_no_background_fields_in() -> None:
    """A kiro-cli ``list_update`` row is a plain dict: whatever it says, its card is an
    ordinary native card, with no label, model or row result, closed for staleness, kept
    open by nothing, and counted as an open native crew."""
    state, slot, tracker = _state(), _slot(), {}
    state.context_builder.hooks.auto_approve_subagent_tools = True
    kiro_row = dict(_background_row("k1"))
    _native_subagent_sync(state, slot, [kiro_row], tracker)
    (kind, spawn), _kw = state.broadcast_ws.call_args
    assert kind == "subagent_spawn" and "label" not in spawn and "model" not in spawn
    assert not tracker["k1"].get("background")
    assert _native_crew_should_auto_approve(tracker, state, slot) is True
    turn = _turn_of(state, slot, tracker, _harness_running("k1"))
    _keep_background_open(turn, STOP_REASON_END_TURN)
    assert not tracker["k1"].get("kept_open")
    tracker["k1"]["last_activity"] = 0.0
    _native_subagent_sync(state, slot, [], tracker)
    assert tracker["k1"]["done"] and tracker["k1"]["error"] == "timed out (no activity)"
    _native_subagent_sync(
        state, slot, [{**kiro_row, "sessionId": "k2", "status": {"type": "completed"}}], tracker
    )
    assert tracker["k2"]["result"] == "(output in chat)"


def test_a_background_card_is_kept_open_only_while_the_harness_runs_it() -> None:
    state, slot, tracker = _state(), _slot(), {}
    _native_subagent_sync(state, slot, [_background_row("w1"), _background_row("w2")], tracker)
    turn = _turn_of(state, slot, tracker, _harness_running("w1"))
    _keep_background_open(turn, STOP_REASON_END_TURN)
    _native_subagent_close_all(state, slot, tracker)
    assert (tracker["w1"]["done"], tracker["w2"]["done"]) == (False, True)
    # The one it does not run closes as interrupted, not as a clean completion.
    assert tracker["w2"]["error"] == NATIVE_SUBAGENT_INTERRUPTED_ERROR
    retained = _retain_terminal_native(tracker)
    assert set(retained) == {"w1", "w2"}
    carried = _carried_background_native(retained)
    assert set(carried) == {"w1", "w2"} and carried["w1"]["kept_open"] is False


def test_a_background_card_never_counts_as_an_active_native_crew() -> None:
    state, slot, tracker = _state(), _slot(), {}
    state.context_builder.hooks.auto_approve_subagent_tools = True
    _native_subagent_sync(state, slot, [_background_row("w1")], tracker)
    assert _native_crew_should_auto_approve(tracker, state, slot) is False
    tracker["kiro-child"] = {"done": False}
    assert _native_crew_should_auto_approve(tracker, state, slot) is True


def test_a_closed_background_card_is_not_drawn_again_by_a_later_roster() -> None:
    """A Stop closes the cards; the run's own end arrives later and draws none."""
    state, slot, tracker = _state(), _slot(), {}
    _native_subagent_sync(state, slot, [_background_row("w1")], tracker)
    _native_subagent_close_all(state, slot, tracker, error=NATIVE_SUBAGENT_INTERRUPTED_ERROR)
    next_turn = _carried_background_native(_retain_terminal_native(tracker))
    state.broadcast_ws.reset_mock()
    _native_subagent_sync(state, slot, [_background_row("w1", "failed")], next_turn)
    assert state.broadcast_ws.call_args_list == []


# ── 9. The turn's liveness clocks ──


def _ticking(client: AcpClient, clock: ManualClock, msg: JsonRpcMessage) -> None:
    """Give *client* a stdout whose every line is *msg*, read five seconds after the last
    on *clock*."""

    async def readline() -> bytes:
        clock.advance(5.0)
        return _line(msg)

    stdout = types.SimpleNamespace(readline=readline)
    client._process = types.SimpleNamespace(stdout=stdout, returncode=None)


@pytest.mark.asyncio
async def test_a_background_frame_is_read_past_and_moves_no_activity_clock(monkeypatch) -> None:
    """A read goes on past Claude Code's background work to the session's own next frame;
    one that meets only that work ends empty at its timeout, the activity clock unmoved."""
    client = _client()
    client._last_activity = 0.0
    beat = _progress("t1", [_agent_row(1, "progress", promptPreview="p")])
    _feed(_piped(client), [beat, _retry(1), _chunk("hi")])
    msg = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)
    assert msg is not None and msg.method == "session/update"
    assert client._last_activity > 0.0
    assert list(client._raw_sdk.pending) == [client._raw_sdk.tasks["t1"].workflow, _OVERLOADED]

    clock = ManualClock()
    clock.install(monkeypatch, acp_client)
    quiet = _client()
    quiet._last_activity = 0.0
    _ticking(quiet, clock, beat)
    start = clock.monotonic()
    assert await quiet._read_message(timeout=12.0) is None  # flake-ok: virtual-clock deadline
    assert clock.monotonic() - start == 15.0
    assert quiet._last_activity == 0.0


@pytest.mark.asyncio
async def test_a_stalled_tool_trips_the_watchdog_while_a_workflow_reports(monkeypatch) -> None:
    """None of a Workflow's reports is the open call's, so a foreground tool's stall is
    still caught: the loop reads none of them, and each read that meets only them ends
    at its timeout as a silent read."""
    clock = ManualClock()
    clock.install(monkeypatch, acp_client)
    monkeypatch.setattr(acp_client, "_TOOL_STALL_TIMEOUT", 60.0)
    client = _client()
    client._tool_dispatched = True
    client._last_activity = clock.monotonic()
    client._is_process_alive = lambda: True  # type: ignore[method-assign]
    client._kill_process = AsyncMock()  # type: ignore[method-assign]
    row = _agent_row(1, "progress", promptPreview="p", lastToolName="Bash")
    _ticking(client, clock, _progress("t1", [row]))
    start = clock.monotonic()
    actions: list[str] = []
    with pytest.raises(AcpProcessDied, match="tool stalled"):
        loop = client._prompt_loop(req_id=1, timeout=3600.0)  # flake-ok: virtual-clock deadline
        async for action, _msg in loop:
            actions.append(action)
    assert actions == []
    # Caught at the stall window, not at the turn's deadline.
    assert clock.monotonic() - start <= 60.0 + acp_client._READ_TIMEOUT + 5.0
    client._kill_process.assert_awaited_once()


# ── 10. Cards kept open, the reconnect snapshot and the text-only readers ──


@pytest.mark.parametrize(
    ("stop_reason", "kept"),
    [
        (STOP_REASON_END_TURN, True),
        ("max_tokens", True),
        ("refusal", True),
        (STOP_REASON_CANCELLED, False),
        (STOP_REASON_TOOL_STALL, False),
        (STOP_REASON_STALE_RECOVER, False),
    ],
)
def test_only_a_turn_crew_cut_short_closes_the_cards_the_harness_runs(
    stop_reason: str, kept: bool
) -> None:
    """A Stop or a recovery stops Claude Code's background work; an ending the harness
    chose sends no ``session/cancel`` and leaves the run going."""
    state, slot, tracker = _state(), _slot(), {}
    _native_subagent_sync(state, slot, [_background_row("w1")], tracker)
    _keep_background_open(_turn_of(state, slot, tracker, _harness_running("w1")), stop_reason)
    _native_subagent_close_all(state, slot, tracker, error=NATIVE_SUBAGENT_INTERRUPTED_ERROR)
    assert tracker["w1"]["done"] is not kept
    if not kept:
        assert tracker["w1"]["error"] == NATIVE_SUBAGENT_INTERRUPTED_ERROR


@pytest.mark.parametrize(
    ("runs", "end", "kept"),
    [
        pytest.param(True, lambda: _MAX_TOKENS, True, id="max tokens"),
        pytest.param(False, lambda: _DONE, False, id="run lost"),
        pytest.param(True, lambda: Raise(RuntimeError("provider failed")), True, id="error"),
        pytest.param(True, lambda: Raise(AcpError("bad", transient=False)), True, id="acp error"),
        pytest.param(True, lambda: Raise(AcpProcessDied("exited")), False, id="process died"),
        pytest.param(True, lambda: Raise(asyncio.CancelledError()), False, id="cancelled"),
    ],
)
@pytest.mark.asyncio
async def test_a_turns_end_keeps_open_only_the_cards_the_harness_runs(
    claude_provider, runs: bool, end, kept: bool
) -> None:
    """A landing on ``max_tokens`` keeps agent b's card open while the harness runs it,
    and so does a turn that ends on an error its retries did not clear: it gets no
    landing, and its release holds the cards to the landing's rule. A card the harness no
    longer runs, a cancel, or a dead process whose session the release resets closes it."""
    client, launched, _after = await _launched()
    state = copy.deepcopy(client._raw_sdk) if runs else raw_sdk_tasks.RawSdkState()
    slots: list[Any] = []
    record = await run_turn(
        TurnScript(
            events=[
                _now(claude_provider, state),
                Do(lambda ctx: slots.append(ctx.slot)),
                *_shown(launched),
                end(),
            ],
            provider=claude_provider,
        )
    )
    closed = [] if kept else [(NATIVE_B, NATIVE_SUBAGENT_INTERRUPTED_ERROR)]
    done = [(p["id"], p["error"]) for p in _payloads(record, "subagent_done")]
    assert done == [(NATIVE_A, None), *closed]
    (slot,) = slots
    assert slot._native_subagent_tracker[CARD_B]["done"] is not kept
    # An open background card is not a child the turn was left waiting on.
    assert slot._last_turn_children_announced is False


async def _kept_open(tmp_path) -> tuple[AcpClient, MagicMock, dict[str, dict]]:
    """A claude client whose Workflow run still runs agent b, and a slot tracker whose
    turn kept b's card open."""
    first, _after = _split(_frames(WORKFLOW))
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = WORKFLOW_SESSION
    roster = _rosters(await _turn(client, first))[-1]
    del client._prompt_loop  # the scripted loop; a later reader reads stdout
    state, slot, tracker = _state(), _slot(), {}
    _native_subagent_sync(state, slot, roster, tracker)
    _keep_background_open(_turn_of(state, slot, tracker, client), STOP_REASON_END_TURN)
    _native_subagent_close_all(state, slot, tracker)
    assert not tracker[CARD_B]["done"]
    state.broadcast_ws.reset_mock()
    return client, state, tracker


async def _process_ends(client: AcpClient, tracker: dict[str, dict]) -> None:
    """The process ends between prompts: the idle reader reads its stdout's end."""
    reader = _idle_client(client)
    client._process.returncode = 0
    reader.feed_eof()
    await async_wait_until(lambda: tracker[CARD_B]["done"], timeout=FALLBACK_TIMEOUT_SECS)


async def _session_replaced(client: AcpClient, _tracker: dict[str, dict]) -> None:
    client._store_session_config({})


async def _process_reset(client: AcpClient, _tracker: dict[str, dict]) -> None:
    client._reset_state()


@pytest.mark.parametrize(
    "lose",
    [
        pytest.param(_session_replaced, id="session replaced"),
        pytest.param(_process_reset, id="process reset"),
        pytest.param(_process_ends, id="process ends between prompts"),
    ],
)
@pytest.mark.asyncio
async def test_a_card_kept_open_closes_when_the_client_loses_its_run(tmp_path, lose) -> None:
    client, state, tracker = await _kept_open(tmp_path)
    await lose(client, tracker)
    assert tracker[CARD_B]["done"]
    assert tracker[CARD_B]["error"] == NATIVE_SUBAGENT_INTERRUPTED_ERROR
    (kind, payload), _kw = state.broadcast_ws.call_args
    assert (kind, payload["id"], payload["label"]) == ("subagent_done", NATIVE_B, "Greet: b")
    assert not client._raw_sdk.tasks


def test_a_reconnect_snapshot_carries_the_cards_label_and_model() -> None:
    state, slot, tracker = _state(), _slot(), {}
    state._slots = {slot.key: slot}
    slot._native_subagent_tracker = tracker
    rows = [_background_row("w1"), _background_row("w2", "completed")]
    _native_subagent_sync(state, slot, rows, tracker)
    snapshots = DashboardState.native_subagent_snapshots(state)
    assert {(s["id"], s["done"], s["label"], s["model"]) for s in snapshots} == {
        ("native:w1", False, "Greet: a", "probe-model"),
        ("native:w2", True, "Greet: a", "probe-model"),
    }


def _child_announced() -> JsonRpcMessage:
    """An Agent-tool child's ``subagent_update``: every reader lists the turn's cards."""
    update = {"sessionUpdate": "subagent_update", "sessionId": "child-0001"}
    update |= {"title": "general-purpose", "description": "count the files"}
    update["state"] = {"state": "running"}
    params = {"sessionId": WORKFLOW_SESSION, "update": update}
    return JsonRpcMessage(method="session/update", params=params)


@pytest.mark.parametrize("reader", ["send_message", "send_message_stream"])
@pytest.mark.asyncio
async def test_a_text_only_reader_keeps_the_state_and_marks_no_card_shown(
    tmp_path, reader: str
) -> None:
    """The Workflow turn and an Agent-tool child, replayed through a text-only reader:
    the run's state is kept, no run is listed and no card is marked shown, so the next
    dashboard turn still draws agent a's end, which this one never showed."""
    first, after = _split(_frames(WORKFLOW))
    client = _live_client(tmp_path, WORKFLOW_SESSION)
    _feed(_piped(client), [*first, _child_announced(), _ANSWER])
    if reader == "send_message":
        text = await client.send_message("go", timeout=FALLBACK_TIMEOUT_SECS)
    else:
        stream = client.send_message_stream("go", timeout=FALLBACK_TIMEOUT_SECS)
        text = "".join([chunk async for chunk in stream])
    assert text == "The workflow is running. I'll reply WFDONE once it finishes."
    run = client._raw_sdk.tasks[TASK].workflow
    assert run is not None and set(run.agents) == {1, 2}
    # The change waits for a dashboard turn; no card is marked shown.
    assert [item is run for item in client._raw_sdk.pending] == [True]
    assert not any(agent.final_listed for agent in run.agents.values())
    rosters = _rosters(await _turn(client, after))
    assert _statuses(rosters[-1])[CARD_A] == "completed"


@pytest.mark.asyncio
async def test_a_workflow_agent_is_a_native_child_of_its_session() -> None:
    """It has no conversation of its own: a resume of its card id is refused naming the
    parent, as an Agent-tool child's is, and the agents still running are counted at the
    parent as uncharged residency."""
    client, _launch, _after = await _launched()
    for card in (CARD_A, CARD_B, NATIVE_B):
        reason = client.native_child_resume_refusal(card)
        assert reason is not None and reason.startswith(f"{NATIVE_CHILD_NOT_RESUMABLE}: {card}")
        assert WORKFLOW_SESSION in reason
    assert client.native_child_resume_refusal(f"{TASK}:another") is None
    assert client.native_child_resume_refusal(TASK) is None
    budget = MagicMock()
    assert client.report_native_children(budget) == 1
    budget.report_uncharged.assert_called_once_with(
        NATIVE_CHILDREN_UNCHARGED_KIND, 1, label=WORKFLOW_SESSION
    )
