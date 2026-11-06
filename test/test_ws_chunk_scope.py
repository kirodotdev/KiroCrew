"""Per-tab stream scoping: ``chunk_scope.1`` sockets get a slot's stream only while they watch it.

A socket that declared the capability receives ``chat_chunk``, ``chat_thinking``,
``tool_call`` and ``tool_result`` for a slot only while its ``chunk_watch`` set names
that slot. For any other slot it gets one ``chat_phase`` frame per phase transition,
a slim ``tool_status`` frame per tool call and one per return, and its reasoning
held back into one ``chat_thinking`` frame per run. Coarse frames reach every socket, and
a socket without the capability receives every frame exactly as before.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.dashboard import ws_chunk_scope as scope
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog


def _make_state(tmp_path) -> DashboardState:
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    sessions.remove = AsyncMock()
    return DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path / "convo"),
    )


class _FakeWS(dict):
    """A dashboard-user socket that records what it was sent."""

    def __init__(self, *, dashboard_user: bool = True) -> None:
        super().__init__(_is_dashboard_user=dashboard_user)
        self.closed = False
        self.sent: list[str] = []

    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)

    def types(self) -> list[str]:
        return [json.loads(m)["type"] for m in self.sent]

    def frames(self, msg_type: str) -> list[dict]:
        out = [json.loads(m) for m in self.sent]
        return [f["data"] for f in out if f["type"] == msg_type]


async def _settle(state: DashboardState) -> None:
    """Await every send the fan-out spawned, so ``sent`` holds them all."""
    pending = [t for t in list(state._background_tasks) if not t.done()]
    if pending:
        await asyncio.gather(*pending)


def _open(state: DashboardState, *, capable: bool, watch=None) -> _FakeWS:
    ws = _FakeWS()
    state.register_ws(ws, owner=True)
    state.set_chunk_scope_capable(ws, capable)
    if watch is not None:
        state.set_chunk_watch(ws, list(watch))
    return ws


def _stream(state: DashboardState, slot: str) -> None:
    """One turn's worth of streamed frames for *slot*."""
    state.broadcast_ws("chat_thinking", {"slot": slot, "content": "hm"})
    state.broadcast_ws("chat_thinking", {"slot": slot, "content": "m"})
    state.broadcast_ws(
        "tool_call",
        {
            "slot": slot,
            "tool": "Shell",
            "kind": "execute",
            "tool_call_id": "t1",
            "input_preview": "ls",
        },
    )
    state.broadcast_ws("tool_result", {"slot": slot, "tool_call_id": "t1", "output": "x" * 100})
    for seq in (1, 2, 3):
        state.broadcast_ws("chat_chunk", {"slot": slot, "content": "ab", "seq": seq, "gen": "g"})


@pytest.fixture
def state(tmp_path):
    return _make_state(tmp_path)


@pytest.mark.asyncio
async def test_a_socket_without_the_capability_gets_every_stream_frame(state):
    ws = _open(state, capable=False)
    _stream(state, "A")
    await _settle(state)
    assert ws.types() == ["chat_thinking"] * 2 + ["tool_call", "tool_result"] + ["chat_chunk"] * 3


@pytest.mark.asyncio
async def test_a_capable_socket_gets_the_full_stream_until_its_first_watch(state):
    # Nothing is held back before the tab says what it shows: a client that
    # never reads the ack could not recover chunks dropped in that window.
    ws = _open(state, capable=True)
    _stream(state, "A")
    await _settle(state)
    assert ws.types() == ["chat_thinking"] * 2 + ["tool_call", "tool_result"] + ["chat_chunk"] * 3
    state.set_chunk_watch(ws, [])
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "x", "seq": 4, "gen": "g"})
    await _settle(state)
    assert ws.types()[-1] == "chat_phase"


@pytest.mark.asyncio
async def test_a_watching_socket_gets_the_full_stream(state):
    ws = _open(state, capable=True, watch=["A"])
    _stream(state, "A")
    await _settle(state)
    assert ws.types() == ["chat_thinking"] * 2 + ["tool_call", "tool_result"] + ["chat_chunk"] * 3


@pytest.mark.asyncio
async def test_an_unwatched_slot_reaches_a_scoped_socket_as_phase_transitions_only(state):
    ws = _open(state, capable=True, watch=["B"])
    _stream(state, "A")
    await _settle(state)
    assert ws.types() == ["chat_phase", "chat_thinking", "tool_status", "tool_status", "chat_phase"]
    # The run's reasoning goes out whole, ahead of the slot's next frame.
    assert ws.frames("chat_thinking") == [{"slot": "A", "content": "hmm"}]
    assert ws.frames("chat_phase") == [
        {"slot": "A", "phase": "thinking"},
        {"slot": "A", "phase": "streaming"},
    ]
    # The call, then its return: the status line moves off it and no tool
    # output is sent, so a tool log the tab cached keeps its output.
    assert ws.frames("tool_status") == [
        {
            "slot": "A",
            "tool": "Shell",
            "kind": "execute",
            "tool_call_id": "t1",
            "input_preview": "ls",
        },
        {"slot": "A", "tool_call_id": "t1", "returned": True},
    ]


@pytest.mark.asyncio
async def test_an_unwatched_tool_result_keeps_only_the_output_scrollback_copies(state):
    ws = _open(state, capable=True, watch=[])
    launch = "Spawned 2 subagent(s). Results will arrive as completion events."
    answered = 'User has answered your questions:\n"Q" -> "A"'
    state.broadcast_ws("tool_result", {"slot": "A", "tool_call_id": "s", "output": launch})
    state.broadcast_ws("tool_result", {"slot": "A", "tool_call_id": "q", "output": answered})
    state.broadcast_ws(
        "tool_result", {"slot": "A", "tool_call_id": "p", "output": "y", "final": False}
    )
    state.broadcast_ws("tool_result", {"slot": "A", "output": "no id"})
    await _settle(state)
    # A mid-stream result is not a return, and one without an id matches no call.
    assert ws.types() == ["tool_status", "tool_status"]
    assert ws.frames("tool_status") == [
        {"slot": "A", "tool_call_id": "s", "returned": True, "output": launch},
        {"slot": "A", "tool_call_id": "q", "returned": True, "output": answered},
    ]


@pytest.mark.asyncio
async def test_an_unwatched_tool_status_keeps_the_auto_approval_mark(state):
    ws = _open(state, capable=True, watch=[])
    state.broadcast_ws(
        "tool_call", {"slot": "A", "tool": "Shell", "tool_call_id": "t1", "auto": True}
    )
    await _settle(state)
    assert ws.frames("tool_status")[0]["auto"] is True


@pytest.mark.asyncio
async def test_a_refinement_after_an_unwatched_result_is_dropped_until_the_turn_ends(state):
    ws = _open(state, capable=True, watch=[])
    call = {"slot": "A", "tool": "Shell", "tool_call_id": "t1"}
    state.broadcast_ws("tool_call", call)
    state.broadcast_ws("tool_result", {"slot": "A", "tool_call_id": "t1", "output": "x"})
    state.broadcast_ws("tool_call", {**call, "is_update": True})
    state.broadcast_ws("chat_thinking", {"slot": "A", "content": "hm"})
    state.broadcast_ws("chat_done", {"slot": "A"})
    # A new turn may reuse the id, so the finished set ends with the turn.
    state.broadcast_ws("tool_call", {**call, "is_update": True})
    await _settle(state)
    assert ws.types() == [
        "tool_status",
        "tool_status",
        "chat_phase",
        "chat_thinking",
        "chat_done",
        "tool_status",
    ]


@pytest.mark.asyncio
async def test_held_reasoning_goes_out_when_the_slot_becomes_watched(state, monkeypatch):
    monkeypatch.setattr(state, "get_slot", lambda key: SimpleNamespace(running=True))
    ws = _open(state, capable=True, watch=[])
    state.broadcast_ws("chat_thinking", {"slot": "A", "content": "first "})
    state.broadcast_ws("chat_thinking", {"slot": "A", "content": "second"})
    await _settle(state)
    assert ws.types() == ["chat_phase"]
    # Reasoning is never persisted, so the transcript re-read cannot bring it
    # back: it is sent before the ack, and the live stream follows.
    assert state.set_chunk_watch(ws, ["A"])["catchup"] == ["A"]
    state.broadcast_ws("chat_thinking", {"slot": "A", "content": " third"})
    await _settle(state)
    assert ws.types() == ["chat_phase", "chat_thinking", "chat_thinking"]
    assert ws.frames("chat_thinking") == [
        {"slot": "A", "content": "first second"},
        {"slot": "A", "content": " third"},
    ]


@pytest.mark.asyncio
async def test_held_reasoning_goes_out_once_it_passes_the_size_cap(state):
    ws = _open(state, capable=True, watch=[])
    part = "x" * (scope._THINK_FLUSH_CHARS // 2)
    for _ in range(3):
        state.broadcast_ws("chat_thinking", {"slot": "A", "content": part})
    state.broadcast_ws("chat_done", {"slot": "A"})
    await _settle(state)
    assert ws.types() == ["chat_phase", "chat_thinking", "chat_thinking", "chat_done"]
    assert [len(f["content"]) for f in ws.frames("chat_thinking")] == [
        2 * len(part),
        len(part),
    ]


@pytest.mark.asyncio
async def test_coarse_frames_reach_every_socket_and_reset_the_phase(state):
    scoped = _open(state, capable=True, watch=[])
    legacy = _open(state, capable=False)
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "a", "seq": 1})
    state.broadcast_ws("chat_status", {"slot": "A", "status": "Compacting"})
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "b", "seq": 2})
    state.broadcast_ws("chat_done", {"slot": "A"})
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "c", "seq": 3})
    await _settle(state)
    # A label replaces the line and a turn end clears it, so the chunk after
    # each is a transition again.
    assert scoped.types() == [
        "chat_phase",
        "chat_status",
        "chat_phase",
        "chat_done",
        "chat_phase",
    ]
    assert legacy.types() == ["chat_chunk", "chat_status", "chat_chunk", "chat_done", "chat_chunk"]


@pytest.mark.asyncio
async def test_a_watch_change_mid_turn_switches_the_socket_between_full_and_coarse(state):
    ws = _open(state, capable=True, watch=[])
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "a", "seq": 1})
    state.set_chunk_watch(ws, ["A"])
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "b", "seq": 2})
    state.set_chunk_watch(ws, [])
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "c", "seq": 3})
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "d", "seq": 4})
    await _settle(state)
    assert ws.types() == ["chat_phase", "chat_chunk", "chat_phase"]
    assert ws.frames("chat_chunk") == [{"slot": "A", "content": "b", "seq": 2}]


@pytest.mark.asyncio
async def test_a_closed_socket_drops_its_watch_and_gets_nothing_more(state):
    ws = _open(state, capable=True, watch=["A"])
    other = _open(state, capable=True, watch=["A"])
    ws.closed = True
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "a", "seq": 1})
    await _settle(state)
    assert ws.sent == []
    assert scope.CHUNK_WATCH_WS_KEY not in ws
    assert scope.CHUNK_PHASE_WS_KEY not in ws
    assert ws not in state._ws_clients
    assert other.types() == ["chat_chunk"]


def test_the_ack_names_newly_watched_running_slots_for_catch_up(state, monkeypatch):
    running = {"A"}
    monkeypatch.setattr(
        state,
        "get_slot",
        lambda key: SimpleNamespace(running=key in running) if key in ("A", "B") else None,
    )
    ws = _open(state, capable=True)
    assert state.set_chunk_watch(ws, ["A", "B", "gone"]) == {
        "slots": ["A", "B", "gone"],
        "catchup": ["A"],
    }
    # Already watched: nothing to catch up on, its chunks were never withheld.
    assert state.set_chunk_watch(ws, ["A"]) == {"slots": ["A"], "catchup": []}


def test_a_linked_channel_turn_counts_as_running_for_catch_up(state, monkeypatch):
    # A Slack or Discord turn streams into the slot with ``running`` false.
    monkeypatch.setattr(
        state,
        "get_slot",
        lambda key: SimpleNamespace(running=False, linked_turn_running=key == "L"),
    )
    ws = _open(state, capable=True)
    assert state.set_chunk_watch(ws, ["L", "idle"])["catchup"] == ["L"]


def test_set_chunk_watch_is_a_no_op_without_the_capability(state):
    ws = _open(state, capable=False)
    assert state.set_chunk_watch(ws, ["A"]) is None
    app_ws = _FakeWS(dashboard_user=False)
    assert state.set_chunk_scope_capable(app_ws, True) is False


def test_the_watch_set_drops_invalid_keys_and_is_capped():
    slots = ["ok", "", 7, "x" * (scope.MAX_CHUNK_WATCH_KEY_LEN + 1), "bad\n", "ok"]
    slots += [f"s{i}" for i in range(200)]
    kept = scope.clean_watch(slots)
    assert "ok" in kept and len(kept) == scope.MAX_CHUNK_WATCH_SLOTS
    assert scope.clean_watch("not-a-list") == frozenset()


def test_a_large_tool_input_is_slimmed_but_keeps_what_a_title_reads():
    body = "y" * 50_000
    preview = json.dumps({"path": "/src/app.py", "content": body, "ops": [{"path": "/a"}] * 40})
    slim = json.loads(scope.slim_preview(preview))
    assert slim["path"] == "/src/app.py"
    assert len(slim["content"]) == 512
    assert len(slim["ops"]) == 16
    diff = "--- a/x.py\n+++ b/x.py\n" + "+" * 10_000
    assert scope.slim_preview(diff).startswith("--- a/x.py\n+++ b/x.py")
    assert scope.slim_preview("z" * 10_000) == ""
    assert scope.slim_preview("short") == "short"


@pytest.mark.asyncio
async def test_each_coarse_frame_is_serialized_once_per_fan_out(state, monkeypatch):
    sockets = [_open(state, capable=True, watch=[]) for _ in range(5)]
    calls = []

    def counting_dumps(obj, *args, **kwargs):
        calls.append(obj)
        return json.dumps(obj, *args, **kwargs)

    # Only this module's encodes, so another module's json.dumps on the loop
    # cannot land in the count.
    monkeypatch.setattr(scope, "json", SimpleNamespace(dumps=counting_dumps, loads=json.loads))
    state.broadcast_ws("chat_chunk", {"slot": "A", "content": "a", "seq": 1})
    await _settle(state)
    # ONE coarse frame for five sockets.
    assert [c["type"] for c in calls] == ["chat_phase"]
    assert all(s.types() == ["chat_phase"] for s in sockets)
    assert all(ws.types() == ["chat_phase"] for ws in sockets)


# --------------------------------------------------------------------------- #
# The real ``/api/ws`` handler: caps parsing, ``chunk_watch`` and its ack
# --------------------------------------------------------------------------- #


@pytest.fixture
def e2e_state(tmp_path, monkeypatch):
    from chat_test_helpers import _make_state as _chat_state

    from kiro_crew.dashboard import revocation_gen, token_auth
    from kiro_crew.dashboard.handlers import updates

    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"chunk-scope-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    monkeypatch.setattr(
        "kiro_crew.dashboard.ws.load_declared_events_for_connect",
        lambda _app: (True, frozenset()),
    )
    st = _chat_state(tmp_path)
    monkeypatch.setattr(st, "status_snapshot", lambda **_kwargs: {})
    yield st
    stopped.set()


async def _until(socket, msg_type: str) -> list[dict]:
    from kiro_crew.testing.wait import finished

    seen = []
    while True:
        frame = await finished(socket.receive_json(), f"a {msg_type} frame arrives")
        seen.append(frame)
        if frame.get("type") == msg_type:
            return seen


@pytest.mark.asyncio
async def test_the_handler_routes_a_declaring_tab_by_its_chunk_watch(e2e_state, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer
    from chat_test_helpers import _make_app

    from kiro_crew.dashboard import token_auth
    from kiro_crew.dashboard.ws import api_ws

    monkeypatch.setattr(
        e2e_state, "get_slot", lambda key: SimpleNamespace(running=True) if key == "A" else None
    )
    app = _make_app(e2e_state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)
    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        token = token_auth.generate_token("local-app")
        new_tab = await client.ws_connect(
            "/api/ws", params={"token": token, "caps": f"slot_patch,{scope.CHUNK_SCOPE_CAPABILITY}"}
        )
        old_tab = await client.ws_connect("/api/ws", params={"token": token, "caps": "slot_patch"})
        await new_tab.send_json({"type": "chunk_watch", "slots": ["A"]})
        ack = (await _until(new_tab, scope.CHUNK_WATCH_ACK_FRAME))[-1]
        assert ack["data"] == {"slots": ["A"], "catchup": ["A"]}
        # The old tab declared nothing, so its chunk_watch is ignored and unanswered.
        await old_tab.send_json({"type": "chunk_watch", "slots": ["A"]})

        e2e_state.broadcast_ws("chat_chunk", {"slot": "B", "content": "b", "seq": 1})
        e2e_state.broadcast_ws("chat_chunk", {"slot": "A", "content": "a", "seq": 1})
        new_frames = await _until(new_tab, "chat_chunk")
        old_frames = await _until(old_tab, "chat_chunk")
        old_frames += await _until(old_tab, "chat_chunk")

        assert [f["data"] for f in new_frames if f["type"] == "chat_phase"] == [
            {"slot": "B", "phase": "streaming"}
        ]
        assert new_frames[-1]["data"]["slot"] == "A"
        assert [f["data"]["slot"] for f in old_frames if f["type"] == "chat_chunk"] == ["B", "A"]
        assert not [f for f in old_frames if f["type"] == scope.CHUNK_WATCH_ACK_FRAME]
        await new_tab.close()
        await old_tab.close()


@pytest.mark.asyncio
async def test_tool_status_is_audited_under_its_own_type(state, monkeypatch):
    import kiro_crew.dashboard.ws_event_scope as event_scope

    granted: list[str] = []
    monkeypatch.setattr(event_scope, "_audit_allow", lambda who, frame: granted.append(frame))
    scoped = _open(state, capable=True, watch=[])
    legacy = _open(state, capable=False)
    state.broadcast_ws("tool_call", {"slot": "A", "tool": "Shell", "tool_call_id": "t1"})
    await _settle(state)
    assert scoped.types() == ["tool_status"]
    assert legacy.types() == ["tool_call"]
    # One grant per socket, each under the frame that socket received.
    assert sorted(granted) == ["tool_call", "tool_status"]


def test_the_disabled_caps_switch_turns_scoping_off(monkeypatch):
    # The one switch for every ws capability; ws.py applies it before asking.
    from kiro_crew.dashboard import ws_slot_diff

    caps = {"slot_patch", scope.CHUNK_SCOPE_CAPABILITY}
    assert scope.declared(ws_slot_diff.without_disabled_caps(caps))
    monkeypatch.setenv(ws_slot_diff.DISABLED_CAPS_ENV, f"other, {scope.CHUNK_SCOPE_CAPABILITY}")
    assert not scope.declared(ws_slot_diff.without_disabled_caps(caps))


@pytest.mark.asyncio
async def test_the_phase_map_starts_over_past_its_cap(state):
    ws = _open(state, capable=True, watch=[])
    for i in range(scope._MAX_PHASE_ENTRIES + 1):
        state.broadcast_ws("chat_chunk", {"slot": f"s{i}", "content": "a", "seq": 1})
    # Starting over forgets only what was already sent: the next chunk of an
    # early slot is announced again, never withheld.
    state.broadcast_ws("chat_chunk", {"slot": "s0", "content": "b", "seq": 2})
    await _settle(state)
    assert ws.types().count("chat_phase") == scope._MAX_PHASE_ENTRIES + 2
    assert len(ws[scope.CHUNK_PHASE_WS_KEY]) <= scope._MAX_PHASE_ENTRIES + 1
