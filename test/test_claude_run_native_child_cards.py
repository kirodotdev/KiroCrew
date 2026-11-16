"""A Crew-spawned run on claude draws its own harness-native children under its card.

A run on claude has its own ``AcpClient``, which declares ``subagents`` as a chat
slot's does, so its turn yields the roster and activity of the children Claude Code
starts from it (an Agent-tool child, a Workflow agent). ``subagent_manager.
native_child_cards`` reconciles them with the dashboard's own card owners and relays
each frame to the gateway as one ``run_native_card`` event, named
``native:<run id>:<child>``.

Section 1 is the set and the capability, section 2 recorded claude frames replayed
through a real ``AcpProvider`` / ``AcpClient`` into the real run loop
(``SubagentManager._run_inner``), section 3 the bounds, section 4 how and when the
cards close, section 5 the run's Workflow agents, section 6 the other harnesses,
section 7 a Stop on a run child's card, and section 8 the reconnect replay.

The captures are ``claude_native_subagents/agent-task-0.94.0.jsonl`` (an Agent call
and its child), ``acp_frames/claude/raw-sdk-workflow.jsonl`` (a Workflow run of two
agents) and ``acp_frames/claude/raw-sdk-background-shell.jsonl`` (a background shell
whose end is a notice).
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from acp_frame_replay_harness import CORPUS, read_fixture
from subagent_helpers import _mock_ctx_builder_auto_spawn, _mock_sessions

from kiro_crew.acp import raw_sdk_tasks
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.raw_sdk_workflow_cards import BackgroundCardRow
from kiro_crew.acp.transport_framing import _STDOUT_BUFFER_LIMIT
from kiro_crew.acp.types import (
    EVENT_HARNESS_NOTICE,
    EVENT_PERMISSION_REQUEST,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    STOP_REASON_END_TURN,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_CODEX,
    ACP_BACKEND_KIRO,
    ACP_BACKEND_OPENCODE,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_RUN_NATIVE_CHILD_CARDS,
)
from kiro_crew.agent_sdk.capabilities import UNKNOWN_BACKEND_CAPABILITIES, capabilities_for
from kiro_crew.dashboard.chat_runner import (
    NATIVE_SUBAGENT_DONE_RESULT_CAP,
    NATIVE_SUBAGENT_DONE_TRUNC_MARKER,
    NATIVE_SUBAGENT_INTERRUPTED_ERROR,
    NATIVE_SUBAGENT_TERMINAL_TTL_SECS,
)
from kiro_crew.dashboard.handlers.messaging import api_spawn_delete
from kiro_crew.dashboard.native_child_stop import STOP_REQUESTED
from kiro_crew.dashboard.run_native_card_relay import relay_run_native_card
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.execution_context import execution_for_store
from kiro_crew.hooks import TOOL_ALLOW, ToolHookResult
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.subagent_manager import native_child_cards
from kiro_crew.subagent_manager.native_child_cards import (
    EVENTS,
    MAX_RUN_NATIVE_CARDS,
    RELAY_EVENT,
    RUN_CARD_OUTPUT_HARD,
    RUN_CARD_OUTPUT_TAIL,
    TOOL_LINE_MAX,
    RunNativeChildCards,
    close_for,
)
from kiro_crew.testing.synthetic_credentials import SAMPLE_AWS_SECRET
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, await_until

# ``SubagentManager`` refuses a run while the host looks short of memory, which is the
# runner's state, not this test's input.
pytestmark = pytest.mark.usefixtures("healthy_host_memory")

#: A run id as ``_mint_agent_id`` draws one: hex.
RUN = "0123456789abcdef"
AGENT_CAPTURE = (
    Path(__file__).parent / "fixtures" / "claude_native_subagents" / "agent-task-0.94.0.jsonl"
)
WORKFLOW = CORPUS / "claude" / "raw-sdk-workflow.jsonl"
SHELL = CORPUS / "claude" / "raw-sdk-background-shell.jsonl"
PARENT = "parent-0001"
CHILD = "child-0001"
CARD = f"native:{RUN}:{CHILD}"
TASK = "wy57uompy"
ROOT_TAB = "root-tab"


# ── helpers ─────────────────────────────────────────────────────────────────────


def _agent_frames() -> list[dict[str, Any]]:
    """The Agent capture's frames: a header line, then the turn's ``session/update``s."""
    lines = [json.loads(line) for line in AGENT_CAPTURE.read_text(encoding="utf-8").splitlines()]
    assert lines[0]["_meta"]["backend"] == ACP_BACKEND_CLAUDE
    return [line for line in lines[1:] if "method" in line]


def _session_of(frames: list[dict[str, Any]]) -> str:
    params = [frame.get("params") or {} for frame in frames]
    for p in params:
        if (p.get("update") or {}).get("sessionUpdate") == "subagent_update":
            return p["sessionId"]
    return next(p["sessionId"] for p in params if p.get("sessionId"))


def _scripted_client(
    frames: list[dict[str, Any]], backend: str, requests: tuple[dict[str, Any], ...] = ()
) -> AcpClient:
    """A real client whose next prompt reads *frames*, then the permission *requests*,
    then the prompt's answer."""
    client = AcpClient(acp_backend=backend)
    client._session_id = _session_of(frames)
    script = [("update", JsonRpcMessage.from_dict(frame)) for frame in frames]
    script += [("permission", JsonRpcMessage.from_dict(frame)) for frame in requests]
    script.append(("complete", JsonRpcMessage(id=1, result={"stopReason": STOP_REASON_END_TURN})))

    async def _fake_loop(req_id: int, timeout: float) -> AsyncIterator[tuple[str, Any]]:
        for item in script:
            yield item

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    return client


def _piped_client(frames: list[dict[str, Any]]) -> AcpClient:
    """A real claude client whose next prompt reads *frames* off its stdout, each taken
    and classified as the prompt loop does, then the prompt's answer."""
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    client._session_id = _session_of(frames)
    reader = asyncio.StreamReader(limit=_STDOUT_BUFFER_LIMIT)
    proc = MagicMock()
    proc.stdout = reader
    proc.returncode = None
    client._process = proc
    for frame in [*frames, {"jsonrpc": "2.0", "id": 1, "result": {"stopReason": "end_turn"}}]:
        reader.feed_data((json.dumps(frame) + "\n").encode())

    async def _loop(req_id: int, timeout: float) -> AsyncIterator[tuple[str, Any]]:
        while True:
            msg = await client._read_message(timeout=FALLBACK_TIMEOUT_SECS)
            assert msg is not None
            action = client._process_message(msg, 1)
            yield action, msg
            if action == "complete":
                return

    client._prompt_loop = _loop  # type: ignore[assignment]
    return client


def _provider(client: AcpClient) -> AcpProvider:
    """A real ``AcpProvider`` over *client*, which needs no process to start."""
    client.ensure_ready = AsyncMock()  # type: ignore[method-assign]
    client._send_prompt = AsyncMock(return_value=1)  # type: ignore[method-assign]
    client.approve_tool = AsyncMock(return_value=True)  # type: ignore[method-assign]
    client.reject_tool = AsyncMock()  # type: ignore[method-assign]
    client._is_process_alive = lambda: True  # type: ignore[method-assign]
    with patch("kiro_crew.providers.acp.AcpClient"):
        provider = AcpProvider(acp_backend=client.backend)
    provider._client = client
    provider._ready_confirmed_session = AsyncMock()  # type: ignore[method-assign]
    return provider


def _info(run_id: str = RUN) -> SubagentInfo:
    return SubagentInfo(
        execution_context=execution_for_store(""),
        id=run_id,
        task="ask a child",
        parent_session_key="subagent:fedcba9876543210",
        root_session_key=f"dashboard:{ROOT_TAB}",
    )


async def _run(
    client: AcpClient,
) -> tuple[SubagentInfo, list[tuple[str, dict[str, Any]]], SubagentManager]:
    """Run one subagent turn on *client*; the run, every event it fired, its manager."""
    sessions = _mock_sessions()
    sessions.get_or_create = AsyncMock(return_value=(_provider(client), True, False))
    ctx = _mock_ctx_builder_auto_spawn()
    ctx.hooks.auto_approve_subagent_tools = True
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx)
    fired: list[tuple[str, dict[str, Any]]] = []

    async def _on_event(kind: str, info: SubagentInfo, extra: dict | None = None) -> None:
        fired.append((kind, dict(extra or {})))

    info = _info()
    manager._log_spawned(info)
    with (
        patch.object(manager, "_fire_event", side_effect=_on_event),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        await manager._run_inner(info, f"subagent:{RUN}")
    return info, fired, manager


def _relays(fired: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
    return [(extra["etype"], extra["frame"]) for kind, extra in fired if kind == RELAY_EVENT]


class _Manager:
    """The manager surface the cards use: every event fired, in order."""

    def __init__(self) -> None:
        self.fired: list[tuple[str, dict[str, Any]]] = []

    async def _fire_event(self, etype: str, info: Any, extra: dict | None = None) -> None:
        self.fired.append((etype, dict(extra or {})))

    def relays(self) -> list[tuple[str, dict[str, Any]]]:
        return _relays(self.fired)


def _cards(backend: str = ACP_BACKEND_CLAUDE, client: Any = None) -> RunNativeChildCards:
    provider = SimpleNamespace(capabilities=capabilities_for(backend), client=client)
    return RunNativeChildCards(RUN, provider)


def _row(sid: str, status: str = "working", message: str = "", query: str = "do it") -> dict:
    return {
        "sessionId": sid,
        "sessionName": query,
        "agentName": "general-purpose",
        "initialQuery": query,
        "status": {"type": status, "message": message},
    }


def _roster(*rows: dict) -> AcpEvent:
    return AcpEvent(kind=EVENT_SUBAGENT_LIST, subagents=list(rows))


def _text(sid: str, text: str) -> AcpEvent:
    return AcpEvent(kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id=sid, text=text)


def _tool(sid: str, call: str, title: str) -> AcpEvent:
    return AcpEvent(
        kind=EVENT_SUBAGENT_ACTIVITY, sub_session_id=sid, tool_call_id=call, title=title
    )


async def _drawn(cards: RunNativeChildCards, *events: AcpEvent) -> _Manager:
    manager = _Manager()
    for event in events:
        await cards.on_event(manager, _info(), event)
    return manager


# ── 1. The set and the capability ───────────────────────────────────────────────


def test_only_claude_draws_a_runs_own_children() -> None:
    assert ACP_BACKENDS_RUN_NATIVE_CHILD_CARDS == frozenset({ACP_BACKEND_CLAUDE})
    for backend in ACP_BACKENDS_KNOWN:
        assert capabilities_for(backend).draws_run_native_child_cards is (
            backend in ACP_BACKENDS_RUN_NATIVE_CHILD_CARDS
        ), backend
    assert UNKNOWN_BACKEND_CAPABILITIES.draws_run_native_child_cards is False


def test_the_run_hands_over_the_roster_and_activity_only() -> None:
    """A harness notice a marked prompt drains has no card: it is never handed over."""
    assert EVENTS == (EVENT_SUBAGENT_LIST, EVENT_SUBAGENT_ACTIVITY)
    assert EVENT_HARNESS_NOTICE not in EVENTS


def test_a_card_keeps_the_chats_bounds() -> None:
    assert RUN_CARD_OUTPUT_TAIL == NATIVE_SUBAGENT_DONE_RESULT_CAP
    assert RUN_CARD_OUTPUT_HARD == 2 * RUN_CARD_OUTPUT_TAIL
    assert TOOL_LINE_MAX == 80


# ── 2. The run loop, over recorded frames ───────────────────────────────────────


@pytest.mark.asyncio
async def test_an_agent_child_gets_a_card_named_by_its_run() -> None:
    """The capture's child is drawn as its own card: spawned with its task and agent,
    its handback call as one tool line, and closed by its own ``end_turn``."""
    _info_, fired, _mgr = await _run(_scripted_client(_agent_frames(), ACP_BACKEND_CLAUDE))
    relays = _relays(fired)
    assert [(etype, frame["id"]) for etype, frame in relays] == [
        ("subagent_spawn", CARD),
        ("subagent_chunk", CARD),
        ("subagent_done", CARD),
    ]
    spawn, chunk, done = (frame for _etype, frame in relays)
    assert spawn == {"id": CARD, "task": "say pong", "agent": "general-purpose"}
    assert chunk == {"id": CARD, "text": "→ SubagentHandback\n"}
    assert done["error"] is None and done["result"] == "→ SubagentHandback\n"
    assert all("slot" not in frame for _etype, frame in relays)
    children = {extra["child"] for kind, extra in fired if kind == RELAY_EVENT}
    assert children == {CHILD}


@pytest.mark.asyncio
async def test_the_runs_own_card_is_unchanged() -> None:
    """The child's tool call is still listed as the run's, as before its card existed."""

    def _own(fired: list[tuple[str, dict[str, Any]]]) -> list[tuple[str, dict[str, Any]]]:
        return [(k, e) for k, e in fired if k in ("subagent_tool", "subagent_chunk")]

    drawn, drawn_fired, _m = await _run(_scripted_client(_agent_frames(), ACP_BACKEND_CLAUDE))
    with patch.object(
        native_child_cards, "capabilities_of", return_value=UNKNOWN_BACKEND_CAPABILITIES
    ):
        plain, plain_fired, _m = await _run(_scripted_client(_agent_frames(), ACP_BACKEND_CLAUDE))
    assert _relays(plain_fired) == []
    assert _own(drawn_fired) == _own(plain_fired)
    assert (drawn.tool_count, drawn.last_tool) == (plain.tool_count, plain.last_tool)


def _child_text_frames(*texts: str) -> list[dict[str, Any]]:
    """The capture's announce and delegated prompt, the child's *texts*, then its end."""
    frames = _agent_frames()
    end = next(i for i, f in enumerate(frames) if f["params"]["update"].get("messageId"))
    chunks = [
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": CHILD,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": text},
                },
            },
        }
        for text in texts
    ]
    finished = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": PARENT,
            "update": {
                "sessionUpdate": "subagent_update",
                "sessionId": CHILD,
                "state": {"state": "idle", "stopReason": "end_turn"},
            },
        },
    }
    return [*frames[: end + 1], *chunks, finished]


@pytest.mark.asyncio
async def test_a_childs_text_streams_redacted_to_its_card() -> None:
    """A credential split across two chunks is redacted on the card and in its result."""
    half = len(SAMPLE_AWS_SECRET) // 2
    frames = _child_text_frames(
        "PONG aws_secret_access_key=", SAMPLE_AWS_SECRET[:half], SAMPLE_AWS_SECRET[half:]
    )
    _i, fired, _m = await _run(_scripted_client(frames, ACP_BACKEND_CLAUDE))
    relays = _relays(fired)
    text = "".join(frame["text"] for etype, frame in relays if etype == "subagent_chunk")
    [done] = [frame for etype, frame in relays if etype == "subagent_done"]
    assert "PONG" in text and "PONG" in done["result"]
    assert SAMPLE_AWS_SECRET not in text and SAMPLE_AWS_SECRET[:half] not in done["result"]
    assert done["error"] is None


# ── 3. Bounds ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_child_past_the_bound_gets_no_card_and_is_counted_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    cards = _cards()
    rows = [_row(f"c{i}") for i in range(MAX_RUN_NATIVE_CARDS + 1)]
    manager = await _drawn(cards, _roster(*rows), _roster(*rows))
    spawned = [frame["id"] for etype, frame in manager.relays() if etype == "subagent_spawn"]
    assert len(spawned) == MAX_RUN_NATIVE_CARDS
    assert f"native:{RUN}:c{MAX_RUN_NATIVE_CARDS}" not in spawned
    assert cards.refused == 1 and len(cards.tracker) == MAX_RUN_NATIVE_CARDS
    with caplog.at_level(logging.WARNING, logger=native_child_cards.__name__):
        await cards.close(manager, _info(), completed=True)
    [warning] = [r for r in caplog.records if r.name == native_child_cards.__name__]
    assert "1 harness-native" in warning.getMessage()
    assert str(MAX_RUN_NATIVE_CARDS) in warning.getMessage()


@pytest.mark.asyncio
async def test_many_refused_children_are_each_counted_once_across_rosters() -> None:
    cards = _cards()
    extra = 200
    rows = [_row(f"c{i}") for i in range(MAX_RUN_NATIVE_CARDS + extra)]
    await _drawn(cards, _roster(*rows), _roster(*rows), _roster(*rows))
    assert cards.refused == extra


@pytest.mark.asyncio
async def test_a_cards_output_and_result_are_bounded() -> None:
    cards = _cards()
    chunk = "x" * 3_000
    manager = await _drawn(cards, _roster(_row("c1")), *[_text("c1", chunk)] * 10)
    held = "".join(cards.output[cards.tracker["c1"]["id"]])
    assert len(held) <= RUN_CARD_OUTPUT_HARD
    await cards.on_event(manager, _info(), _roster(_row("c1", "completed")))
    [done] = [frame for etype, frame in manager.relays() if etype == "subagent_done"]
    assert done["result"].startswith(NATIVE_SUBAGENT_DONE_TRUNC_MARKER)
    assert len(done["result"]) <= NATIVE_SUBAGENT_DONE_RESULT_CAP + len(
        NATIVE_SUBAGENT_DONE_TRUNC_MARKER
    )


@pytest.mark.asyncio
async def test_a_tool_line_is_redacted_and_cut() -> None:
    cards = _cards()
    title = f"curl -H 'Authorization: Bearer {SAMPLE_AWS_SECRET}' " + "y" * 200
    manager = await _drawn(cards, _roster(_row("c1")), _tool("c1", "t1", title))
    [chunk] = [frame for etype, frame in manager.relays() if etype == "subagent_chunk"]
    assert SAMPLE_AWS_SECRET not in chunk["text"]
    assert chunk["text"].startswith("→ ") and len(chunk["text"]) <= len("→ \n") + TOOL_LINE_MAX


@pytest.mark.asyncio
async def test_a_failed_child_closes_with_a_clipped_error() -> None:
    cards = _cards()
    message = "boom " * 80 + "(request_id: 0123-abcd)"
    manager = await _drawn(cards, _roster(_row("c1")), _roster(_row("c1", "failed", message)))
    [done] = [frame for etype, frame in manager.relays() if etype == "subagent_done"]
    assert len(done["error"]) <= 200 and done["error"].endswith("(request_id: 0123-abcd)")


@pytest.mark.asyncio
async def test_a_child_with_no_task_gets_no_card() -> None:
    cards = _cards()
    manager = await _drawn(cards, _roster(_row("c1", query="")))
    assert manager.relays() == []


# ── 4. How and when the cards close ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_completed_run_closes_its_open_cards_with_no_error() -> None:
    cards = _cards()
    manager = await _drawn(cards, _roster(_row("c1")))
    await cards.close(manager, _info(), completed=True)
    [done] = [frame for etype, frame in manager.relays() if etype == "subagent_done"]
    assert done["id"] == f"native:{RUN}:c1" and done["error"] is None
    assert cards.output == {} and cards.tracker["c1"]["done"] is True


@pytest.mark.asyncio
async def test_a_run_cut_short_puts_the_held_text_on_the_card_then_closes_it_interrupted() -> None:
    client = SimpleNamespace(flush_native_child_text=lambda: [_text("c1", "the tail")])
    cards = _cards(client=client)
    manager = await _drawn(cards, _roster(_row("c1")))
    await cards.close(manager, _info(), completed=False)
    assert [(etype, frame.get("text"), frame.get("error")) for etype, frame in manager.relays()][
        1:
    ] == [
        ("subagent_chunk", "the tail", None),
        ("subagent_done", None, NATIVE_SUBAGENT_INTERRUPTED_ERROR),
    ]


@pytest.mark.asyncio
async def test_a_workflow_agents_card_closes_interrupted_whenever_the_run_ends() -> None:
    cards = _cards()
    row = BackgroundCardRow(_row(f"{TASK}:a"), label="Greet: a", model="m", background=True)
    manager = await _drawn(cards, _roster(row))
    await cards.close(manager, _info(), completed=True)
    spawn, done = (frame for _etype, frame in manager.relays())
    assert spawn["id"] == f"native:{RUN}:{TASK}:a" and spawn["label"] == "Greet: a"
    assert done["error"] == NATIVE_SUBAGENT_INTERRUPTED_ERROR


@pytest.mark.asyncio
async def test_close_runs_once_and_draws_nothing_after() -> None:
    cards = _cards()
    manager = await _drawn(cards, _roster(_row("c1")))
    await cards.close(manager, _info(), completed=True)
    seen = len(manager.fired)
    await cards.close(manager, _info(), completed=False)
    await cards.on_event(manager, _info(), _roster(_row("c2")))
    assert len(manager.fired) == seen


@pytest.mark.asyncio
async def test_a_new_attempt_closes_what_the_last_one_left_open() -> None:
    info = _info()
    manager = _Manager()
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_CLAUDE), client=None)
    first = await RunNativeChildCards.start(manager, info, provider)
    await first.on_event(manager, info, _roster(_row("c1")))
    second = await RunNativeChildCards.start(manager, info, provider)
    assert info._native_child_cards is second and first.closed and not second.closed
    [done] = [frame for etype, frame in manager.relays() if etype == "subagent_done"]
    assert done["error"] == NATIVE_SUBAGENT_INTERRUPTED_ERROR


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [("", None), ("boom", NATIVE_SUBAGENT_INTERRUPTED_ERROR)],
    ids=["completed", "failed"],
)
async def test_the_runs_end_closes_its_childrens_cards_before_its_own_done(
    error: str, expected: str | None
) -> None:
    """Every terminal path fires ``subagent_done`` through ``_fire_event``, so the
    children's done frames always reach the gateway first."""
    seen: list[tuple[str, dict[str, Any]]] = []

    async def _on_event(kind: str, _info: Any, extra: dict) -> None:
        seen.append((kind, dict(extra)))

    manager = SubagentManager(
        sessions=_mock_sessions(), ctx_builder=_mock_ctx_builder_auto_spawn(), on_event=_on_event
    )
    info = _info()
    info.error = error
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_CLAUDE), client=None)
    cards = await RunNativeChildCards.start(manager, info, provider)
    await cards.on_event(manager, info, _roster(_row("c1")))
    await manager._fire_event("subagent_done", info, {"outcome": info.outcome})
    kinds = [(kind, extra.get("etype")) for kind, extra in seen]
    assert kinds == [
        (RELAY_EVENT, "subagent_spawn"),
        (RELAY_EVENT, "subagent_done"),
        ("subagent_done", None),
    ]
    assert seen[1][1]["frame"]["error"] == expected
    # The cards are closed for good: a second done adds no grandchild frame.
    await manager._fire_event("subagent_done", info, {})
    assert [kind for kind, _extra in seen[3:]] == ["subagent_done"]


@pytest.mark.asyncio
async def test_close_for_a_run_with_no_cards_does_nothing() -> None:
    manager = _Manager()
    await close_for(manager, _info())
    await close_for(manager, MagicMock())
    assert manager.fired == []


# ── 5. The run's Workflow agents ────────────────────────────────────────────────


def test_each_prompt_marks_the_runs_own_client_for_its_workflow_agents() -> None:
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    cards = _cards(client=client)
    assert raw_sdk_tasks.begin_draw(client) is False
    cards.begin_prompt()
    assert raw_sdk_tasks.begin_draw(client) is True


@pytest.mark.asyncio
async def test_the_runs_close_takes_the_mark_back() -> None:
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE)
    client.flush_native_child_text = lambda: []  # type: ignore[method-assign]
    cards = _cards(client=client)
    cards.begin_prompt()
    await cards.close(_Manager(), _info(), completed=True)
    assert raw_sdk_tasks.begin_draw(client) is False


@pytest.mark.asyncio
async def test_a_runs_workflow_agents_draw_under_its_card() -> None:
    """The recorded Workflow call's agents get cards named by the run, with the label
    and model their rows carry; the run's end closes the one still running."""
    meta, frames = read_fixture(WORKFLOW)
    assert meta["backend"] == ACP_BACKEND_CLAUDE
    answer = next(i for i, f in enumerate(frames) if "method" not in f and "id" in f)
    info, fired, manager = await _run(_piped_client(frames[:answer]))
    relays = _relays(fired)
    spawned = [frame for etype, frame in relays if etype == "subagent_spawn"]
    assert spawned and all(f["id"].startswith(f"native:{RUN}:{TASK}:") for f in spawned)
    assert {f["label"] for f in spawned} >= {"Greet: a"}
    assert all(f.get("model") for f in spawned)
    open_ids = {f["id"] for f in spawned} - {
        f["id"] for etype, f in relays if etype == "subagent_done"
    }
    assert open_ids
    closer = _Manager()
    await close_for(closer, info)
    closed = {f["id"]: f["error"] for etype, f in closer.relays() if etype == "subagent_done"}
    assert closed == dict.fromkeys(open_ids, NATIVE_SUBAGENT_INTERRUPTED_ERROR)


@pytest.mark.asyncio
@pytest.mark.parametrize("marked", [True, False], ids=["claude", "unmarked"])
async def test_a_task_end_notice_drawn_into_a_run_makes_no_card_frame(marked: bool) -> None:
    """The background shell's end is read as a notice: a marked run drains it and has no
    row for it, where an unmarked run leaves it waiting for a dashboard turn."""
    meta, frames = read_fixture(SHELL)
    assert meta["backend"] == ACP_BACKEND_CLAUDE
    answer = next(i for i, f in enumerate(frames) if "method" not in f and "id" in f)
    client = _piped_client(frames[:answer] + frames[answer + 1 :])
    caps = capabilities_for(ACP_BACKEND_CLAUDE if marked else ACP_BACKEND_KIRO)
    with patch.object(native_child_cards, "capabilities_of", return_value=caps):
        _info_, fired, _m = await _run(client)
    assert _relays(fired) == []
    assert len(client._raw_sdk.pending) == (0 if marked else 1)


# ── 6. The other harnesses ──────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_CODEX, ACP_BACKEND_OPENCODE])
async def test_another_harnesss_run_draws_no_child_card(backend: str) -> None:
    with patch.object(native_child_cards, "expect_dashboard_turn") as mark:
        cards = _cards(backend, client=MagicMock())
        cards.begin_prompt()
        manager = await _drawn(cards, _roster(_row("c1")), _text("c1", "hi"))
        await cards.close(manager, _info(), completed=True)
    assert manager.fired == [] and cards.tracker == {}
    mark.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", [ACP_BACKEND_KIRO, ACP_BACKEND_OPENCODE])
async def test_the_same_frames_on_another_harnesss_run_relay_nothing(backend: str) -> None:
    _info_, fired, _m = await _run(_scripted_client(_agent_frames(), backend))
    assert _relays(fired) == []


# ── 7. A Stop on a run child's card ─────────────────────────────────────────────


class _Stdin:
    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.drain = AsyncMock()

    def write(self, data: bytes) -> None:
        self.frames.append(json.loads(data.decode("utf-8")))


async def _gateway_with_a_running_child(
    *, grant_cancel: bool = True
) -> tuple[MagicMock, RunNativeChildCards, AcpClient, SubagentInfo]:
    """The run's client after the child's announce and prompt, its cards, and a gateway
    state whose registry holds the card the relay drew."""
    frames = _agent_frames()
    end = next(i for i, f in enumerate(frames) if f["params"]["update"].get("messageId"))
    frames = frames[: end + 1]
    if not grant_cancel:
        for frame in frames:
            frame["params"]["update"].pop("capabilities", None)
    client = _scripted_client(frames, ACP_BACKEND_CLAUDE)
    process = MagicMock()
    process.stdin = _Stdin()
    process.returncode = None
    client._process = process
    info = _info()
    manager = _Manager()
    provider = SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_CLAUDE), client=client)
    cards = await RunNativeChildCards.start(manager, info, provider)
    async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS):
        if event.kind in EVENTS:
            await cards.on_event(manager, info, event)
    state = MagicMock()
    state._native_cards = {}
    state.subagents.get = MagicMock(side_effect=lambda run_id: info if run_id == RUN else None)
    tab_client = MagicMock()
    tab_client.cancel_native_child = AsyncMock(return_value=True)
    state.get_slot.return_value = SimpleNamespace(_acp_client=tab_client)
    state.tab_client = tab_client
    coalescer = MagicMock()
    coalescer.handle.return_value = False
    for kind, extra in manager.fired:
        relay_run_native_card(state, coalescer, ROOT_TAB, RUN, extra)
    assert state._native_cards[CARD]["run_id"] == RUN
    return state, cards, client, info


async def _stop(state: Any) -> tuple[int, dict[str, Any]]:
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"agent_id": CARD}
    with patch("kiro_crew.dashboard.handlers.messaging._sel"):
        response = await api_spawn_delete(request)
    return response.status, json.loads(response.text)


@pytest.mark.asyncio
async def test_stop_on_a_run_childs_card_asks_the_runs_client() -> None:
    state, cards, client, info = await _gateway_with_a_running_child()
    assert await _stop(state) == (200, {"ok": True, "cancelled": True, "stopping": True})
    assert client._process.stdin.frames == [  # type: ignore[union-attr]
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": CHILD}}
    ]
    state.tab_client.cancel_native_child.assert_not_awaited()
    assert cards.tracker[CHILD][STOP_REQUESTED] is True
    manager = _Manager()
    cancelled = {"type": "failed", "message": "cancelled"}
    await cards.on_event(manager, info, _roster({**_row(CHILD), "status": cancelled}))
    [(etype, done)] = manager.relays()
    assert (etype, done["id"], done["stopped"], done["error"]) == (
        "subagent_done",
        CARD,
        True,
        None,
    )


@pytest.mark.asyncio
async def test_a_run_child_with_no_cancel_closes_alone_and_stays_closed() -> None:
    state, cards, client, info = await _gateway_with_a_running_child(grant_cancel=False)
    assert await _stop(state) == (200, {"ok": True, "cancelled": True})
    assert client._process.stdin.frames == []  # type: ignore[union-attr]
    state.tab_client.cancel_native_child.assert_not_awaited()
    [done] = [c.args[1] for c in state.broadcast_ws.call_args_list if c.args[0] == "subagent_done"]
    assert (done["id"], done["slot"], done["stopped"]) == (CARD, ROOT_TAB, True)
    record = cards.tracker[CHILD]
    assert (record["done"], record["stopped"], record["outcome"]) == (True, True, "stopped")
    manager = _Manager()
    await cards.close(manager, info, completed=True)
    assert manager.relays() == []


@pytest.mark.asyncio
async def test_a_stop_on_a_run_no_longer_held_asks_no_client() -> None:
    state, _cards_, client, _info_ = await _gateway_with_a_running_child()
    state.subagents.get = MagicMock(return_value=None)
    assert await _stop(state) == (200, {"ok": True, "cancelled": True})
    assert client._process.stdin.frames == []  # type: ignore[union-attr]
    state.tab_client.cancel_native_child.assert_not_awaited()


def _permission(request_id: int, *, child: bool) -> dict[str, Any]:
    """A permission request the run's client reads: its child's (on the child's
    session, stamped with the Agent call it serves) or the run's own."""
    meta = {"toolName": "Bash", **({"parentToolUseId": "toolu_0001"} if child else {})}
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": CHILD if child else PARENT,
            "toolCall": {
                "toolCallId": f"toolu_9{request_id:03d}",
                "name": "Bash",
                "status": "pending",
                "rawInput": {"command": f"touch {request_id}.txt"},
                "title": f"touch {request_id}.txt",
                "kind": "execute",
                "_meta": {"claudeCode": meta},
            },
            "options": [
                {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
                {"optionId": "reject", "name": "No", "kind": "reject_once"},
            ],
        },
    }


class _Person:
    """The gateway's approver: each request waits for its answer. A cancelled wait
    answers False, as the dashboard's own does (``ApprovalCoordinator.request``)."""

    def __init__(self) -> None:
        self.asked: dict[str, asyncio.Event] = {}
        self.answers: dict[str, asyncio.Future[bool]] = {}
        self.cancelled: list[str] = []

    def reached(self, request_id: str) -> asyncio.Event:
        return self.asked.setdefault(request_id, asyncio.Event())

    async def __call__(self, event: Any, _parent_session_key: str = "") -> bool:
        request_id = str(event.request_id)
        answer = self.answers.setdefault(request_id, asyncio.get_running_loop().create_future())
        self.reached(request_id).set()
        try:
            return await answer
        except asyncio.CancelledError:
            self.cancelled.append(request_id)
            return False


async def _run_asking(client: AcpClient, person: _Person, state: Any, info: SubagentInfo) -> None:
    """Run one turn on *client* whose requests are put to *person*, relaying each card
    frame to the gateway *state*."""
    sessions = _mock_sessions()
    sessions.get_or_create = AsyncMock(return_value=(_provider(client), True, False))
    sessions.get_approval_policy = MagicMock(return_value="ask")
    ctx = _mock_ctx_builder_auto_spawn()
    # Neither a grant nor a deny: the request goes to the person.
    ctx.hooks.on_tool_call = MagicMock(return_value=ToolHookResult(action=TOOL_ALLOW))
    ctx.hooks.auto_approve_subagent_tools = False
    manager = SubagentManager(sessions=sessions, ctx_builder=ctx, on_tool_approval=person)
    coalescer = MagicMock()
    coalescer.handle.return_value = False

    async def _on_event(kind: str, _info: SubagentInfo, extra: dict | None = None) -> None:
        if kind == RELAY_EVENT:
            relay_run_native_card(state, coalescer, ROOT_TAB, RUN, dict(extra or {}))

    manager._log_spawned(info)
    with (
        patch.object(manager, "_fire_event", side_effect=_on_event),
        patch("kiro_crew.subagent.Stats"),
        patch("kiro_crew.subagent.sel"),
    ):
        await manager._run_inner(info, f"subagent:{RUN}")


@pytest.mark.asyncio
async def test_stop_ends_the_runs_wait_on_the_childs_request_and_the_run_goes_on() -> None:
    """The run's ladder waits on the person for the child's request and reads nothing
    meanwhile, so it never sees the harness withdraw it. The Stop ends that wait: that
    call alone is rejected, and the run's own next request is still put to the person."""
    frames = _agent_frames()
    end = next(i for i, f in enumerate(frames) if f["params"]["update"].get("messageId"))
    client = _scripted_client(
        frames[: end + 1],
        ACP_BACKEND_CLAUDE,
        (_permission(7, child=True), _permission(9, child=False)),
    )
    process = MagicMock()
    process.stdin = _Stdin()
    process.returncode = None
    client._process = process
    info = _info()
    state = MagicMock()
    state._native_cards = {}
    state.subagents.get = MagicMock(side_effect=lambda run_id: info if run_id == RUN else None)
    person = _Person()
    run = asyncio.ensure_future(_run_asking(client, person, state, info))
    try:
        await await_until(person.reached("7"), what="the child's request put to the person")
        assert await _stop(state) == (200, {"ok": True, "cancelled": True, "stopping": True})
        await await_until(person.reached("9"), what="the run's own next request")
        assert person.cancelled == ["7"]
        person.answers["9"].set_result(True)
        await await_until(run, what="the run's end")
    finally:
        run.cancel()
    assert process.stdin.frames == [
        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": CHILD}}
    ]
    assert [c.args[0] for c in client.reject_tool.await_args_list] == [7]  # type: ignore[attr-defined]
    assert [c.args[0] for c in client.approve_tool.await_args_list] == [9]  # type: ignore[attr-defined]


def _request(request_id: int, *, child: bool = True) -> AcpEvent:
    return AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id=request_id, native_child_call=child)


@pytest.mark.asyncio
async def test_a_stop_ends_only_a_wait_on_the_stopped_childs_own_request() -> None:
    """Another child's Stop and the run's own requests leave the wait alone; the run's
    own Stop still reaches the approver, as when the run awaited it directly."""
    by_child = {"c1": ("7",), "c2": ("8",)}
    client = SimpleNamespace(native_child_requests=lambda sid: by_child.get(sid, ()))
    info = SimpleNamespace(_native_child_cards=_cards(client=client))
    cards = info._native_child_cards
    person = _Person()

    waiting = asyncio.ensure_future(
        native_child_cards.unless_withdrawn(info, _request(8), person(_request(8)))
    )
    await await_until(person.reached("8"), what="c2's request put to the person")
    assert cards.withdraw("c1") is False
    assert not waiting.done()
    assert cards.withdraw("c2") is True
    assert await await_until(waiting, what="the withdrawn wait's end") is False
    assert person.cancelled == ["8"]
    assert cards.withdraw("c2") is False

    own = asyncio.ensure_future(
        native_child_cards.unless_withdrawn(info, _request(9, child=False), person(_request(9)))
    )
    await await_until(person.reached("9"), what="the run's own request put to the person")
    assert cards.withdraw("c1") is False and cards.withdraw("c2") is False
    own.cancel()
    assert await await_until(own, what="the run's own wait's end") is False

    asked = asyncio.ensure_future(
        native_child_cards.unless_withdrawn(info, _request(7), person(_request(7)))
    )
    await await_until(person.reached("7"), what="c1's request put to the person")
    asked.cancel()
    assert await await_until(asked, what="the run's stop of its wait") is False
    assert person.cancelled == ["8", "9", "7"]


# ── 8. The reconnect replay ─────────────────────────────────────────────────────


def _replay_state(*runs: SubagentInfo) -> MagicMock:
    state = MagicMock()
    state._slots = {}
    state.subagents.all_agents = list(runs)
    return state


@pytest.mark.asyncio
async def test_a_reconnect_replays_a_runs_children_in_its_tab() -> None:
    info = _info()
    cards = await RunNativeChildCards.start(
        _Manager(),
        info,
        SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_CLAUDE), client=None),
    )
    await cards.on_event(_Manager(), info, _roster(_row("c1"), _row("c2")))
    await cards.on_event(_Manager(), info, _text("c1", "half way"))
    await cards.on_event(_Manager(), info, _roster(_row("c1"), _row("c2", "completed")))
    snapshots = DashboardState.native_subagent_snapshots(_replay_state(info))  # type: ignore[arg-type]
    by_id = {s["id"]: s for s in snapshots}
    running, done = by_id[f"native:{RUN}:c1"], by_id[f"native:{RUN}:c2"]
    assert (running["slot"], running["done"], running["streaming"]) == (ROOT_TAB, False, "half way")
    assert (done["slot"], done["done"], done["outcome"]) == (ROOT_TAB, True, "completed")


@pytest.mark.asyncio
async def test_the_replay_keeps_the_shared_ttl_and_terminal_cap() -> None:
    info = _info()
    cards = await RunNativeChildCards.start(
        _Manager(),
        info,
        SimpleNamespace(capabilities=capabilities_for(ACP_BACKEND_CLAUDE), client=None),
    )
    rows = [_row("old"), _row("new"), _row("newest")]
    await cards.on_event(_Manager(), info, _roster(*rows))
    await cards.on_event(
        _Manager(), info, _roster(*[{**r, "status": {"type": "completed"}} for r in rows])
    )
    now = cards.tracker["newest"]["done_at"]
    cards.tracker["old"]["done_at"] = now - 2 * NATIVE_SUBAGENT_TERMINAL_TTL_SECS
    cards.tracker["new"]["done_at"] = now - NATIVE_SUBAGENT_TERMINAL_TTL_SECS / 2
    state = _replay_state(info)
    kept = DashboardState.native_subagent_snapshots(state)  # type: ignore[arg-type]
    assert {s["id"] for s in kept} == {f"native:{RUN}:new", f"native:{RUN}:newest"}
    capped = DashboardState.native_subagent_snapshots(state, terminal_limit=1)  # type: ignore[arg-type]
    assert [s["id"] for s in capped] == [f"native:{RUN}:newest"]
