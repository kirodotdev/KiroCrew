"""A dashboard tab that stays connected but stops reading (D47, D48).

One normal tab and one stalled tab (a raw socket with a 4 KiB receive buffer that
completes the upgrade and never reads again) connect to the real ``/api/ws``
handler through aiohttp's test server, with the real token auth and the real
``WebSocketHub`` fan-out. The config dir, token secret and nonce store are pinned
under ``tmp_path`` as in ``test_chat_send_client_meta_survival``.

The stalled tab's frames stay in its transport's write buffer, and a send that has
to wait for that buffer to drain never completes. The hub must drop such a tab and
abort its connection, without touching a tab that reads.
"""

from __future__ import annotations

import asyncio
import base64
import collections
import json
import logging
import selectors
import socket
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient, TestServer
from chat_test_helpers import _make_app, _make_state

from kiro_crew.dashboard import revocation_gen, token_auth, websocket_hub
from kiro_crew.dashboard.handlers import updates
from kiro_crew.dashboard.state import _websocket_for
from kiro_crew.dashboard.websocket_hub import SLOT_PATCH_WS_FLAG
from kiro_crew.testing.clock import ManualClock
from kiro_crew.testing.wait import async_wait_until

FRAME = 64 * 1024


def _level_moves_on_this_loop() -> bool:
    """Whether the running loop's transports show a reader's progress in their
    write-buffer level. Not on the proactor loop Windows runs: there the level is
    the overlapped send in flight plus the bytes queued behind it, and it can read
    0 for a peer that stopped reading once the kernel holds every byte."""
    proactor = getattr(asyncio, "ProactorEventLoop", None)
    return proactor is None or not isinstance(asyncio.get_running_loop(), proactor)


@pytest.fixture
def ws_state(tmp_path, monkeypatch):
    monkeypatch.setattr("kiro_crew.config.loader.config_dir", lambda: tmp_path)
    monkeypatch.setattr("kiro_crew.dashboard.state.config_dir", lambda: tmp_path)
    monkeypatch.setattr(token_auth, "_get_secret", lambda: b"stalled-tab-test-signing-key")
    monkeypatch.setattr(token_auth, "_state", token_auth.TokenStateManager())
    monkeypatch.setattr(token_auth, "_app_perms_cache", {})
    monkeypatch.setattr(token_auth, "_revoked_store_singleton", None)
    monkeypatch.setattr(revocation_gen, "_gen", 0)
    stopped = asyncio.Event()
    monkeypatch.setattr(updates, "shutdown_event", stopped)
    monkeypatch.setattr("kiro_crew.dashboard.ws.shutdown_event", stopped)
    # A stalled tab is dropped at the first send that finds its buffer over the
    # limit and not shrunk since the send before (no window in these tests).
    monkeypatch.setattr(websocket_hub, "WS_STALL_SECONDS", 0.0, raising=False)
    return _make_state(tmp_path)


async def _stalled_socket(host: str, port: int, token: str, origin: str) -> socket.socket:
    """Open ``/api/ws`` on a raw socket that never reads after the upgrade."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    sock.setblocking(False)
    await loop.sock_connect(sock, (host, port))
    # RFC 6455 asks for a 16-byte nonce; any fixed one is valid for this socket.
    key = base64.b64encode(b"stalled-tab-key!").decode()
    request = (
        f"GET /api/ws?token={token} HTTP/1.1\r\nHost: {host}:{port}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n"
    )
    await loop.sock_sendall(sock, request.encode())
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = await asyncio.wait_for(loop.sock_recv(sock, 1024), timeout=30)
        assert chunk, "the server closed the upgrade"
        head += chunk
    assert head.startswith(b"HTTP/1.1 101"), head[:80]
    return sock


async def _harness(state, body):
    from kiro_crew.dashboard.ws import api_ws

    app = _make_app(state)
    app.middlewares.insert(0, token_auth.token_auth_middleware())
    app["allowed_origins"] = set()
    app.router.add_get("/api/ws", api_ws)
    async with TestClient(TestServer(app)) as client:
        origin = str(client.make_url("/")).rstrip("/")
        app["allowed_origins"].add(origin)
        client.session.headers["Origin"] = origin
        token = token_auth.generate_token("local-app")
        normal = await client.ws_connect("/api/ws", params={"token": token})
        received: dict[int, float] = {}

        async def _read() -> None:
            async for msg in normal:
                data = json.loads(msg.data)
                if data.get("type") == "stall_probe":
                    received[data["data"]["n"]] = time.monotonic()

        reader = asyncio.ensure_future(_read())
        sock = await _stalled_socket(client.host, client.port, token, origin)
        await async_wait_until(
            lambda: len(state._ws_clients) >= 2,
            describe=lambda: f"{len(state._ws_clients)} of 2 tabs registered",
        )
        local = sock.getsockname()
        stalled = next(
            ws
            for ws in state._ws_clients
            if ws._req.transport.get_extra_info("peername")[:2] == local[:2]
        )
        reading = next(ws for ws in state._ws_clients if ws is not stalled)
        try:
            await body(state, stalled, reading, received)
        finally:
            reader.cancel()
            await normal.close()
            sock.close()


async def _broadcast(state, start: int, count: int, stop=None) -> tuple[int, int]:
    """Broadcast up to ``count`` frames; return (next frame number, peak buffer)."""
    pad = "x" * FRAME
    peak = 0
    n = start
    for n in range(start, start + count):
        state.broadcast_ws("stall_probe", {"n": n, "pad": pad})
        await asyncio.sleep(0)
        if stop is not None:
            peak = max(peak, stop())
            if stop() < 0:
                return n + 1, peak
    return n + 1, peak


async def _wait_received(received: dict[int, float], n: int, ceiling: float = 30.0) -> None:
    """Wait until the reading tab has ``n`` frames; the ceiling only bounds a hang."""
    await async_wait_until(
        lambda: len(received) >= n,
        timeout=ceiling,
        describe=lambda: f"the reading tab got {len(received)} of {n} frames",
    )


async def _wait_until(done, what: str, ceiling: float = 30.0) -> None:
    """Poll ``done`` until it holds; the ceiling only bounds a hang."""
    await async_wait_until(done, timeout=ceiling, describe=lambda: f"{what} did not happen")


class _Transport:
    """The write side of a fake tab: unsent bytes, and whether it was aborted."""

    def __init__(self, buffered: int) -> None:
        self.buffered = buffered
        self.aborted = False

    def get_write_buffer_size(self) -> int:
        return self.buffered

    def abort(self) -> None:
        self.aborted = True
        self.buffered = 0


def _patch_capable_tab(buffered: int) -> MagicMock:
    store: dict[str, object] = {SLOT_PATCH_WS_FLAG: True}
    ws = MagicMock()
    ws.closed = False
    ws.send_str = AsyncMock()
    ws.get = MagicMock(side_effect=store.get)
    ws.pop = MagicMock(side_effect=store.pop)
    ws.__setitem__.side_effect = store.__setitem__
    ws._req.transport = _Transport(buffered)
    return ws


def _hub_on_a_manual_clock(state, monkeypatch, window: float = 5.0):
    """The hub, with its clock replaced by one the test advances, and a stall window."""
    clock = ManualClock()
    clock.install(monkeypatch, websocket_hub)
    monkeypatch.setattr(websocket_hub, "WS_STALL_SECONDS", window, raising=False)
    return _websocket_for(state), clock


class _Verdict:
    """Stands in for the helper the hub asks whether a tab's level shows it reading
    (``level_is_progress_signal``), and counts the calls, so a forced branch the hub
    never asks about fails instead of passing."""

    def __init__(self, level_moves: bool) -> None:
        self.level_moves = level_moves
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.level_moves


def _force_verdict(monkeypatch, level_moves: bool) -> _Verdict:
    """Make the hub's answer ``level_moves`` whichever loop runs the test.

    ``raising=False`` keeps the test runnable where the hub has no such helper, so
    the test's assertions, not the patch, report what the hub's rule does.
    """
    verdict = _Verdict(level_moves)
    monkeypatch.setattr(websocket_hub, "level_is_progress_signal", verdict, raising=False)
    return verdict


class _VirtualClock:
    """The time of a ``_VirtualTimeLoop``, in seconds from its start."""

    def __init__(self) -> None:
        self.now = 0.0


class _VirtualTimeSelector(selectors.DefaultSelector):
    """Where the loop would block until its next timer, move the clock to that timer
    and return at once."""

    def __init__(self, clock: _VirtualClock) -> None:
        super().__init__()
        self._clock = clock

    def select(self, timeout=None):
        if timeout is None:
            raise AssertionError("the loop waits with nothing scheduled that the test drives")
        self._clock.now += timeout
        return super().select(0)


class _VirtualTimeLoop(asyncio.SelectorEventLoop):
    """An event loop whose clock moves only to its next timer, so a wait of any length
    takes no real time and every timer fires in order of its deadline."""

    def __init__(self) -> None:
        self._virtual = _VirtualClock()
        super().__init__(_VirtualTimeSelector(self._virtual))

    def time(self) -> float:
        return self._virtual.now


def _run_in_virtual_time(main):
    loop = _VirtualTimeLoop()
    try:
        return loop.run_until_complete(main(loop))
    finally:
        loop.close()


class _SlowLink:
    """The write side of a fake owner tab on a link the test sets.

    From the moment a send writes its frame, the tab reads ``chunk`` bytes every
    ``every`` seconds of loop time, until nothing is left or, when ``reads`` is set,
    after that many reads. A send returns once the tab has read every byte written
    to it, as a send waiting on a full buffer does once that buffer drains.
    """

    def __init__(self, backlog: int, chunk: int, every: float, reads: int | None = None) -> None:
        self.buffered = backlog
        self.read_at: list[float] = []
        self.aborted_at: float | None = None
        self.unread_at_abort: int | None = None
        self._chunk = chunk
        self._every = every
        self._reads_left = reads
        self._drained: asyncio.Future[None] | None = None

    def get_write_buffer_size(self) -> int:
        return self.buffered

    def abort(self) -> None:
        self.aborted_at = asyncio.get_running_loop().time()
        self.unread_at_abort = self.buffered
        self.buffered = 0
        if self._drained is not None and not self._drained.done():
            self._drained.set_exception(ConnectionResetError("aborted"))

    async def send(self, msg: str) -> None:
        loop = asyncio.get_running_loop()
        self.buffered += len(msg)
        self._drained = loop.create_future()
        loop.call_later(self._every, self._read, loop)
        await self._drained

    def _read(self, loop: asyncio.AbstractEventLoop) -> None:
        if self.aborted_at is not None or self._reads_left == 0:
            return
        self.buffered = max(0, self.buffered - self._chunk)
        self.read_at.append(loop.time())
        if self._reads_left is not None:
            self._reads_left -= 1
        if self.buffered:
            loop.call_later(self._every, self._read, loop)
        elif self._drained is not None and not self._drained.done():
            self._drained.set_result(None)


def _owner_tab_on(link: _SlowLink) -> MagicMock:
    store: dict[str, object] = {}
    ws = MagicMock()
    ws.closed = False
    ws.send_str = AsyncMock(side_effect=link.send)
    ws.get = MagicMock(side_effect=store.get)
    ws.pop = MagicMock(side_effect=store.pop)
    ws.__setitem__.side_effect = store.__setitem__
    ws._req.transport = link
    return ws


def _deliver_in_virtual_time(state, link: _SlowLink, pad: int) -> tuple[MagicMock, int, float]:
    """Register one owner tab on ``link``, deliver a frame of ``pad`` bytes to it, and
    return the tab, the count ``deliver_ws_owners`` returned and the seconds it took."""
    tab = _owner_tab_on(link)

    async def main(loop):
        state.register_ws(tab, owner=True)
        start = loop.time()
        delivered = await state.deliver_ws_owners("stall_probe", {"pad": "x" * pad})
        return delivered, loop.time() - start

    delivered, took = _run_in_virtual_time(main)
    return tab, delivered, took


def _assert_dropped_at(peak: int, level_moves: bool) -> None:
    """Where the level shows reading, the window (0 s in these tests) drops a stalled
    tab just over the limit. Where it does not, only the ceiling may drop it."""
    if level_moves:
        assert peak <= websocket_hub.WS_MAX_BUFFERED_BYTES + 3 * FRAME, peak
        return
    assert peak > websocket_hub.WS_HARD_MAX_BUFFERED_BYTES, (
        f"the window dropped the tab at {peak} bytes buffered, where its level shows "
        "no reading and only the ceiling may drop it"
    )
    assert peak <= websocket_hub.WS_HARD_MAX_BUFFERED_BYTES + 3 * FRAME, peak


@pytest.mark.asyncio
async def test_a_tab_over_the_limit_whose_buffer_shrinks_is_kept(ws_state, monkeypatch):
    """Each send writes its whole frame before it waits on drain, so a burst can take a
    tab that still reads over the limit. Its buffer shrinking restarts the window."""
    hub, clock = _hub_on_a_manual_clock(ws_state, monkeypatch)
    tab = _patch_capable_tab(websocket_hub.WS_MAX_BUFFERED_BYTES + 8 * FRAME)
    ws_state.register_ws(tab, owner=True)
    for _ in range(4):
        hub._spawn_ws_send(tab, "{}")
        clock.advance(4.0)
        tab._req.transport.buffered -= FRAME  # it read a frame since that send
    hub._spawn_ws_send(tab, "{}")
    await asyncio.sleep(0)
    assert tab in ws_state._ws_clients, "a tab whose buffer kept shrinking was dropped as stalled"
    assert not tab._req.transport.aborted
    assert tab.send_str.call_count == 5


@pytest.mark.asyncio
async def test_a_tab_whose_buffer_stops_shrinking_is_dropped_once_the_window_passes(
    ws_state, monkeypatch
):
    verdict = _force_verdict(monkeypatch, True)
    hub, clock = _hub_on_a_manual_clock(ws_state, monkeypatch)
    tab = _patch_capable_tab(websocket_hub.WS_MAX_BUFFERED_BYTES + FRAME)
    ws_state.register_ws(tab, owner=True)
    hub._spawn_ws_send(tab, "{}")  # over the limit: its window starts
    clock.advance(4.0)
    tab._req.transport.buffered += FRAME  # it read nothing, so that frame stays
    hub._spawn_ws_send(tab, "{}")
    assert tab in ws_state._ws_clients, "dropped 4 s into its 5 s window"
    clock.advance(1.5)
    hub._spawn_ws_send(tab, "{}")
    await asyncio.sleep(0)
    assert tab not in ws_state._ws_clients, "kept after its buffer went 5.5 s without shrinking"
    assert tab._req.transport.aborted
    assert tab.send_str.call_count == 2
    assert verdict.calls, "the hub ran the window without asking whether the level shows reading"


@pytest.mark.asyncio
async def test_where_the_level_shows_no_reading_only_the_ceiling_drops_a_flat_tab(
    ws_state, monkeypatch
):
    """On the proactor loop a tab that reads shows a flat level for as long as one large
    send takes, so the window must not drop it there. The ceiling still bounds it."""
    verdict = _force_verdict(monkeypatch, False)
    hub, clock = _hub_on_a_manual_clock(ws_state, monkeypatch)
    tab = _patch_capable_tab(websocket_hub.WS_MAX_BUFFERED_BYTES + FRAME)
    ws_state.register_ws(tab, owner=True)
    for _ in range(3):
        hub._spawn_ws_send(tab, "{}")
        clock.advance(4.0)  # over the limit and flat, for 12 s in all
    hub._spawn_ws_send(tab, "{}")
    await asyncio.sleep(0)
    assert tab in ws_state._ws_clients, "the window dropped a tab whose level shows no reading"
    assert not tab._req.transport.aborted
    assert verdict.calls, "the hub never asked whether the level shows reading"
    tab._req.transport.buffered = websocket_hub.WS_HARD_MAX_BUFFERED_BYTES + 1
    hub._spawn_ws_send(tab, "{}")
    await asyncio.sleep(0)
    assert tab not in ws_state._ws_clients, "a tab past the ceiling was kept"
    assert tab._req.transport.aborted
    assert tab.send_str.call_count == 4


@pytest.mark.asyncio
async def test_where_the_level_shows_no_reading_the_owner_send_timeout_drops_a_tab(
    ws_state, monkeypatch
):
    """With the window off, an owner tab that stopped reading is dropped by the owner-send
    cap in ``deliver_ws_owners``, and the call returns."""
    verdict = _force_verdict(monkeypatch, False)
    hub, clock = _hub_on_a_manual_clock(ws_state, monkeypatch)
    monkeypatch.setattr(websocket_hub, "WS_OWNER_SEND_MAX_S", 0.05)
    tab = _patch_capable_tab(websocket_hub.WS_MAX_BUFFERED_BYTES + FRAME)
    ws_state.register_ws(tab, owner=True)
    hub._drop_if_stalled(tab)  # where the level shows reading, its window starts here
    clock.advance(10.0)  # flat for twice the window
    drained = asyncio.Event()

    async def _waits_for_a_drain(_msg: str) -> None:
        # Never set: the tab stopped reading. The bound is past the 30 s ceiling
        # below, so a hub with no send timeout fails there first.
        await asyncio.wait_for(drained.wait(), timeout=60)

    tab.send_str = AsyncMock(side_effect=_waits_for_a_drain)
    delivered = await asyncio.wait_for(ws_state.deliver_ws_owners("stall_probe", {}), timeout=30)
    sent = tab.send_str.await_count
    assert sent == 1, "the window dropped the owner tab before its send timed out"
    assert delivered == 0, "a timed-out tab counts as not delivered"
    assert tab not in ws_state._owner_ws_clients, "the owner-send cap did not drop the tab"
    assert tab._req.transport.aborted
    assert verdict.calls, "the hub never asked whether the level shows reading"


@pytest.mark.asyncio
async def test_a_tab_past_the_hard_ceiling_is_dropped_at_once(ws_state, monkeypatch):
    hub, _clock = _hub_on_a_manual_clock(ws_state, monkeypatch)
    ceiling = 4 * websocket_hub.WS_MAX_BUFFERED_BYTES
    at_ceiling = _patch_capable_tab(ceiling)
    past = _patch_capable_tab(ceiling + 1)
    ws_state.register_ws(at_ceiling, owner=True)
    ws_state.register_ws(past, owner=True)
    hub._spawn_ws_send(at_ceiling, "{}")
    hub._spawn_ws_send(past, "{}")
    await asyncio.sleep(0)
    # Control: over the limit but not past the ceiling, its window has only started.
    assert at_ceiling in ws_state._ws_clients, "a tab at the ceiling was dropped at once"
    assert past not in ws_state._ws_clients, "a tab past the ceiling was kept for its window"
    assert past._req.transport.aborted
    past.send_str.assert_not_called()
    assert websocket_hub.WS_HARD_MAX_BUFFERED_BYTES == ceiling


@pytest.mark.asyncio
@pytest.mark.parametrize("forced_flat", [False, True], ids=["this-loop", "level-flat"])
async def test_a_stalled_log_subscriber_is_dropped_by_the_log_stream(
    ws_state, monkeypatch, forced_flat
):
    """The log stream sends outside the hub's fan-out, so it applies the same limit."""
    verdict = _force_verdict(monkeypatch, False) if forced_flat else None

    async def body(state, stalled, reading, received):
        level_moves = not forced_flat and _level_moves_on_this_loop()
        # Past the ceiling takes more frames than past the limit.
        frames = 400 if level_moves else 1000
        transport = stalled._req.transport
        state._ws_log_subscribers.add(stalled)
        handler = updates._RingLogHandler(collections.deque(maxlen=8))
        handler.set_state(state)
        record = logging.LogRecord("stall", logging.INFO, __file__, 0, "x" * FRAME, None, None)
        peak = 0
        for _ in range(frames):
            if stalled not in state._ws_clients:
                break
            handler.emit(record)
            for _ in range(3):
                await asyncio.sleep(0)
            peak = max(peak, transport.get_write_buffer_size())
        assert stalled not in state._ws_clients, (
            f"a log subscriber that reads nothing is still registered after {frames} log frames "
            f"of {FRAME // 1024} KiB, with {transport.get_write_buffer_size()} bytes buffered for it"
        )
        assert stalled not in state._ws_log_subscribers
        _assert_dropped_at(peak, level_moves)
        assert transport.is_closing()
        assert transport.get_write_buffer_size() == 0
        assert reading in state._ws_clients
        asked = verdict is None or verdict.calls
        assert asked, "the hub never asked whether the level shows reading"

    await _harness(ws_state, body)


@pytest.mark.asyncio
async def test_a_stalled_tab_does_not_delay_a_reading_tab(ws_state):
    """Control: one fan-out task per send, so a stalled tab holds only its own sends."""

    async def body(state, stalled, reading, received):
        # How many frames fill the stalled tab's socket buffers depends on the
        # host, so send until writing to it is paused rather than a fixed count.
        n = 0
        try:
            async with asyncio.timeout(30):
                while not stalled._req.protocol._paused and n < 2000:
                    n, _ = await _broadcast(state, n, 1)
        except TimeoutError:
            pass
        assert stalled._req.protocol._paused, (
            f"precondition: writing to the stalled tab never paused after {n} frames of "
            f"{FRAME // 1024} KiB ({stalled._req.transport.get_write_buffer_size()} bytes buffered)"
        )
        await _wait_received(received, n)
        # The reading tab got every frame while writing to the stalled tab is
        # paused, so no frame waited on the stalled tab's buffer.
        assert stalled in state._ws_clients, "a tab under the limit must stay registered"
        # Under the selector loops the stalled tab's frames show in its level. The
        # proactor loop's level can read 0 by now, once the kernel holds them all.
        if _level_moves_on_this_loop():
            assert stalled._req.transport.get_write_buffer_size() > 0
        assert reading in state._ws_clients

    await _harness(ws_state, body)


@pytest.mark.asyncio
@pytest.mark.parametrize("forced_flat", [False, True], ids=["this-loop", "level-flat"])
async def test_a_stalled_tab_is_dropped_and_its_buffer_released(ws_state, monkeypatch, forced_flat):
    """D48: the server must not keep queueing frames for a tab that stopped reading."""
    verdict = _force_verdict(monkeypatch, False) if forced_flat else None

    async def body(state, stalled, reading, received):
        level_moves = not forced_flat and _level_moves_on_this_loop()
        transport = stalled._req.transport
        baseline_tasks = len(state._background_tasks)
        n, _ = await _broadcast(state, 0, 40)
        await _wait_received(received, n)
        under_limit = transport.get_write_buffer_size()
        # Control: a tab whose buffer is still under the limit is left alone.
        assert stalled in state._ws_clients, f"dropped at {under_limit} bytes buffered"
        assert not transport.is_closing()

        def buffered() -> int:
            return -1 if stalled not in state._ws_clients else transport.get_write_buffer_size()

        # Past the ceiling takes more frames than past the limit.
        n, peak = await _broadcast(state, n, 400 if level_moves else 1000, stop=buffered)
        assert stalled not in state._ws_clients, (
            f"a tab that reads nothing is still registered after {n} frames of "
            f"{FRAME // 1024} KiB, with {transport.get_write_buffer_size()} bytes buffered "
            f"for it (after 40 frames: {under_limit} bytes)"
        )
        assert stalled not in state._owner_ws_clients
        _assert_dropped_at(peak, level_moves)
        # abort() discards the buffer and ends every send waiting on its drain.
        assert transport.is_closing()
        assert transport.get_write_buffer_size() == 0
        await _wait_until(
            lambda: len(state._background_tasks) <= baseline_tasks,
            "the dropped tab's waiting send tasks finishing",
        )
        # Control: the reading tab is untouched and got every frame.
        await _wait_received(received, n)
        assert reading in state._ws_clients
        asked = verdict is None or verdict.calls
        assert asked, "the hub never asked whether the level shows reading"

    await _harness(ws_state, body)


@pytest.mark.asyncio
async def test_deliver_ws_owners_returns_while_an_owner_tab_is_stalled(ws_state, monkeypatch):
    """D47: one owner tab that stopped reading must not hold the awaited delivery."""
    monkeypatch.setattr(websocket_hub, "WS_OWNER_SEND_TIMEOUT_S", 1.0)
    # Where the level shows no reading (the proactor loop), only the cap ends the send.
    monkeypatch.setattr(websocket_hub, "WS_OWNER_SEND_MAX_S", 2.0)

    async def body(state, stalled, reading, received):
        transport = stalled._req.transport
        n = 0
        async with asyncio.timeout(30):
            # Past the kernel's buffers, so the transport pauses writing to it,
            # and under the drop limit, so only the send timeout can end it.
            while transport.get_write_buffer_size() <= 256 * 1024:
                n, _ = await _broadcast(state, n, 1)
        await _wait_until(
            lambda: stalled._req.protocol._paused, "the transport pausing writes to the stalled tab"
        )
        assert stalled in state._owner_ws_clients, "precondition: the stalled tab is an owner"
        assert stalled._req.protocol._paused, "precondition: writing to it is paused"
        pad = "x" * FRAME
        # The writer waits for a drain only on the send that crosses its byte
        # limit, so deliver until one does; on a paused socket that send never ends.
        for attempt in range(20):
            frame = 1000 + attempt
            try:
                delivered = await asyncio.wait_for(
                    state.deliver_ws_owners("stall_probe", {"n": frame, "pad": pad}), timeout=30
                )
            except TimeoutError:
                pytest.fail(
                    f"deliver_ws_owners call {attempt + 1} did not return within 30 s while one "
                    f"owner tab stopped reading (buffered={transport.get_write_buffer_size()} "
                    f"bytes); the reading tab had that frame: {frame in received}"
                )
            await _wait_received(received, n + attempt + 1)
            assert frame in received, "the reading owner tab must still get the frame"
            if stalled not in state._owner_ws_clients:
                break
        assert stalled not in state._owner_ws_clients, "the stalled owner tab was never dropped"
        assert stalled not in state._ws_clients
        assert delivered == 1, "a timed-out tab counts as not delivered"
        assert transport.is_closing()
        assert transport.get_write_buffer_size() == 0
        assert reading in state._owner_ws_clients

    await _harness(ws_state, body)


#: What one owner tab already holds when the frame is sent, and the frame's padding.
#: Together about 2 MiB, under ``WS_MAX_BUFFERED_BYTES``, so only the send's own bound
#: can drop the tab. Each test runs on a ``_VirtualTimeLoop``, so its waits take no
#: real time.
_OWNER_BACKLOG = 1024 * 1024
_OWNER_FRAME_PAD = 1024 * 1024


@pytest.mark.parametrize("level_moves", [True, False], ids=["level-moves", "level-flat"])
def test_a_slow_owner_tab_that_still_reads_keeps_its_delivery(ws_state, monkeypatch, level_moves):
    """A send to an owner tab returns once the tab's buffer drains, so how long it takes
    is the backlog over the link speed, not whether the tab reads. A tab that reads
    128 KiB a second takes 17 s to drain its backlog and the frame, longer than the
    window, and keeps its delivery because its buffer keeps shrinking. Where the level
    shows no reading, only the cap bounds the send, and 17 s is under it."""
    verdict = _force_verdict(monkeypatch, level_moves)
    link = _SlowLink(_OWNER_BACKLOG, chunk=128 * 1024, every=1.0)
    tab, delivered, took = _deliver_in_virtual_time(ws_state, link, _OWNER_FRAME_PAD)
    assert delivered == 1, (
        f"a tab that read 128 KiB every second was dropped {link.aborted_at} s into the "
        f"send, after {len(link.read_at)} reads, with {link.unread_at_abort} bytes unread"
    )
    assert link.aborted_at is None
    assert tab in ws_state._owner_ws_clients
    assert link.buffered == 0
    assert took == link.read_at[-1] == 17.0, (took, link.read_at[-1:])
    assert took > websocket_hub.WS_OWNER_SEND_TIMEOUT_S
    assert verdict.calls, "the hub never asked whether the level shows reading"


def test_an_owner_tab_that_stops_reading_mid_send_is_dropped_after_the_window(
    ws_state, monkeypatch
):
    """The tab reads twice, a second apart, then stops. Its send ends once its buffer
    has not shrunk for ``WS_OWNER_SEND_TIMEOUT_S``, long before the cap."""
    _force_verdict(monkeypatch, True)
    link = _SlowLink(_OWNER_BACKLOG, chunk=128 * 1024, every=1.0, reads=2)
    tab, delivered, _took = _deliver_in_virtual_time(ws_state, link, _OWNER_FRAME_PAD)
    assert delivered == 0, "a tab that stopped reading kept its owner send"
    assert link.read_at == [1.0, 2.0]
    assert link.aborted_at is not None, "a tab that stopped reading was not aborted"
    window = websocket_hub.WS_OWNER_SEND_TIMEOUT_S
    poll = getattr(websocket_hub, "_OWNER_SEND_POLL_S", 0.25)
    since = link.aborted_at - link.read_at[-1]
    assert window <= since <= window + 2 * poll, (
        f"dropped {link.aborted_at:g} s into the send, {since:g} s after the tab's last "
        f"read; a send ends once the buffer has not shrunk for {window:g} s"
    )
    assert tab not in ws_state._owner_ws_clients
    assert tab not in ws_state._ws_clients


@pytest.mark.parametrize("level_moves", [True, False], ids=["level-moves", "level-flat"])
def test_an_owner_tab_that_reads_too_slowly_is_dropped_at_the_cap(
    ws_state, monkeypatch, level_moves
):
    """A tab that reads 4 KiB a second keeps shrinking its buffer but would take over
    eight minutes to drain it. Its send ends at ``WS_OWNER_SEND_MAX_S``, so it cannot
    hold its caller past that."""
    verdict = _force_verdict(monkeypatch, level_moves)
    link = _SlowLink(_OWNER_BACKLOG, chunk=4 * 1024, every=1.0)
    tab, delivered, _took = _deliver_in_virtual_time(ws_state, link, _OWNER_FRAME_PAD)
    cap = getattr(websocket_hub, "WS_OWNER_SEND_MAX_S", None)
    poll = getattr(websocket_hub, "_OWNER_SEND_POLL_S", 0.25)
    assert cap is not None, f"the send ended {link.aborted_at} s in, and the hub has no cap"
    assert delivered == 0
    assert link.aborted_at is not None and cap <= link.aborted_at <= cap + poll, (
        f"a tab still reading 4 KiB a second was dropped {link.aborted_at} s into the "
        f"send, not at the {cap:g} s cap"
    )
    assert len(link.read_at) >= cap - 1, "the tab stopped reading before the cap"
    assert tab not in ws_state._owner_ws_clients
    assert verdict.calls, "the hub never asked whether the level shows reading"
