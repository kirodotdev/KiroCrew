"""Stop on a Claude Agent-tool child's card stops that child in claude-agent-acp.

The adapter announces each child to a client declaring ``clientCapabilities.subagents``
with ``capabilities.cancel`` (0.107.0 ``native-subagents.js`` ``announceNativeSubagent``
1443-1460; 0.94.0 945). A ``session/cancel`` naming the child's session stops it alone
(``v1/acp-agent.js`` ``cancel`` / ``cancelChildSession`` 5185-5205,
``NativeSubagentRuntime.cancelChild`` 706-717), reported ``idle`` with ``cancelled``
(``rfdEndState`` 1698-1705). Claude Code withdraws the child's open permission and
question requests, and the SDK sends ``$/cancel_request`` for each (``v1/acp-agent.js``
``requestPermissionFromClient`` 6378-6405, ``v1/client-connection.js`` 18-35, the SDK's
``core/jsonrpc.js`` 485-497).

The live capture ``agent-task-0.94.0.jsonl`` is replayed through the real ``AcpClient``
turn reader, the real ``_native_subagent_sync`` draws the card and the real
``DELETE /api/spawn/{id}`` handler takes the Stop.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable, Sequence
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from dashboard_approval_helpers import (
    _answer_approval,
    _complete_event,
    _context_builder,
    _drain,
    _make_slot,
    _make_state,
    _patch_stats,
    _set_stream,
)

from kiro_crew.acp import client_native_children
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_SUBAGENT_LIST, AcpEvent, JsonRpcMessage
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_KIRO,
    ACP_BACKENDS_NATIVE_CHILD_CANCEL,
)
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_runner import _native_subagent_sync
from kiro_crew.dashboard.handlers.messaging import api_spawn_delete
from kiro_crew.dashboard.native_child_stop import STOP_REQUESTED
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.testing.wait import FALLBACK_TIMEOUT_SECS, async_wait_until

CAPTURE = Path(__file__).parent / "fixtures" / "claude_native_subagents" / "agent-task-0.94.0.jsonl"
PARENT = "parent-0001"
CHILD = "child-0001"
CARD = f"native:{CHILD}"
SLOT = "slot-1"

#: The end the adapter reports for a child whose task a cancel stopped (``rfdEndState``).
CANCELLED_END = {"state": {"state": "idle", "stopReason": "cancelled"}}
#: The end of a child that finished its own work.
FINISHED_END = {"state": {"state": "idle", "stopReason": "end_turn"}}
REQUIRES_ACTION = {"state": {"state": "requires_action"}}
CANCEL_CHILD = {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": CHILD}}
#: The Stop's answer when the harness was asked, and when the card closed alone.
STOPPING = {"ok": True, "cancelled": True, "stopping": True}
CLOSED = {"ok": True, "cancelled": True}


def _subagent_update(update: dict[str, Any]) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": PARENT,
            "update": {"sessionUpdate": "subagent_update", "sessionId": CHILD, **update},
        },
    )


def _announce_and_prompt(*reports: dict[str, Any]) -> list[JsonRpcMessage]:
    """The capture up to the child's delegated prompt (the child announced and running),
    then a ``subagent_update`` per *report*."""
    lines = [json.loads(line) for line in CAPTURE.read_text(encoding="utf-8").splitlines()]
    frames = [line for line in lines[1:] if "_turn_end" not in line]
    end = next(i for i, f in enumerate(frames) if f["params"]["update"].get("messageId"))
    messages = [JsonRpcMessage.from_dict(frame) for frame in frames[: end + 1]]
    return messages + [_subagent_update(report) for report in reports]


class _Stdin:
    """The adapter's stdin: every frame written, decoded. *while_draining* runs while a
    write drains, as the turn reader runs while a Stop's write is awaited."""

    def __init__(
        self, *, broken: bool = False, while_draining: Callable[[], None] | None = None
    ) -> None:
        self.frames: list[dict[str, Any]] = []
        self.broken = broken
        self.drain = AsyncMock(side_effect=while_draining)

    def write(self, data: bytes) -> None:
        if self.broken:
            raise BrokenPipeError
        self.frames.append(json.loads(data.decode("utf-8")))


def _client(backend: str = ACP_BACKEND_CLAUDE, *, stdin: _Stdin | None = None) -> AcpClient:
    client = AcpClient(acp_backend=backend)
    client._session_id = PARENT
    process = MagicMock()
    process.stdin = stdin if stdin is not None else _Stdin()
    process.returncode = None
    client._process = process
    return client


def _written(client: AcpClient) -> list[dict[str, Any]]:
    return client._process.stdin.frames  # type: ignore[union-attr]


async def _replay(
    client: AcpClient, messages: Sequence[JsonRpcMessage | tuple[str, JsonRpcMessage]]
) -> list[AcpEvent]:
    """One ``_dispatch_events`` turn over *messages* (an update, or a ``(kind, frame)``
    pair), then the prompt's answer."""

    async def _fake_loop(req_id: int, timeout: float) -> AsyncIterator[tuple[str, Any]]:
        for message in messages:
            yield message if isinstance(message, tuple) else ("update", message)
        yield ("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))

    client._prompt_loop = _fake_loop  # type: ignore[assignment]
    return [
        event async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS)
    ]


async def _delete_card(state: object, card_id: str = CARD) -> tuple[int, dict, MagicMock]:
    """Press a card's Stop: ``DELETE /api/spawn/<card id>`` as the dashboard owner."""
    request = MagicMock()
    request.app = {"state": state}
    request.match_info = {"agent_id": card_id}
    with patch("kiro_crew.dashboard.handlers.messaging._sel") as sel:
        response = await api_spawn_delete(request)
    return response.status, json.loads(response.text), sel.return_value


class _Dashboard:
    """The dashboard state, slot and tracker a native card lives in."""

    def __init__(self, client: object) -> None:
        self.state = MagicMock()
        self.state._native_cards = {}
        self.slot = MagicMock()
        self.slot.key = SLOT
        self.slot._native_subagent_tracker = {}
        self.slot._native_subagent_output = {}
        self.slot._acp_client = client
        self.state.get_slot.return_value = self.slot
        self.state._slots = {SLOT: self.slot}

    def snapshots(self) -> list[dict[str, object]]:
        """What a reconnecting window is replayed (``native_subagent_snapshots``)."""
        return DashboardState.native_subagent_snapshots(self.state)  # type: ignore[arg-type]

    def draw(self, events: list[AcpEvent]) -> None:
        for event in events:
            if event.kind == EVENT_SUBAGENT_LIST:
                _native_subagent_sync(
                    self.state, self.slot, event.subagents, self.slot._native_subagent_tracker
                )

    def frames(self, kind: str) -> list[dict[str, Any]]:
        return [c.args[1] for c in self.state.broadcast_ws.call_args_list if c.args[0] == kind]

    async def stop(self, card_id: str = CARD) -> tuple[int, dict[str, Any], MagicMock]:
        return await _delete_card(self.state, card_id)


async def _running_child_card(client: AcpClient) -> _Dashboard:
    dashboard = _Dashboard(client)
    dashboard.draw(await _replay(client, _announce_and_prompt()))
    assert CARD in dashboard.state._native_cards
    return dashboard


def _drawn_stopped(dashboard: _Dashboard, child: str = CHILD) -> None:
    """The card closed and drawn stopped, live and in the replay, with no mark left."""
    [done] = dashboard.frames("subagent_done")
    assert done["id"] == f"native:{child}" and done["error"] is None and done["stopped"] is True
    assert f"native:{child}" not in dashboard.state._native_cards
    record = dashboard.slot._native_subagent_tracker[child]
    assert record["stopped"] is True and record["outcome"] == "stopped"
    assert STOP_REQUESTED not in record
    [snapshot] = dashboard.snapshots()
    assert (snapshot["outcome"], snapshot["stopped"], snapshot["error"]) == ("stopped", True, None)


def _drawn_completed(dashboard: _Dashboard) -> None:
    """The card closed by the child's own end, with no mark of the Stop."""
    [done] = dashboard.frames("subagent_done")
    assert done["error"] is None and "stopped" not in done
    record = dashboard.slot._native_subagent_tracker[CHILD]
    assert STOP_REQUESTED not in record and "stopped" not in record
    assert dashboard.snapshots()[0]["outcome"] == "completed"


# ── 1. The grant the client keeps ──────────────────────────────────────────────


def test_only_claude_stops_a_native_child_by_its_session() -> None:
    assert ACP_BACKENDS_NATIVE_CHILD_CANCEL == frozenset({ACP_BACKEND_CLAUDE})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reports", "granted"),
    [
        ((), {CHILD}),
        ((REQUIRES_ACTION,), {CHILD}),
        *(
            (({"capabilities": withdrawn, "state": {"state": "unknown"}},), set())
            for withdrawn in (None, {}, {"cancel": None}, "cancel")
        ),
    ],
    ids=["announce", "names-no-capabilities", "null", "empty", "cancel-null", "not-an-object"],
)
async def test_the_announce_grants_the_cancel_and_a_report_can_withdraw_it(
    reports: tuple[dict[str, Any], ...], granted: set[str]
) -> None:
    """0.107.0 sends ``capabilities: null`` for a child it has lost track of."""
    client = _client()
    await _replay(client, _announce_and_prompt(*reports))
    assert client._native_child_cancellable == granted


# ── 2. The Stop, end to end ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stop_on_a_running_child_sends_one_cancel_naming_the_child() -> None:
    """Pressed mid-turn: one ``session/cancel`` naming the child alone, the parent's turn
    not cancelled, and the card open until the adapter's end closes it as stopped, live
    and in the replay."""
    client = _client()
    dashboard = _Dashboard(client)

    async def _adapter(req_id: int, timeout: float) -> AsyncIterator[tuple[str, Any]]:
        for message in _announce_and_prompt():
            yield ("update", message)
        # The adapter reports the cancelled end only once it has been asked to stop.
        asked = [f for f in _written(client) if f["params"] == {"sessionId": CHILD}]
        yield ("update", _subagent_update(CANCELLED_END if asked else FINISHED_END))
        yield ("complete", JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))

    client._prompt_loop = _adapter  # type: ignore[assignment]
    events, stop = [], None
    async for event in client._dispatch_events(req_id=1, timeout=FALLBACK_TIMEOUT_SECS):
        events.append(event)
        dashboard.draw([event])
        if CARD in dashboard.state._native_cards and stop is None:
            stop = await dashboard.stop()
            assert CARD in dashboard.state._native_cards and not dashboard.frames("subagent_done")

    status, body, sel = stop
    assert (status, body) == (200, STOPPING)
    assert _written(client) == [CANCEL_CHILD]
    assert client._cancelled is False
    _drawn_stopped(dashboard)
    assert events[-1].kind == EVENT_COMPLETE and events[-1].stop_reason == "end_turn"
    audit = sel.log_tool_invocation.call_args.kwargs
    assert audit["outcome"] == "cancelled_by_user"
    assert audit["metadata"] == {"card_id": CARD, "harness_cancel": True}


@pytest.mark.asyncio
async def test_a_cancelled_end_nobody_asked_for_stays_failed() -> None:
    """The parent's own cancel also ends a child ``cancelled``: no Stop on its card asked
    for that, so it keeps its failed drawing."""
    client = _client()
    dashboard = await _running_child_card(client)
    dashboard.draw(await _replay(client, [_subagent_update(CANCELLED_END)]))
    [done] = dashboard.frames("subagent_done")
    assert done["error"] == "cancelled" and "stopped" not in done
    [snapshot] = dashboard.snapshots()
    assert snapshot["outcome"] == "failed" and snapshot["error"] == "cancelled"


@pytest.mark.asyncio
async def test_a_stopped_child_that_finishes_first_stays_completed() -> None:
    """Only the cancelled end is the Stop's; the mark goes with any end."""
    client = _client()
    dashboard = await _running_child_card(client)
    assert (await dashboard.stop())[:2] == (200, STOPPING)
    dashboard.draw(await _replay(client, [_subagent_update(FINISHED_END)]))
    _drawn_completed(dashboard)


async def _stop_as_the_end_arrives(
    end: dict[str, Any], *, pipe_breaks: bool = False
) -> tuple[_Dashboard, int, dict[str, Any]]:
    """Stop, with the child's *end* read while the cancel's write drains."""
    cards: list[_Dashboard] = []

    def _end_arrives() -> None:
        cards[0].draw(client._note_subagent_update({"sessionId": CHILD, **end}))
        if pipe_breaks:
            raise BrokenPipeError

    client = _client(stdin=_Stdin(while_draining=_end_arrives))
    cards.append(await _running_child_card(client))
    status, body, _ = await cards[0].stop()
    return cards[0], status, body


@pytest.mark.asyncio
async def test_the_end_read_while_the_cancel_is_written_is_drawn_stopped() -> None:
    """The end can be drawn before the route resumes, so the mark is set before the write."""
    dashboard, status, body = await _stop_as_the_end_arrives(CANCELLED_END)
    assert (status, body) == (200, STOPPING)
    _drawn_stopped(dashboard)


@pytest.mark.asyncio
async def test_a_card_its_own_end_closed_during_the_stop_is_not_closed_again() -> None:
    """A write that fails after the child's own end closed the card answers the Stop
    without a second close, which would redraw a finished child as stopped."""
    dashboard, status, body = await _stop_as_the_end_arrives(FINISHED_END, pipe_breaks=True)
    assert (status, body) == (200, CLOSED)
    _drawn_completed(dashboard)


# ── 3. The Stops that send nothing ─────────────────────────────────────────────


async def _stops_alone(dashboard: _Dashboard, child: str = CHILD) -> None:
    """The Stop closes the card as stopped by the dashboard, as no harness cancel."""
    status, body, sel = await dashboard.stop(f"native:{child}")
    assert (status, body) == (200, CLOSED)
    assert sel.log_tool_invocation.call_args.kwargs["metadata"] == {"card_id": f"native:{child}"}
    _drawn_stopped(dashboard, child)


@pytest.mark.asyncio
async def test_a_child_granted_no_cancel_gets_its_card_closed_alone() -> None:
    client = _client()
    messages = _announce_and_prompt()
    announce = next(
        m for m in messages if m.params["update"].get("sessionUpdate") == "subagent_update"
    )
    del announce.params["update"]["capabilities"]
    dashboard = _Dashboard(client)
    dashboard.draw(await _replay(client, messages))
    await _stops_alone(dashboard)
    assert _written(client) == []


async def _withdraw_the_grant(client: AcpClient, dashboard: _Dashboard) -> None:
    withdrawn = {"capabilities": None, "state": {"state": "unknown"}}
    dashboard.draw(await _replay(client, [_subagent_update(withdrawn)]))


async def _break_the_pipe(client: AcpClient, dashboard: _Dashboard) -> None:
    client._process.stdin.broken = True  # type: ignore[union-attr]


async def _release_the_turn(client: AcpClient, dashboard: _Dashboard) -> None:
    """The slot drops its live client at the turn's release."""
    dashboard.slot._acp_client = None


@pytest.mark.asyncio
@pytest.mark.parametrize("spoil", [_withdraw_the_grant, _break_the_pipe, _release_the_turn])
async def test_a_stop_the_harness_cannot_take_closes_the_card_alone(
    spoil: Callable[[AcpClient, _Dashboard], Any],
) -> None:
    client = _client()
    dashboard = await _running_child_card(client)
    await spoil(client, dashboard)
    await _stops_alone(dashboard)
    assert _written(client) == []


def _adapter_exits(client: AcpClient, monkeypatch: pytest.MonkeyPatch) -> None:
    client._process.returncode = 1  # type: ignore[union-attr]


def _backend_leaves_the_set(client: AcpClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """The positive membership is what lets the cancel through."""
    monkeypatch.setattr(client_native_children, "ACP_BACKENDS_NATIVE_CHILD_CANCEL", frozenset())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("reports", "spoil", "sent"),
    [
        ((REQUIRES_ACTION,), None, True),
        ((FINISHED_END,), None, False),
        ((CANCELLED_END,), None, False),
        ((), _adapter_exits, False),
        ((), _backend_leaves_the_set, False),
    ],
    ids=["waiting-on-a-request", "finished", "cancelled", "dead-adapter", "outside-the-set"],
)
async def test_a_cancel_is_sent_only_to_a_live_granted_child(
    reports: tuple[dict[str, Any], ...],
    spoil: Callable[[AcpClient, pytest.MonkeyPatch], None] | None,
    sent: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    await _replay(client, _announce_and_prompt(*reports))
    if spoil is not None:
        spoil(client, monkeypatch)
    assert await client.cancel_native_child(CHILD) is sent
    assert _written(client) == ([CANCEL_CHILD] if sent else [])


@pytest.mark.asyncio
async def test_the_parent_is_never_named_by_a_child_cancel() -> None:
    client = _client()
    await _replay(client, _announce_and_prompt())
    client._native_child_cancellable.add(PARENT)
    client._native_child_rows[PARENT] = dict(client._native_child_rows[CHILD])
    assert await client.cancel_native_child(PARENT) is False
    assert await client.cancel_native_child("") is False
    assert await client.cancel_native_child("child-unknown") is False
    assert _written(client) == []


def _roster(session_id: str, **row: str) -> list[AcpEvent]:
    """An ``EVENT_SUBAGENT_LIST`` naming one working child."""
    status = {"type": "working", "message": ""}
    return [
        AcpEvent(
            kind=EVENT_SUBAGENT_LIST, subagents=[{"sessionId": session_id, **row, "status": status}]
        )
    ]


@pytest.mark.asyncio
async def test_a_workflow_agents_card_keeps_its_stop() -> None:
    """A Workflow agent has no session of its own and the adapter offers no per-agent
    stop, so its card (``native:<task>:<agent>``) is closed alone."""
    client = _client()
    await _replay(client, _announce_and_prompt())
    dashboard = _Dashboard(client)
    row = {"sessionName": "review", "agentName": "reviewer", "initialQuery": "review the diff"}
    dashboard.draw(_roster("task-1:reviewer", **row))
    await _stops_alone(dashboard, "task-1:reviewer")
    assert _written(client) == []


# ── 4. Other harnesses keep their Stop ─────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime", [False, True], ids=["kiro-client", "runtime-provider"])
async def test_a_kiro_native_card_closes_alone(runtime: bool) -> None:
    """A kiro-cli card is closed alone: kiro-cli's client sends nothing, and the
    ``AcpSessionProvider`` a runtime slot holds asks its handle, which grants no child
    cancel off ``ACP_BACKENDS_NATIVE_CHILD_CANCEL``."""
    holder = MagicMock(spec=AcpSessionProvider) if runtime else _client(ACP_BACKEND_KIRO)
    if runtime:
        holder.cancel_native_child = AsyncMock(return_value=False)
    dashboard = _Dashboard(holder)
    row = {"sessionName": "summarize", "role": "worker", "initialQuery": "summarize the log"}
    dashboard.draw(_roster(CHILD, **row))
    await _stops_alone(dashboard)
    if runtime:
        # Asked once and declined: nothing else is called on it.
        assert holder.method_calls == [call.cancel_native_child(CHILD)]
    else:
        assert not _written(holder)


# ── 5. A child stopped while a card of its waits on the person ─────────────────

#: The child's permission request: on its session (``toolUseOwner``'s
#: ``toolCallSessionId``, ``v1/acp-agent.js`` 6593, 6674-6678), stamped by
#: ``permissionToolCallMeta`` (``permissions/presentation.js`` 112-124), its toolCall and
#: options as ``acp_frames/claude/builtins-bash-edit-webfetch-live.jsonl`` has them.
CHILD_PERMISSION = {
    "jsonrpc": "2.0",
    "id": 7,
    "method": "session/request_permission",
    "params": {
        "sessionId": CHILD,
        "toolCall": {
            "toolCallId": "toolu_0003",
            "name": "Bash",
            "status": "pending",
            "rawInput": {"command": "touch made.txt", "description": "Create made.txt"},
            "title": "touch made.txt",
            "kind": "execute",
            "_meta": {"claudeCode": {"toolName": "Bash", "parentToolUseId": "toolu_0001"}},
        },
        "options": [
            {"optionId": "allow-once", "name": "Yes", "kind": "allow_once"},
            {"optionId": "reject", "name": "No", "kind": "reject_once"},
        ],
    },
}
#: A question the child asks: AskUserQuestion as a form elicitation on the child's
#: session (``handleAskUserQuestion`` 6767-6775).
CHILD_QUESTION = {
    "jsonrpc": "2.0",
    "id": 8,
    "method": "elicitation/create",
    "params": {
        "sessionId": CHILD,
        "mode": "form",
        "message": "Which file?",
        "requestedSchema": {"type": "object", "properties": {"file": {"type": "string"}}},
    },
}


def _parent_permission(request_id: int) -> dict[str, Any]:
    """The parent's own request: its session, no Agent-call stamp."""
    params = json.loads(json.dumps(CHILD_PERMISSION["params"]))
    params["sessionId"] = PARENT
    params["toolCall"]["toolCallId"] = f"toolu_parent_{request_id}"
    del params["toolCall"]["_meta"]["claudeCode"]["parentToolUseId"]
    return {**CHILD_PERMISSION, "id": request_id, "params": params}


class _WaitingDashboard(_Dashboard):
    """A ``_Dashboard`` whose slot holds real cards, registered as the runner does."""

    def __init__(self, client: object) -> None:
        super().__init__(client)
        self.slot.messages = []
        self.slot._approval_futures = {}
        self.slot._approval_stopped = set()
        # The row identity each waiting card was drawn under.
        self.slot.approval_instance = lambda request_id: f"mid-{request_id}"
        self.state.push_slots_update = MagicMock()

    def open_card(self, request_id: str, role: str = "permission") -> Any:
        """Draw a waiting card and register its future (``_on_permission_prompt``)."""
        meta = {"request_id": request_id, "tool_call_id": ""}
        self.slot.messages.append({"role": role, "cls": json.dumps(meta)})
        fut = asyncio.get_running_loop().create_future()
        self.slot._approval_futures[request_id] = fut
        return fut

    def row(self, request_id: str) -> dict[str, Any]:
        for message in self.slot.messages:
            meta = json.loads(message["cls"])
            if meta["request_id"] == request_id:
                return meta
        raise AssertionError(f"no row for {request_id}")


async def _child_waiting_on_the_person(client: AcpClient) -> list[AcpEvent]:
    """The child ``requires_action`` with its permission request and question, then the
    parent's own permission request, each read by the real turn reader."""
    client.accepts_elicitation = True
    return await _replay(
        client,
        [
            *_announce_and_prompt(REQUIRES_ACTION),
            ("permission", JsonRpcMessage.from_dict(CHILD_PERMISSION)),
            ("elicitation", JsonRpcMessage.from_dict(CHILD_QUESTION)),
            ("permission", JsonRpcMessage.from_dict(_parent_permission(9))),
        ],
    )


@pytest.mark.asyncio
async def test_the_client_keeps_each_childs_requests_until_its_end_or_the_turns() -> None:
    """The child's permission and question are its own; the parent's is nobody's."""
    client = _client()
    events = await _child_waiting_on_the_person(client)
    assert [e.request_id for e in events if e.request_id in (7, 8, 9)] == [7, 8, 9]
    assert client.native_child_requests(CHILD) == ("7", "8")
    assert client.native_child_requests(PARENT) == ()
    client._note_subagent_update({"sessionId": CHILD, **CANCELLED_END})
    assert client.native_child_requests(CHILD) == ()
    client._native_child_requests["7"] = CHILD
    client._reset_native_child_turn()
    assert client.native_child_requests(CHILD) == ()


@pytest.mark.asyncio
async def test_a_child_request_id_past_the_bound_is_not_kept() -> None:
    client = _client()
    long_id = "9" * (client_native_children.MAX_CHILD_REQUEST_ID_CHARS + 1)
    frame = JsonRpcMessage.from_dict({**CHILD_PERMISSION, "id": long_id})
    await _replay(client, [*_announce_and_prompt(REQUIRES_ACTION), ("permission", frame)])
    assert client.native_child_requests(CHILD) == ()


@pytest.mark.asyncio
async def test_no_request_is_kept_on_a_backend_outside_the_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client()
    _backend_leaves_the_set(client, monkeypatch)
    await _child_waiting_on_the_person(client)
    assert client._native_child_requests == {}


@pytest.mark.asyncio
async def test_stop_settles_the_cards_the_stopped_child_left_waiting() -> None:
    """The turn, waiting on the child's card, never reads the withdrawal, so the Stop
    settles the child's cards as the chat's Stop does and leaves the parent's waiting."""
    client = _client()
    dashboard = _WaitingDashboard(client)
    dashboard.draw(await _child_waiting_on_the_person(client))
    permission = dashboard.open_card("7")
    question = dashboard.open_card("8", role="elicitation")
    parents = dashboard.open_card("9")
    with patch("kiro_crew.dashboard.chat_handlers.sel") as chat_sel:
        status, body, _ = await dashboard.stop()

    assert (status, body) == (200, STOPPING)
    assert _written(client)[-1] == CANCEL_CHILD
    # Rejected for this call alone, so the batch's later requests are still asked.
    assert permission.result() == "rejected_once"
    assert question.done() and not parents.done()
    assert dashboard.slot._approval_stopped == {"7", "8"}
    assert dashboard.row("7")["resolved"] == "rejected"
    assert "resolved" not in dashboard.row("9")
    # The Stop retires the permission card on every window; the question's runner
    # retires its own as it answers ``cancel``.
    assert dashboard.frames("approval_resolved") == [
        {"id": "7", "approved": False, "slot": SLOT, "origin": "native", "mid": "mid-7"}
    ]
    dashboard.state.push_slots_update.assert_called_once()
    audits = [c.kwargs for c in chat_sel.return_value.log_tool_invocation.call_args_list]
    assert [(a["tool_name"], a["outcome"]) for a in audits] == [
        ("approval_reject:7", "rejected_on_stop"),
        ("approval_reject:8", "rejected_on_stop"),
    ]
    parents.cancel()


@pytest.mark.asyncio
async def test_a_stop_that_sends_nothing_settles_no_card() -> None:
    """A child the cancel was not sent to runs on, and so does its request."""
    client = _client(stdin=_Stdin(broken=True))
    dashboard = _WaitingDashboard(client)
    dashboard.draw(await _child_waiting_on_the_person(client))
    permission = dashboard.open_card("7")
    await _stops_alone(dashboard)
    assert not permission.done() and dashboard.slot._approval_stopped == set()
    assert dashboard.frames("approval_resolved") == []
    permission.cancel()


def _asked(request_id: str, title: str) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST, title=title, tool_kind="execute", request_id=request_id
    )


@pytest.mark.asyncio
async def test_the_turn_goes_on_and_puts_the_next_request_to_the_person(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the real chat runner: the Stop wakes the runner waiting on the child's
    card, which refuses that call alone and puts the next request (another child's) to
    the person, so the turn ends rather than stalling on a card no answer can reach."""
    state, client = _make_state(tmp_path, context_builder=_context_builder())
    slot = _make_slot()
    monkeypatch.setattr(state, "get_slot", lambda key: slot if key == slot.key else None)
    replies: list[dict[str, Any]] = []
    client.cancel_native_child = AsyncMock(return_value=True)
    client.native_child_requests = MagicMock(return_value=("req-1",))
    _set_stream(
        client,
        [_asked("req-1", "touch made.txt"), _asked("req-2", "touch other.txt"), _complete_event()],
    )

    async def _press_stop() -> None:
        await async_wait_until(
            lambda: "req-1" in slot._approval_futures,
            timeout=FALLBACK_TIMEOUT_SECS,
            describe=lambda: "the child's card was never drawn",
        )
        # The child's card as its roster draws it during the turn.
        slot._acp_client = client
        slot._native_subagent_tracker[CHILD] = dict(
            id=CARD, started=0.0, done=False, agent="general-purpose", task="say pong"
        )
        state._native_cards = {CARD: {"slot": slot.key, "session_id": CHILD, "started": 0.0}}
        replies.append((await _delete_card(state))[1])

    loop = asyncio.get_running_loop()
    stopper = loop.create_task(_press_stop())
    approver = loop.create_task(_answer_approval(slot, "req-2", "approved"))
    with _patch_stats(), patch("kiro_crew.dashboard.chat_handlers.sel"):
        await asyncio.wait_for(_run_chat(state, slot, "hello"), timeout=FALLBACK_TIMEOUT_SECS)
    await _drain(stopper)
    await _drain(approver)

    client.cancel_native_child.assert_awaited_once_with(CHILD)
    assert replies == [STOPPING]
    assert [c.args[0] for c in client.reject_tool.await_args_list] == ["req-1"]
    assert [c.args[0] for c in client.approve_tool.await_args_list] == ["req-2"]
    rows = [m["content"] for m in slot.messages if m.get("role") == "tool"]
    assert any("touch made.txt (rejected — this call only)" in row for row in rows)
    assert not any("touch other.txt (rejected" in row for row in rows)
    assert slot._approval_stopped == set() and slot._approval_futures == {}
    resolved = [
        (frame["id"], frame["approved"])
        for kind, frame in (c.args for c in state.broadcast_ws.call_args_list)
        if kind == "approval_resolved"
    ]
    assert resolved.count(("req-1", False)) == 1
