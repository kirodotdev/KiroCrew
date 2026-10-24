"""Native Codex subagents become Crew subagent cards.

codex-acp gives each native subagent its own ACP session only for a client that
declares ``clientCapabilities.subagents``. The child is then announced by a
lifecycle ``session/update`` on its parent's session and streams its own work
under its own session id. Three seams turn that into one Activity card per
child, and each is pinned here:

1. the codex harness declares the capability;
2. the runtime routes the child's frames to the session that announced it;
3. the session handle turns the lifecycle frames into the card roster, and the
   child's frames into activity on that card, never into parent transcript.

An adapter built on ACP SDK 1.x drops the declaration while parsing
``initialize`` and still reports the child as "Start/Complete subagent" tool
rows; those rows feed the same roster.

The ``fixtures/codex_native_subagents`` captures came off codex-acp driven over
stdio with one synthetic prompt (see each file's ``_meta.note``).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from acp_runtime_helpers import _fast_no_report_ceiling  # noqa: F401
from acp_runtime_helpers import _pinned_kiro_cli_version  # noqa: F401
from acp_runtime_helpers import _stub_sel_for_permission_tests  # noqa: F401
from acp_runtime_helpers import (
    _await_routed,
    _drain,
    _feed,
    _make_runtime,
    _register,
    _start_reader,
    _stop_reader,
)

from kiro_crew.acp.harness.codex import CodexHarness
from kiro_crew.acp.harness.kiro import KiroHarness
from kiro_crew.acp.session_handle import NATIVE_CHILD_ROSTER_CAP
from kiro_crew.acp.types import (
    ACP_BACKEND_CODEX,
    ACP_CLIENT_CAPABILITIES,
    EVENT_SUBAGENT_ACTIVITY,
    EVENT_SUBAGENT_LIST,
    EVENT_TEXT_CHUNK,
    EVENT_TOOL_CALL,
    JsonRpcMessage,
)
from kiro_crew.dashboard.chat_runner import _native_subagent_close_all, _native_subagent_sync

_FIXTURES = Path(__file__).parent / "fixtures" / "codex_native_subagents"


def _fixture_turns(name: str) -> list[list[dict]]:
    """A capture's frames, split at its ``{"_turn_end": ...}`` markers."""
    lines = (_FIXTURES / name).read_text(encoding="utf-8").splitlines()
    header, *records = (json.loads(line) for line in lines if line.strip())
    assert header["_meta"]["backend"] == "codex"
    turns: list[list[dict]] = [[]]
    for record in records:
        if "_turn_end" in record:
            turns.append([])
        else:
            turns[-1].append(record)
    return [t for t in turns if t]


def _fixture(name: str) -> list[dict]:
    return [frame for turn in _fixture_turns(name) for frame in turn]


def _update(sid: str, update: dict) -> dict:
    return {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": sid, "update": update},
    }


def _spawned(parent: str, child: str, name: str = "Probe worker") -> dict:
    return _update(
        parent,
        {
            "sessionUpdate": "subagent_spawned",
            "subagentSessionId": child,
            "name": name,
            "task": f"Delegated task for {name}",
            "capabilities": {},
        },
    )


def _child_text(child: str, text: str) -> dict:
    return _update(
        child, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}}
    )


def _permission(child: str, request_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "session/request_permission",
        "params": {
            "sessionId": child,
            "toolCall": {"toolCallId": "call-1", "title": "echo probe-child-ok"},
            "options": [
                {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Reject", "kind": "reject_once"},
            ],
        },
    }


# ── 1. capability negotiation ────────────────────────────────────────────────


def test_codex_declares_native_subagent_sessions() -> None:
    caps = CodexHarness().client_capabilities
    assert caps["subagents"] == {}
    # Everything else is the shared declaration, unchanged.
    assert {k: v for k, v in caps.items() if k != "subagents"} == ACP_CLIENT_CAPABILITIES
    # Not the JetBrains AIR private opt-in, which turns on the rest of AIR too.
    assert "_meta" not in caps


def test_other_harnesses_do_not_declare_subagents() -> None:
    assert "subagents" not in ACP_CLIENT_CAPABILITIES
    assert "subagents" not in KiroHarness().client_capabilities


# ── 2. runtime routing of announced children ─────────────────────────────────


async def _route(
    frames: list[dict], *, sessions=("parent",), active=("parent",), backend=ACP_BACKEND_CODEX
):
    rt, reader, _ = _make_runtime()
    rt._acp_backend = backend
    queues = _register(rt, *sessions)
    for sid in active:
        rt.mark_turn_active(sid, True)
    task = await _start_reader(rt)
    try:
        for frame in frames:
            _feed(reader, frame)
        await _drain(reader)
    finally:
        await _stop_reader(task)
    got = {sid: [] for sid in sessions}
    for sid, q in queues.items():
        while not q.empty():
            got[sid].append(q.get_nowait())
    return rt, got


@pytest.mark.asyncio
async def test_child_frames_route_to_the_announcing_session_beside_a_cotenant() -> None:
    """A spawn_run child sharing the runtime does not make the route ambiguous:
    the announcement named its owner."""
    rt, got = await _route(
        [_spawned("parent", "child-1"), _child_text("child-1", "probe-child-ok")],
        sessions=("parent", "cotenant"),
    )
    assert [m.params["sessionId"] for m in got["parent"]] == ["parent", "child-1"]
    assert got["cotenant"] == []
    assert rt._announced_children == {"child-1": "parent"}


@pytest.mark.asyncio
async def test_nested_child_routes_to_the_root_owner() -> None:
    rt, got = await _route(
        [
            _spawned("parent", "child-1"),
            _spawned("child-1", "grandchild-1", "Nested worker"),
            _child_text("grandchild-1", "deep"),
        ]
    )
    assert [m.params["sessionId"] for m in got["parent"]] == ["parent", "child-1", "grandchild-1"]
    assert rt._announced_children == {"child-1": "parent", "grandchild-1": "parent"}


@pytest.mark.asyncio
async def test_unknown_child_frames_are_dropped_not_broadcast() -> None:
    rt, got = await _route([_child_text("stranger", "hello")], sessions=("parent", "cotenant"))
    assert got == {"parent": [], "cotenant": []}
    # An announcement on a session nobody registered announces nothing.
    rt2, got2 = await _route([_spawned("ghost", "child-9"), _child_text("child-9", "x")])
    assert rt2._announced_children == {}
    assert got2 == {"parent": []}


@pytest.mark.asyncio
async def test_a_child_cannot_be_reowned_by_a_later_announcement() -> None:
    rt, got = await _route(
        [
            _spawned("parent", "child-1"),
            _spawned("cotenant", "child-1"),
            _child_text("child-1", "x"),
        ],
        sessions=("parent", "cotenant"),
    )
    assert rt._announced_children == {"child-1": "parent"}
    assert [m.params["sessionId"] for m in got["parent"]][-1] == "child-1"


@pytest.mark.asyncio
async def test_child_updates_between_turns_are_dropped() -> None:
    rt, got = await _route(
        [_spawned("parent", "child-1"), _child_text("child-1", "late")], active=()
    )
    assert [m.params["sessionId"] for m in got["parent"]] == ["parent"]


@pytest.mark.asyncio
async def test_child_permission_reaches_the_owner_during_its_turn() -> None:
    rt, got = await _route([_spawned("parent", "child-1"), _permission("child-1", 41)])
    perms = [m for m in got["parent"] if m.method == "session/request_permission"]
    assert perms and perms[0].params["sessionId"] == "child-1"


@pytest.mark.asyncio
async def test_child_permission_between_turns_is_answered_fail_closed() -> None:
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_CODEX
    _register(rt, "parent")
    rt._spawn_answer_task = AsyncMock()
    task = await _start_reader(rt)
    try:
        _feed(reader, _spawned("parent", "child-1"))
        _feed(reader, _permission("child-1", 42))
        await _drain(reader)
    finally:
        await _stop_reader(task)
    rt._spawn_answer_task.assert_awaited_once()
    assert rt._spawn_answer_task.await_args.kwargs["reason"] == "owner_no_active_turn"


@pytest.mark.asyncio
async def test_other_harnesses_learn_no_announced_children() -> None:
    """The announced-child route is codex's: on another harness the same frames
    announce nothing and the child's frames stay unroutable."""
    rt, got = await _route([_spawned("parent", "child-1"), _child_text("child-1", "x")], backend="")
    assert rt._announced_children == {}
    assert [m.params["sessionId"] for m in got["parent"]] == ["parent"]


@pytest.mark.asyncio
async def test_a_backend_without_a_runtime_harness_still_routes_its_frames() -> None:
    """A runtime for a backend no harness serves still delivers its own frames:
    the announced-child question answers "none" instead of stopping the reader."""
    rt, got = await _route([_child_text("parent", "hello")], backend="claude")
    assert [m.params["sessionId"] for m in got["parent"]] == ["parent"]
    assert rt._announced_children == {}


@pytest.mark.asyncio
async def test_a_non_terminal_state_keeps_the_route() -> None:
    running = _update(
        "parent",
        {
            "sessionUpdate": "subagent_state_update",
            "subagentSessionId": "child-1",
            "state": "running",
        },
    )
    rt, _ = await _route([_spawned("parent", "child-1"), running])
    assert rt._announced_children == {"child-1": "parent"}


@pytest.mark.asyncio
async def test_lifecycle_frames_are_not_read_on_other_harnesses() -> None:
    _, events = await _events([_spawned("parent-0001", "child-1")], backend="")
    assert _rosters(events) == []


@pytest.mark.asyncio
async def test_a_legacy_row_remembers_only_bounded_ids_for_rows_it_made() -> None:
    from kiro_crew.acp.session_handle_native_children import LEGACY_ROW_TOOL_CALL_ID_CAP

    def start(tool_call_id: str, child: str) -> dict:
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": tool_call_id,
            "title": "Start subagent w",
            "rawInput": {"agentThreadId": child, "agentPath": "/root/w", "activityKind": "started"},
        }

    handle, _ = _handle()
    long_id = "x" * (LEGACY_ROW_TOOL_CALL_ID_CAP + 1)
    assert _rosters(handle._handle_legacy_codex_subagent(start(long_id, "child-1")))
    assert long_id not in handle._legacy_subagent_tool_ids
    # A child the native-child cap refuses gets no row and leaves no id behind.
    handle._native_child_sids = {f"c{i}" for i in range(NATIVE_CHILD_ROSTER_CAP)}
    assert handle._handle_legacy_codex_subagent(start("call-2", "over")) == []
    assert "call-2" not in handle._legacy_subagent_tool_ids


@pytest.mark.asyncio
async def test_a_finished_child_releases_its_route() -> None:
    """codex-acp ignores a finished child id afterwards, so its route goes too,
    and a late frame under it is not delivered."""
    done = _update(
        "parent",
        {
            "sessionUpdate": "subagent_state_update",
            "subagentSessionId": "child-1",
            "state": "completed",
        },
    )
    rt, got = await _route([_spawned("parent", "child-1"), done, _child_text("child-1", "late")])
    assert rt._announced_children == {}
    assert [m.params["sessionId"] for m in got["parent"]] == ["parent", "parent"]


@pytest.mark.asyncio
async def test_unregistering_the_owner_forgets_its_children() -> None:
    rt, _ = await _route(
        [_spawned("parent", "child-1"), _spawned("cotenant", "child-2")],
        sessions=("parent", "cotenant"),
    )
    rt.unregister_session("parent")
    assert rt._announced_children == {"child-2": "cotenant"}


@pytest.mark.asyncio
async def test_announcements_past_the_cap_are_counted_not_stored() -> None:
    rt, _, _ = _make_runtime()
    _register(rt, "parent")
    rt._announced_children = {f"c{i}": "parent" for i in range(NATIVE_CHILD_ROSTER_CAP)}
    assert rt._note_announced_child("parent", _spawned("parent", "one-too-many")["params"]) is False
    assert "one-too-many" not in rt._announced_children
    assert rt._announced_overflow == 1


@pytest.mark.asyncio
async def test_a_refused_announcement_draws_no_card_and_warns_once(caplog) -> None:
    """Past the route cap the announcement itself is dropped, so the owner never
    draws a card whose output could not reach it, and the first refusal of the
    episode is logged once."""
    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_CODEX
    queues = _register(rt, "parent")
    rt.mark_turn_active("parent", True)
    rt._announced_children = {f"c{i}": "other" for i in range(NATIVE_CHILD_ROSTER_CAP)}
    _register(rt, "other")
    task = await _start_reader(rt)
    try:
        with caplog.at_level("WARNING", logger="kiro_crew.acp.runtime"):
            for child in ("over-1", "over-2"):
                _feed(reader, _spawned("parent", child))
                _feed(reader, _child_text(child, "lost"))
            await _drain(reader)
    finally:
        await _stop_reader(task)
    assert queues["parent"].empty()
    assert rt._announced_overflow == 2
    assert sum("route map is full" in r.getMessage() for r in caplog.records) == 1


# ── 3. the session handle: lifecycle -> roster, child work -> activity ────────


def _handle(backend: str = ACP_BACKEND_CODEX):
    from kiro_crew.acp.session_handle import AcpSessionHandle

    queue: asyncio.Queue = asyncio.Queue()
    rt = MagicMock()
    rt.pid = None
    rt.is_alive = MagicMock(return_value=True)
    rt.send_notification = AsyncMock()
    rt.send_request = AsyncMock(return_value=1)
    rt.send_response = AsyncMock()
    rt.acp_backend = backend
    rt._last_activity = time.monotonic()
    handle = AcpSessionHandle("parent-0001", queue, rt)
    handle._turn_done.clear()
    return handle, queue


async def _events(frames: list[dict], backend: str = ACP_BACKEND_CODEX, handle=None):
    if handle is None:
        handle, queue = _handle(backend)
    else:
        queue = handle._queue
    for frame in frames:
        queue.put_nowait(JsonRpcMessage.from_dict(frame))
    queue.put_nowait(JsonRpcMessage(id=1, result={"stopReason": "end_turn"}))
    return handle, [ev async for ev in handle._dispatch_events(1, 5.0)]


def _rosters(events) -> list[list[dict]]:
    return [ev.subagents for ev in events if ev.kind == EVENT_SUBAGENT_LIST]


@pytest.mark.asyncio
async def test_recorded_native_child_becomes_one_card_with_its_own_output() -> None:
    handle, events = await _events(_fixture("announced-2.2.3-preview.23.jsonl"))

    rosters = _rosters(events)
    assert [r[0]["status"]["type"] for r in rosters] == ["working", "completed"]
    row = rosters[0][0]
    assert row["sessionId"] == "thread-0001"
    assert row["agentName"] == "Probe worker"
    assert row["initialQuery"] == "Delegated task for Probe worker"
    assert row["parentSessionId"] == ""

    child = [ev for ev in events if ev.kind == EVENT_SUBAGENT_ACTIVITY]
    assert {ev.sub_session_id for ev in child} == {"thread-0001"}
    child_tools = [ev.title for ev in child if ev.tool_call_id]
    assert len(child_tools) == 1 and "echo probe-child-ok" in child_tools[0]
    assert "".join(ev.text for ev in child if ev.text) == "probe-child-ok"

    # The parent's own transcript keeps its own text and its wait call, and
    # never the child's command or answer.
    parent_tools = [ev.title for ev in events if ev.kind == EVENT_TOOL_CALL]
    assert parent_tools == ["wait"]
    parent_text = "".join(ev.text for ev in events if ev.kind == EVENT_TEXT_CHUNK)
    assert "delegating" in parent_text
    assert handle.native_child_sessions == frozenset({"thread-0001"})


@pytest.mark.asyncio
async def test_recorded_legacy_rows_become_a_card_instead_of_tool_rows() -> None:
    _, events = await _events(_fixture("legacy-rows-2.2.2.jsonl"))
    rosters = _rosters(events)
    assert [r[0]["status"]["type"] for r in rosters] == ["working", "completed"]
    assert rosters[0][0]["sessionId"] == "thread-0001"
    assert rosters[0][0]["agentName"] == "Probe worker"
    # The Start/Complete rows are gone from the transcript; the wait stays.
    assert [ev.title for ev in events if ev.kind == EVENT_TOOL_CALL] == ["wait"]


@pytest.mark.asyncio
async def test_legacy_rows_are_left_alone_on_other_harnesses() -> None:
    _, events = await _events(_fixture("legacy-rows-2.2.2.jsonl"), backend="")
    assert _rosters(events) == []
    assert "Start subagent probe_worker" in [
        ev.title for ev in events if ev.kind == EVENT_TOOL_CALL
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("completed", {"type": "completed", "message": ""}),
        ("failed", {"type": "failed", "message": "failed"}),
        ("cancelled", {"type": "failed", "message": "cancelled"}),
        ("disconnected", {"type": "failed", "message": "disconnected"}),
    ],
)
async def test_codex_terminal_states(state: str, expected: dict) -> None:
    _, events = await _events(
        [
            _spawned("parent-0001", "child-1"),
            _update(
                "parent-0001",
                {
                    "sessionUpdate": "subagent_state_update",
                    "subagentSessionId": "child-1",
                    "state": state,
                },
            ),
        ]
    )
    assert _rosters(events)[-1][0]["status"] == expected


def test_route_release_and_card_status_agree_on_terminal_states() -> None:
    """The runtime releases a child's route on exactly the states the handle
    closes its card on, so the two cannot drift apart."""
    from kiro_crew.acp.session_handle_native_children import _LEGACY_STATE_STATUS
    from kiro_crew.acp.types import SUBAGENT_TERMINAL_STATES

    assert set(_LEGACY_STATE_STATUS) == set(SUBAGENT_TERMINAL_STATES)


@pytest.mark.asyncio
async def test_state_for_an_unannounced_child_is_dropped() -> None:
    handle, events = await _events(
        [
            _update(
                "parent-0001",
                {
                    "sessionUpdate": "subagent_state_update",
                    "subagentSessionId": "nobody",
                    "state": "failed",
                },
            )
        ]
    )
    assert _rosters(events) == []
    assert handle.native_child_sessions == frozenset()


@pytest.mark.asyncio
async def test_nested_child_names_its_parent() -> None:
    _, events = await _events(
        [_spawned("parent-0001", "child-1"), _spawned("child-1", "grandchild-1", "Nested worker")]
    )
    rows = {r["sessionId"]: r for r in _rosters(events)[-1]}
    assert rows["child-1"]["parentSessionId"] == ""
    assert rows["grandchild-1"]["parentSessionId"] == "child-1"
    # The nested announcement is not the child's transcript either.
    assert not [ev for ev in events if ev.kind == EVENT_SUBAGENT_ACTIVITY]


@pytest.mark.asyncio
async def test_a_lifecycle_shape_no_adapter_sends_draws_no_card() -> None:
    """Only codex-acp 2.2.x's shape is read. Any other ``sessionUpdate`` falls to
    the shared parser, which renders nothing for an unknown kind."""
    _, events = await _events(
        [_update("parent-0001", {"sessionUpdate": "subagent_update", "sessionId": "child-1"})]
    )
    assert _rosters(events) == []
    assert not [ev for ev in events if ev.kind == EVENT_SUBAGENT_ACTIVITY]


def _as_session(frames: list[dict], sid: str) -> list[dict]:
    """The fixture's frames with its synthetic parent id renamed to *sid*."""
    return [json.loads(json.dumps(f).replace("parent-0001", sid)) for f in frames]


async def _prompt_turns(turns: list[list[dict]], *, cotenant: bool = False) -> list[list]:
    """Real runtime, real reader loop, real ``prompt()``: one list of events per turn."""
    from kiro_crew.acp.session_handle import AcpSessionHandle
    from kiro_crew.testing.wait import await_until

    rt, reader, _ = _make_runtime()
    rt._acp_backend = ACP_BACKEND_CODEX
    queues = _register(rt, "sA", *(["sB"] if cotenant else []))
    handle = AcpSessionHandle("sA", queues["sA"], rt)
    task = await _start_reader(rt)
    out: list[list] = []
    try:
        for frames in turns:
            events: list = []

            async def drive(sink=events):
                async for ev in handle.prompt("hi", timeout=5.0):
                    sink.append(ev)

            driver = asyncio.ensure_future(drive())
            req_id = (await _await_routed(rt, "sA"))["sA"]
            for frame in frames:
                _feed(reader, frame)
            _feed(reader, {"id": req_id, "result": {"stopReason": "end_turn"}})
            await await_until(driver, what="the driver finishing its turn")
            out.append(events)
    finally:
        await _stop_reader(task)
    return out


@pytest.mark.asyncio
async def test_end_to_end_prompt_turn_on_a_shared_runtime() -> None:
    """The recorded child, through the real reader loop and prompt(), beside a
    co-tenant session: one card, the child's work on it, none in the parent."""
    (events,) = await _prompt_turns(
        [_as_session(_fixture("announced-2.2.3-preview.23.jsonl"), "sA")], cotenant=True
    )
    rosters = _rosters(events)
    assert rosters and rosters[-1][0]["status"]["type"] == "completed"
    child = [ev for ev in events if ev.kind == EVENT_SUBAGENT_ACTIVITY]
    assert {ev.sub_session_id for ev in child} == {"thread-0001"}
    assert "".join(ev.text for ev in child if ev.text) == "probe-child-ok"
    assert [ev.title for ev in events if ev.kind == EVENT_TOOL_CALL] == ["wait"]


@pytest.mark.asyncio
async def test_recorded_nested_child_and_continuation() -> None:
    """A child that spawns its own child, then a second message to the first child
    in a later turn. The grandchild's work lands on its own card, and the second
    run, which codex-acp announces under a new generation id, gets a fresh card."""
    turns = [
        _as_session(t, "sA") for t in _fixture_turns("nested-and-continued-2.2.3-preview.23.jsonl")
    ]
    first, second = await _prompt_turns(turns)

    rows = {r["sessionId"]: r for r in _rosters(first)[-1]}
    assert set(rows) == {"thread-0001", "thread-0002"}
    assert rows["thread-0001"]["agentName"] == "Lead worker"
    assert rows["thread-0002"]["agentName"] == "Leaf worker"
    assert rows["thread-0002"]["parentSessionId"] == "thread-0001"
    assert {r["status"]["type"] for r in rows.values()} == {"completed"}
    text = {}
    for ev in first:
        if ev.kind == EVENT_SUBAGENT_ACTIVITY and ev.text:
            text[ev.sub_session_id] = text.get(ev.sub_session_id, "") + ev.text
    assert "leaf-ok" in text["thread-0002"]

    (row,) = _rosters(second)[-1]
    assert row["sessionId"] == "thread-0001:generation:2"
    assert row["status"]["type"] == "completed"
    follow = "".join(ev.text for ev in second if ev.kind == EVENT_SUBAGENT_ACTIVITY and ev.text)
    assert "follow-up-ok" in follow


@pytest.mark.asyncio
async def test_recorded_cancellation_closes_the_card_as_cancelled() -> None:
    (events,) = await _prompt_turns(
        [_as_session(_fixture("cancelled-2.2.3-preview.23.jsonl"), "sA")]
    )
    assert _rosters(events)[-1][0]["status"] == {"type": "failed", "message": "cancelled"}
    state, slot, tracker = MagicMock(), MagicMock(), {}
    slot.key = "slot-1"
    for roster in _rosters(events):
        _native_subagent_sync(state, slot, roster, tracker)
    assert [c["error"] for c in _ws(state)["subagent_done"]] == ["cancelled"]


@pytest.mark.asyncio
async def test_roster_resets_each_turn() -> None:
    """A later turn's roster names only that turn's children, so the fresh
    per-turn card tracker does not resurrect a finished one."""
    _, second = await _prompt_turns([[_spawned("sA", "child-1")], [_spawned("sA", "child-2")]])
    assert [r["sessionId"] for r in _rosters(second)[-1]] == ["child-2"]


@pytest.mark.asyncio
async def test_roster_rows_stop_at_the_native_child_cap() -> None:
    handle, _ = _handle()
    handle._native_child_sids = {f"c{i}" for i in range(NATIVE_CHILD_ROSTER_CAP)}
    assert (
        handle._handle_announced_subagent(
            _spawned("parent-0001", "over")["params"]["update"], "parent-0001"
        )
        == []
    )
    assert "over" not in handle._announced_subagent_roster


# ── 4. the dashboard card, fed by the roster ─────────────────────────────────


def _ws(state) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for call in state.broadcast_ws.call_args_list:
        out.setdefault(call.args[0], []).append(call.args[1])
    return out


@pytest.mark.asyncio
async def test_roster_drives_spawn_and_done_cards() -> None:
    _, events = await _events(_fixture("announced-2.2.3-preview.23.jsonl"))
    state, slot, tracker = MagicMock(), MagicMock(), {}
    slot.key = "slot-1"
    for roster in _rosters(events):
        _native_subagent_sync(state, slot, roster, tracker)
    calls = _ws(state)
    assert [c["id"] for c in calls["subagent_spawn"]] == ["native:thread-0001"]
    assert calls["subagent_spawn"][0]["agent"] == "Probe worker"
    assert [c["error"] for c in calls["subagent_done"]] == [None]


@pytest.mark.asyncio
async def test_cancelled_child_card_says_so() -> None:
    _, events = await _events(
        [
            _spawned("parent-0001", "child-1"),
            _update(
                "parent-0001",
                {
                    "sessionUpdate": "subagent_state_update",
                    "subagentSessionId": "child-1",
                    "state": "cancelled",
                },
            ),
        ]
    )
    state, slot, tracker = MagicMock(), MagicMock(), {}
    slot.key = "slot-1"
    for roster in _rosters(events):
        _native_subagent_sync(state, slot, roster, tracker)
    assert [c["error"] for c in _ws(state)["subagent_done"]] == ["cancelled"]


@pytest.mark.asyncio
async def test_child_still_open_at_turn_end_is_closed_by_the_turn() -> None:
    _, events = await _events([_spawned("parent-0001", "child-1", "Worker")])
    state, slot, tracker = MagicMock(), MagicMock(), {}
    slot.key = "slot-1"
    for roster in _rosters(events):
        _native_subagent_sync(state, slot, roster, tracker)
    assert "subagent_done" not in _ws(state)
    _native_subagent_close_all(state, slot, tracker)
    assert [c["id"] for c in _ws(state)["subagent_done"]] == ["native:child-1"]
