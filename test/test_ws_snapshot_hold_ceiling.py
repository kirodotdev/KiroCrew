"""Frames held for a socket that waits for its connect snapshot stay under the hub's ceiling.

``ws_snapshot_hold`` keeps every hub frame for a socket whose connect snapshot is not
out yet. Nothing reaches that socket's transport meanwhile, so ``_drop_if_stalled``
reads an empty buffer and admits every frame. These pin that the hold does not lift
the memory ceiling one tab may take (``WS_HARD_MAX_BUFFERED_BYTES``, and
``WS_HELD_MAX_FRAMES`` held frames): not while the frames are held, not when the
release writes them behind the snapshot, and an awaited owner send refused for it,
or discarded at the release, reports no delivery.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from kiro_crew.dashboard import websocket_hub
from kiro_crew.dashboard.state import DashboardState
from kiro_crew.dashboard.websocket_hub import (
    WS_HARD_MAX_BUFFERED_BYTES,
    WS_HELD_MAX_FRAMES,
    WS_OWNER_SEND_MAX_S,
)
from kiro_crew.dashboard.ws_snapshot_hold import (
    HELD_FRAMES_KEY,
    drop_superseded_slot_frames,
    hold_frames,
    release_held,
)
from kiro_crew.testing.wait import async_wait_until, await_until

#: One large frame, as an app event or a transcript frame can be.
_FRAME_BYTES = 512 * 1024


@pytest.fixture
def state(_floor_monkeypatch, tmp_path) -> DashboardState:
    _floor_monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    return DashboardState(
        sessions=MagicMock(count=0),
        crons=MagicMock(),
        lessons=MagicMock(),
        start_time=0.0,
    )


class _Transport:
    """A tab's write side that never drains: every written byte stays buffered until
    the hub aborts the connection, which discards them and ends every pending drain."""

    def __init__(self) -> None:
        self.level = 0
        self.peak = 0
        self.aborted = False
        self.lost: asyncio.Future[None] = asyncio.get_running_loop().create_future()

    def get_write_buffer_size(self) -> int:
        return self.level

    def write(self, size: int) -> None:
        if self.aborted:
            raise ConnectionResetError("Cannot write to closing transport")
        self.level += size
        self.peak = max(self.peak, self.level)

    def abort(self) -> None:
        self.aborted = True
        self.level = 0
        if not self.lost.done():
            self.lost.set_exception(ConnectionResetError("connection lost"))
            # Retrieved here so a socket no send waits on logs no unretrieved error.
            self.lost.exception()


class _NonReadingTab:
    """A dashboard tab that stays connected and reads nothing.

    Each send writes its whole frame into the transport and then waits for a drain
    that never comes, as aiohttp's ``send_str`` does for a peer that stopped reading.
    """

    closed = False

    def __init__(self) -> None:
        self.transport = _Transport()
        self._req = SimpleNamespace(transport=self.transport)
        self._flags: dict = {"_is_dashboard_user": True}
        self.written = 0
        self.binary_written = 0

    def __setitem__(self, key: str, value) -> None:
        self._flags[key] = value

    def get(self, key: str, default=None):
        return self._flags.get(key, default)

    def pop(self, key: str, default=None):
        return self._flags.pop(key, default)

    async def send_str(self, payload: str) -> None:
        await self._write(len(payload.encode()))

    async def send_bytes(self, payload: bytes) -> None:
        await self._write(len(payload), binary=True)

    async def _write(self, size: int, *, binary: bool = False) -> None:
        self.transport.write(size)
        self.written += 1
        if binary:
            self.binary_written += 1
        await asyncio.shield(self.transport.lost)


def _held_bytes(tab: _NonReadingTab) -> int:
    frames = tab.get(HELD_FRAMES_KEY) or ()
    return sum(len(msg.encode()) if isinstance(msg, str) else len(msg) for msg, _ in frames)


def _publish(state: DashboardState, count: int) -> None:
    for index in range(count):
        state.broadcast_ws("notification", {"n": index, "blob": "x" * _FRAME_BYTES})


def _slot_list(size: int) -> str:
    """A ``slots`` frame of about *size* bytes, as a connect snapshot or a held list is."""
    return json.dumps({"type": "slots", "data": [{"blob": "s" * size}]})


def _dropped(state: DashboardState, tab: _NonReadingTab) -> bool:
    return tab.transport.aborted and tab not in state._ws_clients


async def _end(state: DashboardState, tab: _NonReadingTab, *pending: asyncio.Future) -> None:
    """Unregister *tab* and abort its transport, so no send of it outlives the test."""
    state.unregister_ws(tab)
    tab.transport.abort()
    await await_until(asyncio.gather(*pending, return_exceptions=True), what="the ended sends")


class TestHeldFramesKeepTheCeiling:
    @pytest.mark.asyncio
    async def test_frames_held_past_the_ceiling_drop_the_socket(self, state) -> None:
        """Forty 512 KiB frames published while a socket waits for its shared slot pass
        are 20 MiB, past the 16 MiB a tab may hold: the socket is dropped and what it
        holds stays under the ceiling, as it would had the frames reached its buffer."""
        tab = _NonReadingTab()
        hold_frames(tab)
        state.register_ws(tab, owner=True)

        _publish(state, 40)
        held_bytes, dropped = _held_bytes(tab), _dropped(state, tab)
        await _end(state, tab)

        assert (
            held_bytes <= WS_HARD_MAX_BUFFERED_BYTES
        ), f"{held_bytes} bytes held for one socket (ceiling {WS_HARD_MAX_BUFFERED_BYTES})"
        assert dropped, "a socket holding past the ceiling stays registered"

    @pytest.mark.asyncio
    async def test_frames_held_past_their_count_drop_the_socket(self, state) -> None:
        """Tiny frames stay far under the byte ceiling, but each held frame keeps an entry
        of its own: the frame past ``WS_HELD_MAX_FRAMES`` drops the socket."""
        tab = _NonReadingTab()
        hold_frames(tab)
        state.register_ws(tab, owner=True)

        for _ in range(WS_HELD_MAX_FRAMES):
            state._spawn_ws_send(tab, "{}")
        at_the_count = (len(tab.get(HELD_FRAMES_KEY) or ()), _dropped(state, tab))
        state._spawn_ws_send(tab, "{}")
        dropped = _dropped(state, tab)
        await _end(state, tab)

        assert at_the_count == (WS_HELD_MAX_FRAMES, False)
        assert dropped, "a socket holding past the frame count stays registered"

    @pytest.mark.asyncio
    async def test_superseded_slot_frames_leave_the_held_total(self, state) -> None:
        """A held slot list the snapshot supersedes is discarded, and its bytes leave the
        total with it: frames held after that are measured against what is still held."""
        tab = _NonReadingTab()
        hold_frames(tab)
        state.register_ws(tab, owner=True)
        state._spawn_ws_send(tab, _slot_list(12 * 1024 * 1024))
        assert drop_superseded_slot_frames(tab) is True

        # Twelve MiB more, which fits only once the twelve MiB list is gone.
        _publish(state, 24)
        held_count, dropped = len(tab.get(HELD_FRAMES_KEY) or ()), _dropped(state, tab)
        await _end(state, tab)

        assert not dropped, "the superseded list still counts against the ceiling"
        assert held_count == 24

    @pytest.mark.asyncio
    @pytest.mark.parametrize("level_moves", [True, False], ids=["selector", "proactor"])
    async def test_the_release_stops_at_the_ceiling(self, state, monkeypatch, level_moves) -> None:
        """Twelve MiB held behind an 8 MiB snapshot: the release takes the buffer past the
        ceiling, so the socket is dropped and the frames after the overflow are not written.
        Where the level shows no reading (the proactor loop Windows runs) the stall window
        does not apply, and the ceiling still does."""
        monkeypatch.setattr(websocket_hub, "level_is_progress_signal", lambda: level_moves)
        tab = _NonReadingTab()
        hold_frames(tab)
        state.register_ws(tab, owner=True)
        _publish(state, 24)
        assert not _dropped(state, tab), "12 MiB held is under the ceiling"
        held_count = len(tab.get(HELD_FRAMES_KEY) or ())

        # Queued the way ``_serve_ws`` queues it: the snapshot's send, then the release.
        snapshot_send = asyncio.ensure_future(tab.send_str(_slot_list(8 * 1024 * 1024)))
        released = release_held(tab)
        # Every send writes before it waits on a drain, unless the hub dropped the socket.
        await async_wait_until(
            lambda: tab.written == 1 + held_count or _dropped(state, tab),
            describe=lambda: (tab.written, tab.transport.level),
        )
        dropped, written = _dropped(state, tab), tab.written
        await _end(state, tab, snapshot_send, *released)

        assert tab.transport.peak <= WS_HARD_MAX_BUFFERED_BYTES + _FRAME_BYTES + 64, (
            f"the release buffered {tab.transport.peak} bytes "
            f"(ceiling {WS_HARD_MAX_BUFFERED_BYTES})"
        )
        assert dropped, "a release past the ceiling leaves the socket registered"
        assert written < 1 + held_count, "every held frame was written past the ceiling"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("binary", [False, True], ids=["text", "binary"])
    async def test_an_owner_send_refused_for_the_ceiling_is_not_delivered(
        self, state, binary
    ) -> None:
        """An awaited owner send that would take a held socket past the ceiling ends at once
        as not delivered, rather than waiting for a release that never comes."""
        tab = _NonReadingTab()
        hold_frames(tab)
        state.register_ws(tab, owner=True)
        _publish(state, 31)
        assert not _dropped(state, tab), "15.5 MiB held is under the ceiling"

        owner_send = asyncio.ensure_future(
            state.deliver_ws_owners_bytes(b"\0" * (2 * _FRAME_BYTES))
            if binary
            else state.deliver_ws_owners("notification", {"blob": "x" * (2 * _FRAME_BYTES)})
        )
        try:
            delivered = await await_until(
                owner_send,
                timeout=WS_OWNER_SEND_MAX_S / 6,  # flake-ok: the bound is the assertion
                what="the owner send refused for the ceiling",
            )
            dropped = _dropped(state, tab)
        finally:
            await _end(state, tab, owner_send)

        assert delivered == 0
        assert dropped

    @pytest.mark.asyncio
    async def test_an_owner_send_the_release_discards_is_not_delivered(self, state) -> None:
        """An owner frame held behind twelve MiB of others is never written when the
        release passes the ceiling before it: its awaited send counts no delivery."""
        tab = _NonReadingTab()
        hold_frames(tab)
        state.register_ws(tab, owner=True)
        _publish(state, 24)
        owner_send = asyncio.ensure_future(state.deliver_ws_owners_bytes(b"live frame"))
        await async_wait_until(
            lambda: len(tab.get(HELD_FRAMES_KEY) or ()) == 25,
            describe=lambda: len(tab.get(HELD_FRAMES_KEY) or ()),
        )

        snapshot_send = asyncio.ensure_future(tab.send_str(_slot_list(8 * 1024 * 1024)))
        released = release_held(tab)
        try:
            delivered = await await_until(owner_send, what="the owner send behind the overflow")
            dropped = _dropped(state, tab)
        finally:
            await _end(state, tab, snapshot_send, *released, owner_send)

        assert delivered == 0
        assert dropped
        assert tab.binary_written == 0, "the owner frame was written past the ceiling"
