"""Per-tab fold watch: the hub's watch sets and the exporter's bus subscriptions.

A socket that declared ``fold_delta.1`` (set through ``set_fold_delta_capable``)
receives a board's ``slot_projection`` frames only while it watches that board or
an ancestor of it. Every other owner socket is LEGACY and receives every board's
frames exactly as before. The crew-log exporter keeps its wildcard bus subscription
while any legacy socket is open, and while no capable one is, so with no capable
socket nothing changes: not the frames, not the subscription, not what the eager
folder folds.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from crew_log_fold_eager_helpers import (  # noqa: F401 -- the autouse fixture registers by import
    _isolated_home,
    _memo_keys,
    _slot_log,
    _work_entry,
)

from kiro_crew.crew_log import bus, eager, session_tree_projection
from kiro_crew.dashboard import websocket_hub as hub_mod
from kiro_crew.dashboard.handlers.crew_log import SLOT_FRAME, CrewLogPublisher
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.history import ConversationLog

WILDCARD = frozenset({("*", "*")})


def _cells(*boards: str) -> frozenset[tuple[str, str]]:
    """The keyed (key, work) cells for *boards*: the slot key and its session key.

    A watched root is subscribed under both spellings a fold may name it by; the
    descendants the tree returns are already in the tree's own spelling.
    """
    out = {(b, "work") for b in boards}
    out |= {(f"dashboard:{boards[0]}", "work")} if boards else set()
    return frozenset(out)


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
    """An owner dashboard-user socket that records what it was sent."""

    def __init__(self, *, dashboard_user: bool = True) -> None:
        super().__init__(_is_dashboard_user=dashboard_user)
        self.closed = False
        self.sent: list[str] = []

    # Hashed and compared by identity, like a real ``WebSocketResponse``.
    __hash__ = object.__hash__

    def __eq__(self, other: object) -> bool:
        return self is other

    async def send_str(self, msg: str) -> None:
        self.sent.append(msg)

    def frames(self, msg_type: str = SLOT_FRAME) -> list[dict]:
        out = [json.loads(m) for m in self.sent]
        return [f["data"] for f in out if f.get("type") == msg_type]


#: creator -> children, the slot tree the tests run against.
_TREE: dict[str, tuple[str, ...]] = {"A": ("A1",), "A1": ("A2",), "B": ()}


@pytest.fixture(autouse=True)
def _tree(_floor_monkeypatch):
    bus.reset_for_tests()
    parents = {child: parent for parent, kids in _TREE.items() for child in kids}
    patch = _floor_monkeypatch
    patch.setattr(session_tree_projection, "creator_of", lambda s: parents.get(s, ""))
    patch.setattr(session_tree_projection, "created_children", lambda s: _TREE.get(s, ()))
    patch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(seeded_for_current_store=True),
    )
    yield
    bus.reset_for_tests()


async def _settle() -> None:
    for _ in range(6):
        await asyncio.sleep(0)


class _Rig:
    """A real ``DashboardState`` and a real exporter bound to the running loop.

    Built lazily on first attribute access, because the exporter binds to the loop
    the test itself runs on, which a synchronous fixture has no access to.
    """

    def __init__(self, tmp_path) -> None:
        self._tmp = tmp_path
        self._built: SimpleNamespace | None = None

    def _build(self) -> SimpleNamespace:
        if self._built is None:
            state = _make_state(self._tmp)
            publisher = CrewLogPublisher(state)
            publisher.bind(asyncio.get_running_loop(), state)
            publisher.subscribe_bus()
            self._built = SimpleNamespace(state=state, publisher=publisher)
        return self._built

    @property
    def state(self) -> DashboardState:
        return self._build().state

    @property
    def publisher(self) -> CrewLogPublisher:
        return self._build().publisher

    def close(self) -> None:
        if self._built is not None:
            self._built.publisher.unsubscribe_bus()


@pytest.fixture
def rig(tmp_path):
    made = _Rig(tmp_path)
    yield made
    made.close()


def _open(state: DashboardState, *, capable: bool, watch=()) -> _FakeWS:
    ws = _FakeWS()
    state.register_ws(ws, owner=True)
    if capable:
        assert state.set_fold_delta_capable(ws, True)
        state.set_fold_watch(ws, list(watch))
    return ws


# --------------------------------------------------------------------------- #
# Hub: the watch set
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fold_watch_replaces_the_set(rig):
    ws = _open(rig.state, capable=True, watch=["A"])
    assert ws[hub_mod.FOLD_WATCH_WS_KEY] == frozenset({"A"})
    assert rig.state.set_fold_watch(ws, ["B"]) == frozenset({"B"})
    assert ws[hub_mod.FOLD_WATCH_WS_KEY] == frozenset({"B"}), "a watch REPLACES, not adds"
    assert rig.state.set_fold_watch(ws, []) == frozenset()


@pytest.mark.asyncio
async def test_unknown_and_malformed_slot_keys_are_ignored(rig):
    ws = _open(rig.state, capable=True)
    kept = rig.state.set_fold_watch(
        ws, ["A", "", 7, None, "x" * 300, "bad\nkey", {"k": 1}, "A", "no-such-board"]
    )
    assert kept == frozenset({"A", "no-such-board"})
    # A board nobody publishes matches no frame, so it is harmless.
    rig.state.broadcast_ws_owners(SLOT_FRAME, {"slot": "B", "fold": "work", "revision": 1})
    await _settle()
    assert ws.frames() == []
    # A watch that is not a list is ignored as a whole.
    assert rig.state.set_fold_watch(ws, "A") == frozenset()
    many = [f"s{i}" for i in range(200)]
    assert len(rig.state.set_fold_watch(ws, many)) == hub_mod.MAX_FOLD_WATCH_SLOTS


@pytest.mark.asyncio
async def test_only_a_dashboard_user_socket_can_be_capable(rig):
    app = _FakeWS(dashboard_user=False)
    rig.state.register_ws(app, owner=True)
    assert rig.state.set_fold_delta_capable(app, True) is False
    assert rig.state.set_fold_watch(app, ["A"]) == frozenset(), "a legacy socket keeps no set"


# --------------------------------------------------------------------------- #
# Hub: the fan-out
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_slot_frames_go_to_legacy_sockets_and_to_watchers_of_the_board_or_an_ancestor(rig):
    legacy = _open(rig.state, capable=False)
    on_a = _open(rig.state, capable=True, watch=["A"])
    on_b = _open(rig.state, capable=True, watch=["B"])
    idle = _open(rig.state, capable=True)
    for slot in ("A", "A2", "B", "C"):
        rig.state.broadcast_ws_owners(SLOT_FRAME, {"slot": slot, "fold": "work", "revision": 1})
    rig.state.broadcast_ws_owners(SLOT_FRAME, {"slot": "B"})  # the growth backstop's frame
    rig.state.broadcast_ws_owners("session_projection", {"session_id": "s", "slot": "C"})
    await _settle()
    assert [f["slot"] for f in legacy.frames()] == ["A", "A2", "B", "C", "B"]
    assert [f["slot"] for f in on_a.frames()] == ["A", "A2"], "A2 is A's grandchild"
    assert [f["slot"] for f in on_b.frames()] == ["B", "B"]
    assert idle.frames() == [], "a capable socket watching nothing gets no slot frame"
    for ws in (legacy, on_a, on_b, idle):
        assert len(ws.frames("session_projection")) == 1, "session frames are not routed"


@pytest.mark.asyncio
async def test_without_a_capable_socket_the_owner_send_is_exactly_todays(rig, monkeypatch):
    """Byte-identical: one ``_send_ws_owners`` call with the same serialized frame."""
    sockets = [_open(rig.state, capable=False) for _ in range(3)]
    calls: list[tuple] = []
    real = rig.state._send_ws_owners

    def spy(msg, **kwargs):
        calls.append((msg, kwargs))
        real(msg, **kwargs)

    monkeypatch.setattr(rig.state, "_send_ws_owners", spy)
    data = {"slot": "A", "fold": "work", "revision": 3, "value": {"items": []}}
    rig.state.broadcast_ws_owners(SLOT_FRAME, data)
    await _settle()
    expected = json.dumps({"type": SLOT_FRAME, "data": data})
    assert calls == [(expected, {})]
    for ws in sockets:
        assert ws.sent == [expected]


@pytest.mark.asyncio
async def test_a_socket_that_closes_while_watching_stops_counting(rig):
    on_a = _open(rig.state, capable=True, watch=["A"])
    assert rig.publisher.slot_subscription_cells() == _cells("A", "A1", "A2")
    rig.state.unregister_ws(on_a)
    assert rig.state.fold_watch_routing() == (False, False, frozenset())
    assert rig.publisher.slot_subscription_cells() == WILDCARD


# --------------------------------------------------------------------------- #
# Exporter: wildcard <-> keyed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_exporter_switches_between_wildcard_and_keyed(rig):
    p = rig.publisher
    assert p.slot_subscription_cells() == WILDCARD, "no tab: today's subscription"
    assert not bus._slot_folds_on_demand

    legacy = _open(rig.state, capable=False)
    assert p.slot_subscription_cells() == WILDCARD
    on_b = _open(rig.state, capable=True, watch=["B"])
    assert p.slot_subscription_cells() == WILDCARD, "a legacy tab still needs every fold"
    assert not bus._slot_folds_on_demand

    rig.state.unregister_ws(legacy)
    assert p.slot_subscription_cells() == _cells("B")
    assert bus._slot_folds_on_demand

    rig.state.set_fold_watch(on_b, ["A"])
    assert p.slot_subscription_cells() == _cells("A", "A1", "A2")

    again = _open(rig.state, capable=False)  # a reconnect from an old bundle
    assert p.slot_subscription_cells() == WILDCARD
    assert not bus._slot_folds_on_demand
    rig.state.unregister_ws(again)
    rig.state.unregister_ws(on_b)
    assert p.slot_subscription_cells() == WILDCARD
    assert not bus._slot_folds_on_demand
    # Exactly one slot subscription is registered in wildcard mode, as before.
    assert bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key="Z", fold="radar")


@pytest.mark.asyncio
async def test_an_unseeded_tree_keeps_the_wildcard(rig, monkeypatch):
    monkeypatch.setattr(
        session_tree_projection,
        "projection",
        lambda: SimpleNamespace(seeded_for_current_store=False),
    )
    _open(rig.state, capable=True, watch=["A"])
    assert rig.publisher.slot_subscription_cells() == WILDCARD
    assert not bus._slot_folds_on_demand


@pytest.mark.asyncio
async def test_unsubscribe_bus_drops_every_slot_subscription_and_on_demand(rig):
    _open(rig.state, capable=True, watch=["B"])
    assert bus._slot_folds_on_demand
    rig.publisher.unsubscribe_bus()
    assert rig.publisher.slot_subscription_cells() == frozenset()
    assert not bus._slot_folds_on_demand
    assert not bus.has_subscriber(bus.FOLD_ADVANCED, scope="slot", key="B", fold="work")
    rig.state.set_fold_watch(next(iter(rig.state._owner_ws_clients)), ["A"])
    assert rig.publisher.slot_subscription_cells() == frozenset(), "the listener is gone"


# --------------------------------------------------------------------------- #
# Real eager folder -> real bus -> real exporter -> real hub
# --------------------------------------------------------------------------- #


async def _report(unit: str, board: str, item: str) -> None:
    handle = _slot_log(unit, board)
    seq = _work_entry(handle, item, slot=board)
    eager.note_commit(unit, "work/recorded", seq, board=board)
    assert await asyncio.to_thread(eager.drain, 10.0)
    await _settle()


@pytest.mark.asyncio
async def test_two_watchers_get_only_their_own_boards_and_unwatched_boards_are_not_folded(rig):
    on_a = _open(rig.state, capable=True, watch=["A"])
    on_b = _open(rig.state, capable=True, watch=["B"])
    await _report("s-a", "A", "it-a")
    await _report("s-b", "B", "it-b")
    await _report("s-c", "C", "it-c")
    assert [(f["slot"], f["fold"]) for f in on_a.frames()] == [("A", "work")]
    assert [(f["slot"], f["fold"]) for f in on_b.frames()] == [("B", "work")]
    assert on_a.frames()[0]["value"]["items"][0]["item_id"] == "it-a"
    warm = _memo_keys()
    assert ("C", "work") not in warm, "a board no tab watches was folded anyway"
    assert {fold for slot, fold in warm if slot == "A"} == {"work"}, "only work is wanted"


@pytest.mark.asyncio
async def test_a_legacy_tab_beside_watchers_gets_every_board_and_every_fold(rig):
    legacy = _open(rig.state, capable=False)
    on_a = _open(rig.state, capable=True, watch=["A"])
    await _report("s-a2", "A", "it-a")
    await _report("s-c2", "C", "it-c")
    boards = {f["slot"] for f in legacy.frames()}
    assert boards == {"A", "C"}
    assert {f["fold"] for f in legacy.frames()} > {"work"}, "every woken fold still renders"
    assert ("C", "work") in _memo_keys()
    assert {f["slot"] for f in on_a.frames()} == {"A"}


# --------------------------------------------------------------------------- #
# A fold names its board by session key; a tab watches by slot key
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_frame_under_the_session_key_reaches_the_tab_watching_the_slot_key(rig):
    """``dashboard:A`` and a channel key reach the tab that watches ``A`` / its slot name."""
    on_a = _open(rig.state, capable=True, watch=["A"])
    on_ch = _open(rig.state, capable=True, watch=["discord_agent_direct_123"])
    rig.state.broadcast_ws_owners(SLOT_FRAME, {"slot": "dashboard:A", "fold": "work"})
    rig.state.broadcast_ws_owners(SLOT_FRAME, {"slot": "discord:agent:direct:123", "fold": "work"})
    await _settle()
    assert [f["slot"] for f in on_a.frames()] == ["dashboard:A"]
    assert [f["slot"] for f in on_ch.frames()] == ["discord:agent:direct:123"]


@pytest.mark.asyncio
async def test_a_watch_in_session_key_form_is_stored_as_the_slot_key(rig):
    ws = _open(rig.state, capable=True)
    assert rig.state.set_fold_watch(ws, ["dashboard:A", "A"]) == frozenset({"A"})


@pytest.mark.asyncio
async def test_a_channel_board_subscribes_under_the_live_slots_session_key(rig):
    slot = SimpleNamespace(
        key="discord_agent_direct_123", linked_session_key="discord:agent:direct:123"
    )
    rig.state._slots["discord_agent_direct_123"] = slot
    _open(rig.state, capable=True, watch=["discord_agent_direct_123"])
    cells = rig.publisher.slot_subscription_cells()
    assert ("discord:agent:direct:123", "work") in cells
    assert bus.has_subscriber(
        bus.FOLD_ADVANCED, scope="slot", key="discord:agent:direct:123", fold="work"
    )


@pytest.mark.asyncio
async def test_a_channel_board_with_no_live_slot_keeps_the_wildcard(rig):
    """The channel fold is not reversible: with no slot to read the key from, fold all."""
    _open(rig.state, capable=True, watch=["discord_agent_direct_999"])
    assert rig.publisher.slot_subscription_cells() == WILDCARD
    assert not bus._slot_folds_on_demand


@pytest.mark.asyncio
async def test_a_dashboard_board_reported_under_its_session_key_is_folded_and_sent(rig):
    """Real eager folder: a work entry naming ``dashboard:A`` reaches the tab watching ``A``."""
    on_a = _open(rig.state, capable=True, watch=["A"])
    await _report("s-dash-a", "dashboard:A", "it-a")
    assert [(f["slot"], f["fold"]) for f in on_a.frames()] == [("dashboard:A", "work")]
    assert ("dashboard:A", "work") in _memo_keys()
