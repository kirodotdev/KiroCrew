"""The relay client's loop around a move, a stop and a failure, against a fake relay.

A move that gives up stops its dial, a move outlives the old connection's end for
the rest of its window, a stop that lands during a host-token fetch never dials,
and a loop that fails leaves no connection serving behind it.
"""

from __future__ import annotations

import asyncio
from typing import Dict, List

import pytest
from aiohttp import web
from relay_client_helpers import (
    MOVE_TOKEN,
    OWNER,
    TOKEN,
    FakeCrew,
    FakeRelay,
    MoveTokenSource,
    RecordingSleep,
    make_client,
    ready_frame,
    stop,
    valid_message,
)

from kiro_crew.relay_client.client import STATE_CONNECTED, STATE_ERROR
from kiro_crew.relay_client.connection import HostConnection, RelayTimings
from kiro_crew.relay_client.token_source import HostCredential, StaticHostTokenSource
from kiro_crew.testing.wait import async_wait_until, await_until

pytestmark = pytest.mark.asyncio


def _credential() -> HostCredential:
    return HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER)


class _HeldSource(StaticHostTokenSource):
    """Answers ``current()`` at once until call *hold_from*, which waits for ``gate``."""

    def __init__(self, hold_from: int) -> None:
        super().__init__(_credential())
        self.hold_from = hold_from
        self.calls = 0
        self.gate = asyncio.Event()
        self.held = asyncio.Event()

    async def current(self) -> HostCredential:
        self.calls += 1
        if self.calls >= self.hold_from:
            self.held.set()
            await self.gate.wait()
        return await super().current()


class _HoldMoveUpgrade(FakeRelay):
    """A relay that holds every upgrade carrying :data:`MOVE_TOKEN` until ``release``."""

    def __init__(self) -> None:
        super().__init__(tokens={TOKEN, MOVE_TOKEN})
        self.release = asyncio.Event()

    async def _host(self, request: web.Request) -> web.StreamResponse:
        if request.headers.get("Authorization", "") == f"Bearer {MOVE_TOKEN}":
            self.dials += 1
            await self.release.wait()
            return web.Response(status=503, text="released")
        return await super()._host(request)


async def test_a_given_up_move_cancels_its_dial_so_the_old_ones_end_redials_at_once() -> None:
    async with _HoldMoveUpgrade() as relay, FakeCrew() as crew:
        # The held dial would otherwise run for an hour: only a cancel ends it.
        source = MoveTokenSource()
        client, _, ingress = make_client(
            relay, crew, source=source, timings=RelayTimings(dial=3600.0)
        )  # flake-ok: an hour, never reached: the client cancels the dial it gave up
        await client.start()
        try:
            old = await relay.next_leg()
            await old.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            # No time to move: the client gives the move up with its dial unanswered,
            # and the relay then ends the old connection, so the client must redial.
            await old.send({"type": "reconnect", "reason": "drain", "closeInSeconds": 0})
            await async_wait_until(lambda: source.calls == 2)
            await old.close(1012)
            third = await relay.next_leg()
            assert third.token == TOKEN
            await third.handshake()
            await async_wait_until(
                lambda: client.state == STATE_CONNECTED and len(client._live) == 1
            )
        finally:
            relay.release.set()
            await stop(client, ingress)


async def test_a_move_adopts_a_replacement_that_gets_ready_after_the_old_one_ended(
    monkeypatch,
) -> None:
    runs: Dict[HostConnection, "asyncio.Task[object]"] = {}
    real_run = HostConnection.run

    async def recording_run(self: HostConnection):
        task = asyncio.current_task()
        assert task is not None
        runs[self] = task
        return await real_run(self)

    monkeypatch.setattr(HostConnection, "run", recording_run)
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            old = await relay.next_leg()
            await old.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            (old_conn,) = client._live
            await old.send(valid_message("frames/reconnect", 1))
            new = await relay.next_leg()
            await new.recv_type("hello")
            # The relay ends the old connection well inside the 30-second window,
            # before the replacement's ready.
            await old.close(1012)
            await await_until(runs[old_conn], what="the old connection's run")
            for _ in range(20):
                await asyncio.sleep(0)  # let the move see the old connection's end
            assert not new.closed.is_set()
            await new.send(ready_frame())
            await async_wait_until(lambda: client.relay_connected())
            assert client.state == STATE_CONNECTED
            assert not new.closed.is_set()
            assert relay.dials == 2
            assert client.delays == []
        finally:
            await stop(client, ingress)


async def test_a_stop_during_the_first_token_fetch_never_dials() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        source = _HeldSource(hold_from=1)
        client, _, ingress = make_client(relay, crew, source=source)
        await client.start()
        await await_until(source.held, what="the token fetch")
        stopping = asyncio.ensure_future(client.stop())
        await async_wait_until(lambda: client._stopping.is_set())
        # The fetch answers while the stop waits for the loop.
        source.gate.set()
        await await_until(stopping, what="the relay client to stop")
        await ingress.aclose()
        assert relay.dials == 0
        assert not client.running and not client._live


async def test_a_stop_during_a_moves_token_fetch_never_dials_the_replacement() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        source = _HeldSource(hold_from=2)
        client, _, ingress = make_client(relay, crew, source=source)
        await client.start()
        old = await relay.next_leg()
        await old.handshake()
        await async_wait_until(lambda: client.state == STATE_CONNECTED)
        await old.send(valid_message("frames/reconnect", 1))
        await await_until(source.held, what="the move's token fetch")
        stopping = asyncio.ensure_future(client.stop())
        await async_wait_until(lambda: client._stopping.is_set())
        source.gate.set()
        await await_until(stopping, what="the relay client to stop")
        await ingress.aclose()
        assert await old.wait_closed() == 1001
        assert relay.dials == 1
        assert not client.running and not client._live


async def test_a_loop_that_fails_closes_its_connections_and_unbinds_first() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew, sleep=RecordingSleep())
        seen_at_error: List[object] = []
        calls = {"n": 0}
        real_current = source.current

        async def current() -> HostCredential:
            calls["n"] += 1
            if calls["n"] >= 2:
                raise OverflowError("int too large to convert to float")
            return await real_current()

        source.current = current  # type: ignore[method-assign]
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            assert crew.gateway.binding is not None
            # The move's token fetch raises a type the move does not expect.
            await leg.send(valid_message("frames/reconnect", 1))

            def failed() -> bool:
                if client.state != STATE_ERROR:
                    return False
                seen_at_error.append((set(client._live), crew.gateway.binding))
                return True

            await async_wait_until(failed)
            assert seen_at_error[0] == (set(), None)
            assert client.snapshot()["error"] == "relay client failed: OverflowError"
            assert not client.relay_connected()
            assert await leg.wait_closed() == 1001
            assert relay.dials == 1
        finally:
            await stop(client, ingress)
