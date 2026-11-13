"""Connects and slot-list GETs that ask together share ``serialize_slots`` passes.

A stalled loop drops every dashboard tab's socket at once, and each tab's reconnect
sends an ``/api/ws`` connect and a ``GET /api/chat/slots``. One full serialization
on the loop per request prolonged the stall; ``slot_list_pass.shared_slot_list``
takes a pass at once for a caller after a quiet window and one trailing pass for
every caller of the same audience inside it.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import slot_list_pass
from kiro_crew.dashboard.slot_list_pass import shared_slot_list
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.testing.wait import async_wait_until, await_until


@pytest.fixture
def state(_floor_monkeypatch, tmp_path) -> DashboardState:
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


@pytest.fixture
def passes(state, _floor_monkeypatch) -> list[dict]:
    """Every ``serialize_slots`` call, by its audience; each returns a fresh list."""
    calls: list[dict] = []

    def _serialize(*, include_check_status: bool = False, dashboard_user: bool = False) -> list:
        calls.append(
            {"include_check_status": include_check_status, "dashboard_user": dashboard_user}
        )
        return [{"key": f"chat-{len(calls)}", "title": "listed"}]

    _floor_monkeypatch.setattr(state, "serialize_slots", _serialize)
    return calls


def _slots_get(state: DashboardState):
    from aiohttp.test_utils import make_mocked_request

    request = make_mocked_request("GET", "/api/chat/slots", app={"state": state})
    request["is_dashboard_user"] = True
    return request


@pytest.fixture
def owner_get(_floor_monkeypatch):
    from kiro_crew.dashboard.handlers import source_providers

    async def _no_allowlist() -> None:
        return None

    _floor_monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _no_allowlist)
    _floor_monkeypatch.setattr(source_providers, "is_owner_dashboard_request", lambda _r: True)
    _floor_monkeypatch.setattr(source_providers, "schedule_visibility_refresh", MagicMock())


@pytest.fixture
def wide_window(_floor_monkeypatch) -> None:
    """A window no test step outlasts, so callers a test starts together share a pass."""
    _floor_monkeypatch.setattr(
        slot_list_pass, "PASS_WINDOW_MIN_S", 0.5  # flake-ok: a window waited out, not a bound
    )


def _keys(response) -> list[str]:
    return [row["key"] for row in json.loads(response.text)]


class _Socket:
    """An ``/api/ws`` socket double that keeps every frame sent to it. Its peer sends
    nothing, so the handler ends once the connect frames are out."""

    def __init__(self) -> None:
        self.closed = False
        self.sent: list[dict] = []
        self._flags: dict = {}

    def __setitem__(self, key: str, value) -> None:
        self._flags[key] = value

    def __getitem__(self, key: str):
        return self._flags[key]

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def prepare(self, request) -> None:
        return None

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)

    async def send_str(self, payload: str) -> None:
        self.sent.append(json.loads(payload))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration


def _first_frame(ws: _Socket) -> tuple[str, list[str]]:
    """The type of a socket's first frame and the keys of the rows it carries."""
    if not ws.sent:
        return "nothing sent", []
    return ws.sent[0]["type"], [row["key"] for row in ws.sent[0].get("data", [])]


@pytest.fixture
def tabs(_floor_monkeypatch, wide_window) -> SimpleNamespace:
    """Dashboard tabs that connect through ``api_ws`` to a state double.

    ``connect()`` opens one tab's socket and returns its handler's task. ``sockets``
    holds the sockets in the order they were opened, each a ``socket_type``, and
    ``registered`` is the registry ``register_ws`` and ``unregister_ws`` keep, which a
    broadcast reads.
    ``passes`` counts the ``serialize_slots`` passes, and each pass runs
    ``during_pass`` before it returns its list.
    """
    from kiro_crew.dashboard import ws as dashboard_ws
    from kiro_crew.dashboard.handlers import source_providers

    tabs = SimpleNamespace(
        sockets=[], registered=[], passes=0, during_pass=lambda: None, socket_type=_Socket
    )

    def _serialize(*, include_check_status: bool = False, dashboard_user: bool = False) -> list:
        tabs.passes += 1
        tabs.during_pass()
        return [{"key": f"chat-{tabs.passes}", "title": "listed"}]

    def _unregister(ws: _Socket) -> None:
        if ws in tabs.registered:
            tabs.registered.remove(ws)

    state = MagicMock()
    state.owner_id = "U_OWNER"
    state._yolo = False
    state._folders = []
    state.folders_generation.return_value = 1
    state.tags_generation.return_value = 1
    state.serialize_slots.side_effect = _serialize
    state.register_ws.side_effect = lambda ws, owner=False: tabs.registered.append(ws)
    state.unregister_ws.side_effect = _unregister

    class _Request(dict):
        def __init__(self) -> None:
            # A dashboard user who is not the owner, so no owner-only driver starts.
            super().__init__({"user": "U_TAB", "app": "", "is_dashboard_user": True})
            self.app = {"state": state}

    def _open(**_kwargs) -> _Socket:
        tabs.sockets.append(tabs.socket_type())
        return tabs.sockets[-1]

    async def _no_allowlist() -> None:
        return None

    _floor_monkeypatch.setattr(dashboard_ws, "_check_ws_origin", lambda request: None)
    _floor_monkeypatch.setattr(dashboard_ws, "LagTolerantWebSocketResponse", _open)
    _floor_monkeypatch.setattr(source_providers, "ensure_gitlab_hosts_loaded", _no_allowlist)
    tabs.connect = lambda: asyncio.ensure_future(dashboard_ws.api_ws(_Request()))
    return tabs


class TestSlotListGets:
    @pytest.mark.asyncio
    async def test_eight_concurrent_gets_take_two_passes(
        self, state, passes, owner_get, wide_window
    ) -> None:
        from kiro_crew.dashboard.chat_handlers import api_chat_slots

        responses = await await_until(
            asyncio.gather(*(api_chat_slots(_slots_get(state)) for _ in range(8)))
        )

        # The first takes its list at once; the other seven share the trailing pass.
        assert passes == [{"include_check_status": True, "dashboard_user": True}] * 2
        assert [_keys(r) for r in responses] == [["chat-1"]] + [["chat-2"]] * 7

    @pytest.mark.asyncio
    async def test_a_get_after_a_quiet_window_takes_its_own_pass_at_once(
        self, state, passes, owner_get, _floor_monkeypatch
    ) -> None:
        from kiro_crew.dashboard.chat_handlers import api_chat_slots

        _floor_monkeypatch.setattr(slot_list_pass, "PASS_WINDOW_MIN_S", 0.0)
        _floor_monkeypatch.setattr(slot_list_pass, "PASS_WINDOW_PASSES", 0)
        first = await await_until(asyncio.ensure_future(api_chat_slots(_slots_get(state))))
        second = await await_until(asyncio.ensure_future(api_chat_slots(_slots_get(state))))

        assert len(passes) == 2
        assert (_keys(first), _keys(second)) == (["chat-1"], ["chat-2"])


class TestConnects:
    @pytest.mark.asyncio
    async def test_eight_concurrent_connects_take_two_passes(self, tabs) -> None:
        await await_until(asyncio.gather(*(tabs.connect() for _ in range(8))))

        # The first takes its list at once; the other seven share the trailing pass.
        # Each socket's first frame is the snapshot of the list it was given.
        assert tabs.passes == 2
        assert sorted(_first_frame(ws) for ws in tabs.sockets) == (
            [("slots", ["chat-1"])] + [("slots", ["chat-2"])] * 7
        )

    @pytest.mark.asyncio
    async def test_a_waiting_connect_is_registered_and_gets_its_snapshot_first(
        self, tabs, state
    ) -> None:
        """A connect inside the window waits for the trailing pass, up to
        ``PASS_WINDOW_MAX_S``, after its handshake was answered, and the tab sends its
        catch-up reads at once. Its socket is registered for the whole wait, so a frame
        broadcast after one of those reads was answered still reaches it, and the hub
        holds that frame until the snapshot is out."""
        await await_until(tabs.connect(), what="the first connect, which opens the window")
        registered_in_pass: list[bool] = []

        def _broadcast() -> None:
            # The trailing pass is taken at the last instant of the second connect's
            # wait; a notification created then goes through the hub's send.
            registered_in_pass.append(tabs.sockets[1] in tabs.registered)
            for ws in list(tabs.registered):
                state._spawn_ws_send(ws, json.dumps({"type": "notification", "data": {}}))

        tabs.during_pass = _broadcast
        await await_until(tabs.connect(), what="the second connect, inside the window")

        assert tabs.passes == 2
        assert registered_in_pass == [True]
        assert _first_frame(tabs.sockets[1]) == ("slots", ["chat-2"])
        assert tabs.sockets[1].sent[1] == {"type": "notification", "data": {}}

    @pytest.mark.asyncio
    async def test_frames_held_for_a_connect_go_out_before_its_snapshot_drains(
        self, tabs, state
    ) -> None:
        """A tab that stops reading its snapshot still gets the held frames into its
        buffer, where the hub's stall check sees them."""
        drained = asyncio.Event()

        class _DrainingSocket(_Socket):
            async def send_str(self, payload: str) -> None:
                # Written at once, then waiting for the tab to read, as aiohttp does.
                await super().send_str(payload)
                if self.sent[-1]["type"] == "slots":
                    await drained.wait()

        await await_until(tabs.connect(), what="the first connect, which opens the window")

        def _broadcast() -> None:
            for ws in list(tabs.registered):
                state._spawn_ws_send(ws, json.dumps({"type": "notification", "data": {}}))

        tabs.during_pass = _broadcast
        tabs.socket_type = _DrainingSocket
        connect = tabs.connect()
        await async_wait_until(
            lambda: len(tabs.sockets) == 2 and len(tabs.sockets[1].sent) == 2,
            describe=lambda: [ws.sent for ws in tabs.sockets],
        )

        assert [frame["type"] for frame in tabs.sockets[1].sent] == ["slots", "notification"]
        drained.set()
        await await_until(connect, what="the second connect, once its snapshot drained")

    @pytest.mark.asyncio
    async def test_a_connect_whose_snapshot_failed_still_gets_the_frames_held_for_it(
        self, tabs, state
    ) -> None:
        await await_until(tabs.connect(), what="the first connect, which opens the window")

        def _broadcast_then_fail() -> None:
            for ws in list(tabs.registered):
                state._spawn_ws_send(ws, json.dumps({"type": "notification", "data": {}}))
            raise TypeError("unserializable slot state")

        tabs.during_pass = _broadcast_then_fail
        await await_until(tabs.connect(), what="the second connect, whose pass fails")
        await async_wait_until(lambda: tabs.sockets[1].sent, describe=lambda: tabs.sockets[1].sent)

        assert tabs.sockets[1].sent[0] == {"type": "notification", "data": {}}


_NO_HOLDS = {"slot_list_held_keys": lambda: [], "slot_list_hold_channels": lambda: False}


class _Unhashable:
    """A state double that takes a weak reference but has no hash."""

    __hash__ = None  # type: ignore[assignment]

    def __init__(self, serialize_slots) -> None:
        self.serialize_slots = serialize_slots
        self.slot_list_held_keys = _NO_HOLDS["slot_list_held_keys"]
        self.slot_list_hold_channels = _NO_HOLDS["slot_list_hold_channels"]


class TestSharedSlotList:
    @pytest.mark.asyncio
    async def test_a_caller_inside_the_window_gets_a_list_taken_after_it_asked(
        self, state, passes, wide_window
    ) -> None:
        first = await shared_slot_list(state, dashboard_user=True)
        # Inside the window: no list is reused, the trailing pass serializes again.
        second = await await_until(
            asyncio.ensure_future(shared_slot_list(state, dashboard_user=True))
        )

        assert (first, second) == (
            [{"key": "chat-1", "title": "listed"}],
            [{"key": "chat-2", "title": "listed"}],
        )

    def test_the_window_spans_a_few_passes_within_its_floor_and_ceiling(self) -> None:
        assert slot_list_pass.window_after(0.0) == slot_list_pass.PASS_WINDOW_MIN_S
        assert slot_list_pass.window_after(0.3) == pytest.approx(0.9)
        assert slot_list_pass.window_after(5.0) == slot_list_pass.PASS_WINDOW_MAX_S

    def test_a_trailing_pass_waits_out_its_callers_up_to_the_ceiling(self) -> None:
        # Callers a handshake apart keep moving the pass on...
        assert slot_list_pass.trailing_due(10.0, 10.5, 0.3) == pytest.approx(10.8)
        # ...but never past the ceiling after the first of them.
        assert slot_list_pass.trailing_due(10.0, 11.9, 0.3) == pytest.approx(
            10.0 + slot_list_pass.PASS_WINDOW_MAX_S
        )

    @pytest.mark.asyncio
    async def test_a_caller_joining_while_the_trailing_pass_waits_shares_it(
        self, state, passes, wide_window
    ) -> None:
        await shared_slot_list(state, dashboard_user=True)
        early = asyncio.ensure_future(shared_slot_list(state, dashboard_user=True))
        await asyncio.sleep(0)
        late = asyncio.ensure_future(shared_slot_list(state, dashboard_user=True))

        assert await await_until(early) is await await_until(late)
        assert len(passes) == 2

    @pytest.mark.asyncio
    async def test_each_audience_keeps_its_own_window(self, state, passes, wide_window) -> None:
        await shared_slot_list(state, include_check_status=True, dashboard_user=True)
        # Another audience has had no pass yet: it takes one at once.
        user = await shared_slot_list(state, dashboard_user=True)
        owner_again, owner_too = await await_until(
            asyncio.gather(
                shared_slot_list(state, include_check_status=True, dashboard_user=True),
                shared_slot_list(state, include_check_status=True, dashboard_user=True),
            )
        )

        assert passes == [
            {"include_check_status": True, "dashboard_user": True},
            {"include_check_status": False, "dashboard_user": True},
            {"include_check_status": True, "dashboard_user": True},
        ]
        assert user == [{"key": "chat-2", "title": "listed"}]
        assert owner_again is owner_too

    @pytest.mark.asyncio
    async def test_a_caller_cancelled_while_it_waits_is_dropped(
        self, state, passes, wide_window
    ) -> None:
        await shared_slot_list(state, dashboard_user=True)
        kept = asyncio.ensure_future(shared_slot_list(state, dashboard_user=True))
        dropped = asyncio.ensure_future(shared_slot_list(state, dashboard_user=True))
        # Both join the trailing pass, then one goes away before it runs.
        await asyncio.sleep(0)
        dropped.cancel()

        rows = await await_until(kept)

        assert rows == [{"key": "chat-2", "title": "listed"}]
        assert len(passes) == 2
        assert dropped.cancelled()

    @pytest.mark.asyncio
    async def test_a_failed_pass_raises_for_every_caller(
        self, state, _floor_monkeypatch, wide_window
    ) -> None:
        def _broken(**_kwargs) -> list:
            raise TypeError("unserializable slot state")

        _floor_monkeypatch.setattr(state, "serialize_slots", _broken)

        results = await await_until(
            asyncio.gather(
                shared_slot_list(state),
                shared_slot_list(state),
                shared_slot_list(state),
                return_exceptions=True,
            )
        )

        assert all(isinstance(r, TypeError) for r in results)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "stub_of",
        [lambda serialize: SimpleNamespace(serialize_slots=serialize, **_NO_HOLDS), _Unhashable],
        ids=["no-weakref", "no-hash"],
    )
    async def test_a_state_the_table_cannot_key_serializes_for_every_caller(
        self, stub_of, wide_window
    ) -> None:
        calls: list[dict] = []

        def _serialize(**kwargs) -> list:
            calls.append(kwargs)
            return [{"key": f"chat-{len(calls)}"}]

        stub = stub_of(_serialize)

        # Inside the window: a state the table can key would make the second wait.
        first = await shared_slot_list(stub, dashboard_user=True)
        second = await shared_slot_list(stub, dashboard_user=True)

        assert (first, second) == ([{"key": "chat-1"}], [{"key": "chat-2"}])
        assert calls == [{"include_check_status": False, "dashboard_user": True}] * 2
