"""Frames the hub sends a socket that waits for its connect snapshot follow it.

``_serve_ws`` registers a socket before it takes the slot list of its connect
snapshot, which can wait for a shared pass (``slot_list_pass``). ``ws_snapshot_hold``
keeps every frame the hub sends the socket meanwhile and sends them, in order, once
the snapshot is out.
"""

from __future__ import annotations

import asyncio
import json
import selectors
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.websocket_hub import (
    _OWNER_SEND_POLL_S,
    WS_MAX_BUFFERED_BYTES,
    WS_OWNER_SEND_MAX_S,
    WS_OWNER_SEND_TIMEOUT_S,
)
from kiro_crew.dashboard.ws_snapshot_hold import HELD_FRAMES_KEY, held, hold_frames, release_held
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


class _Socket:
    """A dashboard socket double that keeps every frame sent to it, text or binary."""

    closed = False

    def __init__(self) -> None:
        self.sent: list[str | bytes] = []
        self._flags: dict = {}

    def __setitem__(self, key: str, value) -> None:
        self._flags[key] = value

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)

    async def send_bytes(self, payload: bytes) -> None:
        self.sent.append(payload)


class _Transport:
    """An owner tab's write side: a buffer level under the drop limit, flat or falling
    a byte a read, and when the hub aborted the connection."""

    def __init__(self, *, falling: bool) -> None:
        self._level = WS_MAX_BUFFERED_BYTES
        self._falling = falling
        self.aborted_at: float | None = None

    def get_write_buffer_size(self) -> int:
        if self._falling:
            self._level -= 1
        return self._level

    def abort(self) -> None:
        self.aborted_at = asyncio.get_running_loop().time()


class _TabOnALink(_Socket):
    """An owner tab on ``transport`` whose sends return at once, or, unless ``drains``,
    wait for a drain that never comes."""

    def __init__(self, transport: _Transport, *, drains: bool) -> None:
        super().__init__()
        self._req = SimpleNamespace(transport=transport)
        self._drains = drains

    def pop(self, key: str, default=None):
        return self._flags.pop(key, default)

    async def send_str(self, payload: str) -> None:
        self.sent.append(payload)
        if not self._drains:
            await asyncio.get_running_loop().create_future()


#: Virtual seconds past which a test on ``_VirtualTimeLoop`` fails instead of running on.
_VIRTUAL_CEILING_S = 10 * WS_OWNER_SEND_MAX_S


class _JumpingSelector(selectors.DefaultSelector):
    """Where the loop would block until its next timer, move its clock there instead."""

    def __init__(self, loop: _VirtualTimeLoop) -> None:
        super().__init__()
        self._loop = loop

    def select(self, timeout=None):
        if timeout is None:
            raise AssertionError("the loop waits with nothing scheduled that the test drives")
        self._loop.now += timeout
        if self._loop.now > _VIRTUAL_CEILING_S:
            raise AssertionError(f"the test ran past {_VIRTUAL_CEILING_S:g} virtual seconds")
        return super().select(0)


class _VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock moves only to its next timer, so the owner send's
    bounds run at their real values and take no real time."""

    def __init__(self) -> None:
        self.now = 0.0
        super().__init__(_JumpingSelector(self))

    def time(self) -> float:
        return self.now


def _deliver_while_held(state, tab: _TabOnALink, release_at: float | None) -> int:
    """Deliver one owner frame to *tab* while it waits for its connect snapshot, whose
    held frames go out ``release_at`` virtual seconds in (never, for None)."""

    async def main(loop: _VirtualTimeLoop) -> int:
        hold_frames(tab)
        state.register_ws(tab, owner=True)
        released: list[asyncio.Future[None]] = []
        if release_at is not None:
            loop.call_later(release_at, lambda: released.extend(release_held(tab)))
        delivered = await state.deliver_ws_owners("followup_card", {"slot": "chat-1"})
        # The release's own sends end too, so none outlives the loop.
        await asyncio.gather(*released, return_exceptions=True)
        return delivered

    loop = _VirtualTimeLoop()
    try:
        return loop.run_until_complete(main(loop))
    finally:
        loop.close()


class TestSnapshotHold:
    @pytest.mark.asyncio
    async def test_hub_frames_wait_for_the_snapshot_and_then_go_straight_out(self, state) -> None:
        ws = _Socket()
        hold_frames(ws)
        state.register_ws(ws, owner=True)

        def held_count() -> int:
            return len(ws.get(HELD_FRAMES_KEY) or ())

        state._spawn_ws_send(ws, "broadcast")
        text = asyncio.ensure_future(state.deliver_ws_owners("followup_card", {"slot": "chat-1"}))
        await async_wait_until(lambda: held_count() == 2, describe=held_count)
        binary = asyncio.ensure_future(state.deliver_ws_owners_bytes(b"live frame"))
        await async_wait_until(lambda: held_count() == 3, describe=held_count)

        # Nothing reached the socket ahead of its snapshot, and no owner send counts yet.
        assert (ws.sent, text.done(), binary.done()) == ([], False, False)

        snapshot = asyncio.ensure_future(ws.send_str("snapshot"))
        released = release_held(ws)
        before = set(state._background_tasks)
        state._spawn_ws_send(ws, "later")
        later = state._background_tasks - before
        delivered = await await_until(asyncio.gather(text, binary), what="the owner sends")
        await await_until(asyncio.gather(snapshot, *released, *later), what="the sends")

        # Each owner send counts once its frame went out, right behind the snapshot.
        assert delivered == [1, 1]
        assert ws.sent == [
            "snapshot",
            "broadcast",
            json.dumps({"type": "followup_card", "data": {"slot": "chat-1"}}),
            b"live frame",
            "later",
        ]

    @pytest.mark.asyncio
    async def test_unregistering_a_socket_ends_the_owner_sends_held_for_it(self, state) -> None:
        """A connect cancelled while it waits for the shared slot-list pass unregisters its
        socket without releasing the frames held for it. An owner send awaiting one of them
        must end then, not sit until ``WS_OWNER_SEND_MAX_S``."""
        ws = _Socket()
        hold_frames(ws)
        state.register_ws(ws, owner=True)
        text = asyncio.ensure_future(state.deliver_ws_owners("followup_card", {"slot": "chat-1"}))
        await async_wait_until(lambda: len(ws.get(HELD_FRAMES_KEY) or ()) == 1)

        state.unregister_ws(ws)

        await await_until(
            text,
            timeout=WS_OWNER_SEND_MAX_S / 6,
            what="the owner send held for the unregistered socket",
        )
        assert ws.sent == []

    @pytest.mark.asyncio
    async def test_held_frames_go_out_while_the_snapshot_still_drains(self, state) -> None:
        """The hold ends once the snapshot is queued, not once the tab has read it, so a
        tab that stops reading still gets every frame into the buffer the hub's stall
        check reads."""
        drained = asyncio.Event()

        class _DrainingSocket(_Socket):
            async def send_str(self, payload: str) -> None:
                # Written at once, then waiting for the tab to read, as aiohttp does.
                self.sent.append(payload)
                if payload == "snapshot":
                    await drained.wait()

        ws = _DrainingSocket()
        hold_frames(ws)
        state._spawn_ws_send(ws, "broadcast")

        snapshot = asyncio.ensure_future(ws.send_str("snapshot"))
        released = release_held(ws)
        await await_until(asyncio.gather(*released), what="the held send")

        assert (ws.sent, snapshot.done()) == (["snapshot", "broadcast"], False)
        drained.set()
        await await_until(snapshot, what="the snapshot send")

    @pytest.mark.asyncio
    async def test_a_tab_that_drops_before_its_snapshot_is_not_a_delivered_followup(
        self, state
    ) -> None:
        """An owner send held behind a connect snapshot counts only once it is sent.

        The only owner tab is still waiting for its snapshot when a follow-up card is
        delivered, and drops before the held frame goes out. The card never reached
        it, so ``_suggest_followup`` must take its zero-delivery fallback and tell the
        model to restate the follow-ups, not report the card as shown.
        """
        from kiro_crew.dashboard.session_directive_apply_slot import _suggest_followup

        class _GoneSocket(_Socket):
            async def send_str(self, payload: str) -> None:
                raise ConnectionResetError

        ws = _GoneSocket()
        hold_frames(ws)
        state.register_ws(ws, owner=True)
        slot = MagicMock(key="chat-1", project="/repo")

        reply = asyncio.ensure_future(
            _suggest_followup(state, slot, {"items": [{"title": "Next", "prompt": "do it"}]})
        )
        await async_wait_until(lambda: ws.get(HELD_FRAMES_KEY) or reply.done())
        # The tab drops: the connect handler's snapshot send fails and it releases
        # the frames it held, whose sends fail the same way.
        ws.closed = True
        release_held(ws)
        text = await await_until(reply, what="the follow-up delivery")

        assert text.startswith("Follow-up card prepared, but no dashboard client is attached")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("binary", [False, True], ids=["text", "binary"])
    async def test_a_held_owner_send_that_fails_at_release_is_not_delivered(
        self, state, binary
    ) -> None:
        class _GoneSocket(_Socket):
            async def send_str(self, payload: str) -> None:
                raise ConnectionResetError

            async def send_bytes(self, payload: bytes) -> None:
                raise ConnectionResetError

        ws = _GoneSocket()
        hold_frames(ws)
        state.register_ws(ws, owner=True)
        delivery = asyncio.ensure_future(
            state.deliver_ws_owners_bytes(b"live frame")
            if binary
            else state.deliver_ws_owners("followup_card", {"slot": "chat-1"})
        )
        await async_wait_until(lambda: ws.get(HELD_FRAMES_KEY), describe=lambda: delivery)
        release_held(ws)

        assert await await_until(delivery, what="the owner delivery") == 0
        assert ws not in state._owner_ws_clients

    def test_a_held_owner_send_outlasts_the_stall_window_before_its_release(self, state) -> None:
        """Nothing reaches a held tab's buffer, so its level stays flat while the slot
        list pass runs, however long that takes. The stall window must not read that as
        a tab that stopped reading."""
        transport = _Transport(falling=False)
        tab = _TabOnALink(transport, drains=True)

        delivered = _deliver_while_held(state, tab, release_at=2 * WS_OWNER_SEND_TIMEOUT_S)

        assert delivered == 1, f"dropped {transport.aborted_at} s in, before its release"
        assert tab.sent == [json.dumps({"type": "followup_card", "data": {"slot": "chat-1"}})]
        assert transport.aborted_at is None
        assert tab in state._owner_ws_clients

    def test_the_stall_window_of_a_held_owner_send_starts_at_its_release(self, state) -> None:
        transport = _Transport(falling=False)
        tab = _TabOnALink(transport, drains=False)
        release_at = 2 * WS_OWNER_SEND_TIMEOUT_S

        delivered = _deliver_while_held(state, tab, release_at=release_at)

        assert delivered == 0, "a tab whose buffer never shrank after its release kept the send"
        assert transport.aborted_at is not None, "the tab that stopped reading was not aborted"
        since = transport.aborted_at - release_at
        window = WS_OWNER_SEND_TIMEOUT_S
        assert (
            window <= since <= window + 2 * _OWNER_SEND_POLL_S
        ), f"dropped {since:g} s after its release; the window starts once the send does"
        assert tab not in state._owner_ws_clients

    @pytest.mark.parametrize("released", [False, True], ids=["never-released", "released-late"])
    def test_the_cap_of_a_held_owner_send_counts_its_held_time(self, state, released) -> None:
        """``WS_OWNER_SEND_MAX_S`` bounds the whole wait: a hold that never ends, or a
        late release onto a tab that reads too slowly to finish, cannot hold the caller
        past it."""
        transport = _Transport(falling=True)
        tab = _TabOnALink(transport, drains=False)
        release_at = 2 * WS_OWNER_SEND_MAX_S / 3 if released else None

        delivered = _deliver_while_held(state, tab, release_at=release_at)

        assert delivered == 0
        cap = WS_OWNER_SEND_MAX_S
        assert transport.aborted_at is not None, "the send outlived its cap"
        assert (
            cap <= transport.aborted_at <= cap + _OWNER_SEND_POLL_S
        ), f"dropped {transport.aborted_at:g} s into the send, not at the {cap:g} s cap"
        assert tab not in state._owner_ws_clients

    @pytest.mark.asyncio
    async def test_a_failed_send_is_logged_and_the_hold_still_ends(self) -> None:
        class _GoneSocket(_Socket):
            async def send_str(self, payload: str) -> None:
                raise ConnectionResetError

        ws = _GoneSocket()
        hold_frames(ws)
        assert held(ws, "first") and held(ws, "second")

        released = release_held(ws)
        results = await await_until(
            asyncio.gather(*released, return_exceptions=True), what="the held sends"
        )

        assert [type(r) for r in results] == [ConnectionResetError, ConnectionResetError]
        assert held(ws, "after") is False
        assert ws.get(HELD_FRAMES_KEY) is None
