"""Claude Code's checklist reaches the task progress bar on the claude backend, read-only.

claude-agent-acp sends Claude Code's checklist (TodoWrite's list, or the Task* tools'
task list) as a legacy ACP ``plan`` update, because Crew declares no
``clientCapabilities.plan``. ``kiro_crew.acp.plan_checklist`` reads it on ``AcpClient``
into the ``EVENT_TODO_UPDATE`` kiro-cli's ``todo_list`` tool result becomes, so the
dashboard's one todo path draws it, marked ``read_only``: the harness keeps the list,
so the bar offers no toggle and no prompt block re-teaches it.

Two fixtures. ``plan-checklist.jsonl`` is written from the adapter's source (its header
cites the lines): three TodoWrite calls of one turn, a three-task list started, its
first task done and the second in progress, then the list emptied.
``plan-checklist-tasks.jsonl`` holds the eight ``plan`` frames of a live turn whose
Claude Code used the Task* tools: three tasks created and the first completed, each
change sent twice.

Section 1 replays the TodoWrite frames through the real ``AcpClient._dispatch_events``,
section 2 the Task* frames, section 3 the cases around them, section 4 the read-only
list on the slot, section 5 the gate, section 6 a real dashboard turn, section 7 the
spawn env that offers Claude Code its checklist tools.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from turn_harness import ScriptedProvider, TurnContext, TurnScript, run_turn

from kiro_crew.acp import plan_checklist
from kiro_crew.acp._dispatch import parse_todo_snapshot
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness import claude as claude_harness
from kiro_crew.acp.plan_checklist import plan_todo_snapshot
from kiro_crew.acp.types import (
    EVENT_COMPLETE,
    EVENT_TODO_UPDATE,
    STOP_REASON_END_TURN,
    TODO_TASKS_MAX,
    TODO_TEXT_MAX,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_PLAN_CHECKLIST,
)
from kiro_crew.agent_sdk.capabilities import capabilities_for
from kiro_crew.dashboard.state import _ChatSlot
from kiro_crew.testing.synthetic_credentials import github_classic
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS

FIXTURE = CORPUS / "claude" / "plan-checklist.jsonl"
TASKS_FIXTURE = CORPUS / "claude" / "plan-checklist-tasks.jsonl"
SESSION = "claude-session-1"

STARTED = {
    "description": "",
    "tasks": [
        {"id": "1", "text": "Running the test suite", "completed": False},
        {"id": "2", "text": "Fix the failing test", "completed": False},
        {"id": "3", "text": "Update the changelog", "completed": False},
    ],
    "read_only": True,
}
ONE_DONE = {
    "description": "",
    "tasks": [
        {"id": "1", "text": "Run the test suite", "completed": True},
        {"id": "2", "text": "Fixing the failing test", "completed": False},
        {"id": "3", "text": "Update the changelog", "completed": False},
    ],
    "read_only": True,
}
CLEARED: dict[str, Any] = {"description": "", "tasks": [], "read_only": True}


def _tasks(*rows: tuple[str, bool]) -> dict[str, Any]:
    return {
        "description": "",
        "tasks": [
            {"id": str(i + 1), "text": text, "completed": done}
            for i, (text, done) in enumerate(rows)
        ],
        "read_only": True,
    }


#: What the eight live Task* frames become, in order: each change arrives twice.
TASK_SNAPSHOTS = [
    _tasks(("Boil water", False)),
    _tasks(("Boil water", False)),
    _tasks(("Boil water", False), ("Steep tea bag", False)),
    _tasks(("Boil water", False), ("Steep tea bag", False)),
    _tasks(("Boil water", False), ("Steep tea bag", False), ("Add milk or sugar and serve", False)),
    _tasks(("Boil water", False), ("Steep tea bag", False), ("Add milk or sugar and serve", False)),
    _tasks(("Boil water", True), ("Steep tea bag", False), ("Add milk or sugar and serve", False)),
    _tasks(("Boil water", True), ("Steep tea bag", False), ("Add milk or sugar and serve", False)),
]


def _frames(fixture=FIXTURE) -> list[dict[str, Any]]:
    meta, frames = read_fixture(fixture)
    assert meta["backend"] == ACP_BACKEND_CLAUDE
    return frames


def _messages(frames: list[dict[str, Any]]) -> list[JsonRpcMessage]:
    return [JsonRpcMessage.from_dict(frame) for frame in frames]


def _client(backend: str = ACP_BACKEND_CLAUDE) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = SESSION
    return client


async def _turn(client: AcpClient, messages: list[JsonRpcMessage]) -> list[AcpEvent]:
    """Run one ``_dispatch_events`` turn over *messages*, then the prompt's answer."""
    frames: list[tuple[str, JsonRpcMessage]] = [("update", msg) for msg in messages]
    frames.append(("complete", JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN})))

    async def _fake_loop(req_id, timeout):
        for frame in frames:
            yield frame

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    return [
        event async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    ]


def _todos(events: list[AcpEvent]) -> list[dict[str, Any] | None]:
    return [event.todo for event in events if event.kind == EVENT_TODO_UPDATE]


def _slot() -> _ChatSlot:
    slot = _ChatSlot.__new__(_ChatSlot)
    slot._todo = None
    return slot


def _plan_frame(entries: list[Any], *, session_id: str = SESSION, meta: Any = None) -> dict:
    update: dict[str, Any] = {"sessionUpdate": "plan", "entries": entries}
    if meta is not None:
        update["_meta"] = meta
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }


# ── 1. The TodoWrite frames, through the real client ───────────────────────────


@pytest.mark.asyncio
async def test_the_recorded_plan_frames_become_todo_updates_in_order() -> None:
    events = await _turn(_client(), _messages(_frames()))
    assert _todos(events) == [STARTED, ONE_DONE, CLEARED]
    # The plan event carries no tool call: TodoWrite renders no tool row.
    assert all(not e.tool_call_id for e in events if e.kind == EVENT_TODO_UPDATE)
    assert events[-1].kind == EVENT_COMPLETE


@pytest.mark.asyncio
async def test_three_items_with_one_completed_keep_their_order() -> None:
    [todo] = _todos(await _turn(_client(), _messages(_frames()[1:2])))
    assert todo is not None
    assert [t["text"] for t in todo["tasks"]] == [
        "Run the test suite",
        "Fixing the failing test",
        "Update the changelog",
    ]
    assert [t["completed"] for t in todo["tasks"]] == [True, False, False]
    slot = _slot()
    assert slot.set_todo(todo) is True
    payload = slot.todo_payload()
    assert payload is not None
    assert (payload["completed"], payload["total"]) == (1, 3)


@pytest.mark.asyncio
async def test_the_in_progress_entry_is_open_and_is_the_current_task() -> None:
    """``in_progress`` is not done, and its activeForm text is what the bar names."""
    for frame, current in (
        (_frames()[0], "Running the test suite"),
        (_frames()[1], "Fixing the failing test"),
    ):
        [todo] = _todos(await _turn(_client(), _messages([frame])))
        slot = _slot()
        slot.set_todo(todo)
        payload = slot.todo_payload()
        assert payload is not None
        assert payload["current"] == current


@pytest.mark.asyncio
async def test_an_empty_plan_is_the_clear_the_todo_list_path_sends() -> None:
    """The adapter clears its plan with ``entries: []``; it hides the bar."""
    todos = _todos(await _turn(_client(), _messages(_frames())))
    assert todos[-1] == CLEARED
    # The same zero-task snapshot a kiro-cli todo_list result with no tasks makes,
    # apart from the read-only mark.
    kiro_empty = parse_todo_snapshot(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "call-1",
            "status": "completed",
            "rawOutput": {"items": [{"Json": {"tasks": [], "description": ""}}]},
        },
        {"call-1": "todo_list"},
    )
    assert todos[-1] == {**(kiro_empty or {}), "read_only": True}
    slot = _slot()
    for todo in todos:
        slot.set_todo(todo)
    payload = slot.todo_payload()
    assert payload is not None
    assert (payload["tasks"], payload["total"], payload["current"]) == ([], 0, "")


# ── 2. The live Task* frames, through the real client ──────────────────────────


@pytest.mark.asyncio
async def test_the_live_task_tool_frames_become_todo_updates_in_order() -> None:
    """Three TaskCreate calls and a TaskUpdate to completed, each sent twice."""
    events = await _turn(_client(), _messages(_frames(TASKS_FIXTURE)))
    assert _todos(events) == TASK_SNAPSHOTS
    assert all(not e.tool_call_id for e in events if e.kind == EVENT_TODO_UPDATE)
    # The slot's change test drops each repeat, so the bar moves four times.
    slot = _slot()
    assert [slot.set_todo(todo) for todo in _todos(events)] == [True, False] * 4
    payload = slot.todo_payload()
    assert payload is not None
    assert (payload["completed"], payload["total"], payload["current"]) == (1, 3, "Steep tea bag")
    assert payload["read_only"] is True


# ── 3. Around the recorded shapes ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_an_unknown_status_keeps_the_entry_open() -> None:
    frame = copy.deepcopy(_frames()[1])
    entries = frame["params"]["update"]["entries"]
    entries[2]["status"] = "cancelled"
    entries[1].pop("status")
    [todo] = _todos(await _turn(_client(), _messages([frame])))
    assert todo is not None
    assert [t["completed"] for t in todo["tasks"]] == [True, False, False]
    assert len(todo["tasks"]) == 3


@pytest.mark.asyncio
async def test_the_current_task_is_the_first_unfinished_entry_not_the_in_progress_one() -> None:
    """The bar names the first open entry, kiro's rule, even above an ``in_progress`` one."""
    frame = copy.deepcopy(_frames(TASKS_FIXTURE)[-1])
    entries = frame["params"]["update"]["entries"]
    entries[0]["status"] = "pending"
    entries[1]["status"] = "in_progress"
    [todo] = _todos(await _turn(_client(), _messages([frame])))
    slot = _slot()
    slot.set_todo(todo)
    payload = slot.todo_payload()
    assert payload is not None
    assert payload["current"] == "Boil water"
    assert payload["completed"] == 0


def test_a_non_object_entry_is_skipped_and_keeps_its_position() -> None:
    update = {"sessionUpdate": "plan", "entries": ["junk", {"content": 7, "status": "completed"}]}
    assert plan_todo_snapshot(update) == {
        "description": "",
        "tasks": [{"id": "2", "text": "7", "completed": True}],
        "read_only": True,
    }


@pytest.mark.parametrize(
    "update",
    [
        {"sessionUpdate": "plan"},
        {"sessionUpdate": "plan", "entries": {"content": "a"}},
        {"sessionUpdate": "tool_call", "entries": []},
        "plan",
    ],
)
def test_an_update_with_no_entries_list_is_not_a_plan(update: Any) -> None:
    assert plan_todo_snapshot(update) is None


@pytest.mark.asyncio
async def test_entry_text_is_redacted_and_bounded_like_a_todo_list_task() -> None:
    secret = github_classic()
    entries = [
        {"content": f"use key {secret}", "status": "pending", "priority": "medium"},
        {"content": "x" * (TODO_TEXT_MAX + 50), "status": "pending", "priority": "medium"},
    ] + [
        {"content": f"t{i}", "status": "pending", "priority": "medium"}
        for i in range(TODO_TASKS_MAX)
    ]
    [todo] = _todos(await _turn(_client(), _messages([_plan_frame(entries)])))
    assert todo is not None
    assert len(todo["tasks"]) == TODO_TASKS_MAX
    assert secret not in todo["tasks"][0]["text"]
    assert len(todo["tasks"][1]["text"]) == TODO_TEXT_MAX


@pytest.mark.asyncio
async def test_a_subagents_todowrite_plan_never_becomes_the_parents_checklist() -> None:
    """The adapter sends a child's TodoWrite list on the child's session, or stamped
    with the Agent call it serves; neither is the parent's bar."""
    child_session = _plan_frame([{"content": "c", "status": "pending"}], session_id="child-1")
    stamped = _plan_frame(
        [{"content": "c", "status": "pending"}],
        meta={"claudeCode": {"parentToolUseId": "toolu_agent_1"}},
    )
    events = await _turn(_client(), _messages([child_session, stamped]))
    assert _todos(events) == []


def test_a_plan_frame_naming_another_session_carries_no_event() -> None:
    """``todo_event``'s own session guard, apart from the native-children consumer."""
    client = _client()
    [own_frame] = _frames(TASKS_FIXTURE)[-1:]
    own = plan_checklist.todo_event(client, JsonRpcMessage.from_dict(own_frame))
    assert own is not None and own.todo == TASK_SNAPSHOTS[-1]
    foreign_frame = copy.deepcopy(own_frame)
    foreign_frame["params"]["sessionId"] = "another-session"
    assert plan_checklist.todo_event(client, JsonRpcMessage.from_dict(foreign_frame)) is None


# ── 4. The read-only list on the slot ──────────────────────────────────────────


def test_the_payload_marks_a_plan_list_read_only_and_a_todo_list_list_not() -> None:
    slot = _slot()
    slot.set_todo(TASK_SNAPSHOTS[-1])
    payload = slot.todo_payload()
    assert payload is not None and payload["read_only"] is True
    kiro = _slot()
    kiro.set_todo({"description": "", "tasks": [{"id": "1", "text": "a", "completed": False}]})
    assert "read_only" not in (kiro.todo_payload() or {})


def test_a_tick_on_a_read_only_list_writes_nothing() -> None:
    slot = _slot()
    slot._todo_overrides = {}
    slot.set_todo(TASK_SNAPSHOTS[-1])
    assert slot.set_todo_task_completed("2", True) is False
    assert slot._todo_overrides == {}
    assert slot.todo_payload()["completed"] == 1  # type: ignore[index]


def test_a_read_only_snapshot_drops_a_tick_held_over_an_editable_list() -> None:
    """A person's mark from a ``todo_list`` list cannot outrank the harness's own list."""
    slot = _slot()
    slot._todo_overrides = {}
    slot.set_todo(
        {"description": "", "tasks": [{"id": "1", "text": "Boil water", "completed": False}]}
    )
    assert slot.set_todo_task_completed("1", True) is True
    slot.set_todo(TASK_SNAPSHOTS[0])
    assert slot._todo_overrides == {}
    payload = slot.todo_payload()
    assert payload is not None
    assert payload["tasks"] == [{"id": "1", "text": "Boil water", "completed": False}]


# ── 5. The gate ────────────────────────────────────────────────────────────────


def test_the_plan_checklist_set_names_claude_alone() -> None:
    assert ACP_BACKENDS_PLAN_CHECKLIST == frozenset({ACP_BACKEND_CLAUDE})
    # The question the dashboard asks of a turn's provider translates the same set.
    assert capabilities_for(ACP_BACKEND_CLAUDE).keeps_own_checklist is True
    for backend in (ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX, "nope"):
        assert capabilities_for(backend).keeps_own_checklist is False


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE, ACP_BACKEND_CODEX])
async def test_a_harness_outside_the_set_reads_no_checklist_from_a_plan(backend: str) -> None:
    events = await _turn(_client(backend), _messages(_frames()))
    assert _todos(events) == []
    assert events[-1].kind == EVENT_COMPLETE


# ── 6. A dashboard turn draws the bar ──────────────────────────────────────────


class _ClaudeProvider(ScriptedProvider):
    """A scripted provider whose capabilities are claude's."""

    capabilities = capabilities_for(ACP_BACKEND_CLAUDE)


class _KiroProvider(ScriptedProvider):
    """A scripted provider whose capabilities are kiro-cli's."""

    capabilities = capabilities_for(ACP_BACKEND_KIRO)


@pytest.mark.asyncio
async def test_a_dashboard_turn_sends_the_bar_one_read_only_todo_update_per_change() -> None:
    events = await _turn(_client(), _messages(_frames()))
    todo_events = [event for event in events if event.kind == EVENT_TODO_UPDATE]
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    record = await run_turn(TurnScript(events=[*todo_events, done], provider=_ClaudeProvider))
    sent = [frame.payload["todo"] for frame in record.frames("todo_update")]
    assert [(t["total"], t["completed"], t["current"]) for t in sent] == [
        (3, 0, "Running the test suite"),
        (3, 1, "Fixing the failing test"),
        (0, 0, ""),
    ]
    assert sent[1]["tasks"] == ONE_DONE["tasks"]
    assert all(t["read_only"] is True for t in sent)


@pytest.mark.asyncio
async def test_a_dashboard_turn_drops_the_repeated_task_frames() -> None:
    events = await _turn(_client(), _messages(_frames(TASKS_FIXTURE)))
    todo_events = [event for event in events if event.kind == EVENT_TODO_UPDATE]
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    record = await run_turn(TurnScript(events=[*todo_events, done], provider=_ClaudeProvider))
    sent = [frame.payload["todo"] for frame in record.frames("todo_update")]
    assert [(t["total"], t["completed"], t["current"]) for t in sent] == [
        (1, 0, "Boil water"),
        (2, 0, "Boil water"),
        (3, 0, "Boil water"),
        (3, 1, "Steep tea bag"),
    ]


def _seed_editable_list(ctx: TurnContext) -> None:
    """A ``todo_list``-shaped list the slot holds from before this session started."""
    ctx.slot.set_todo(
        {
            "description": "Config workflow",
            "tasks": [
                {"id": "1", "text": "read runbook", "completed": True},
                {"id": "2", "text": "RCA SC", "completed": False},
            ],
        }
    )


def _prompt_sent(record: Any) -> str:
    [call] = [call for call in record.provider_calls if call.name == "stream"]
    return str(call.args[0])


@pytest.mark.asyncio
async def test_a_cold_start_on_claude_carries_no_todo_list_recovery_block() -> None:
    """The block tells the agent to call ``todo_list``, which Claude Code does not have."""
    done = AcpEvent(kind=EVENT_COMPLETE, stop_reason=STOP_REASON_END_TURN)
    claude = await run_turn(
        TurnScript(events=[done], provider=_ClaudeProvider, setup=_seed_editable_list)
    )
    assert "[Task checklist" not in _prompt_sent(claude)
    # The same cold start on kiro-cli carries it, so the check above measures the gate.
    kiro = await run_turn(
        TurnScript(events=[done], provider=_KiroProvider, setup=_seed_editable_list)
    )
    assert "[Task checklist — automatic recovery]" in _prompt_sent(kiro)


# ── 7. Claude Code is asked for its checklist tools ────────────────────────────


def _spawn_env(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> dict[str, str]:
    monkeypatch.setattr(claude_harness, "_resolve_claude_code_executable", lambda: "/opt/claude")
    # The thinking default reads the host's Claude Code settings files.
    monkeypatch.setattr(claude_harness, "a_source_decides_thinking", lambda *_a, **_k: False)
    claude_harness.ClaudeLaunch().apply_spawn_env(env)
    return env


def test_a_claude_spawn_turns_the_checklist_tools_on(monkeypatch: pytest.MonkeyPatch) -> None:
    env = _spawn_env(monkeypatch, {})
    assert env[claude_harness.CLAUDE_TODO_TOOLS_ENV] == "1"
    assert claude_harness.CLAUDE_TODO_TOOLS_ENV == "CLAUDE_CODE_ENABLE_TODO_TOOLS"


@pytest.mark.parametrize("operator_value", ["0", "", "1"])
def test_the_operators_checklist_tools_value_wins(
    monkeypatch: pytest.MonkeyPatch, operator_value: str
) -> None:
    env = _spawn_env(monkeypatch, {claude_harness.CLAUDE_TODO_TOOLS_ENV: operator_value})
    assert env[claude_harness.CLAUDE_TODO_TOOLS_ENV] == operator_value


def test_the_default_holds_beside_an_operators_claude_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The executable override returns early; the env defaults are set before it."""
    env = _spawn_env(monkeypatch, {"CLAUDE_CODE_EXECUTABLE": "/srv/claude"})
    assert env == {
        "CLAUDE_CODE_EXECUTABLE": "/srv/claude",
        claude_harness.CLAUDE_TODO_TOOLS_ENV: "1",
        claude_harness.CLAUDE_THINKING_DISPLAY_ENV: "summarized",
    }
