"""Keepalive, reconnect backoff, and what the client does on each host-leg close code.

The close-code table and the timing numbers come from the copied protocol v1
fixtures (``close-codes/host-leg.json``, ``timing/keepalive-and-backoff.json``), so a
change to either side of the contract fails here.
"""

from __future__ import annotations

import random

import pytest
from relay_client_helpers import (
    FakeCrew,
    FakeRelay,
    RecordingSleep,
    fixture,
    make_client,
    stop,
    valid_message,
)

from kiro_crew.relay_client.backoff import Backoff, CloseAction, ConnectionEnd, classify
from kiro_crew.relay_client.client import STATE_CONNECTED, STATE_ERROR
from kiro_crew.relay_client.connection import RelayTimings
from kiro_crew.relay_client.protocol import (
    BACKOFF_RESET_AFTER_SECONDS,
    BACKOFF_STEPS_SECONDS,
    PING_INTERVAL_SECONDS,
    SILENCE_REPLACE_SECONDS,
)
from kiro_crew.testing.wait import async_wait_until

#: What the client does when the relay closes a connection that had reached ready.
EXPECTED_ACTION = {
    4401: CloseAction.RENEW_ONCE,
    4403: CloseAction.RENEW_THEN_BACKOFF,
    4408: CloseAction.BACKOFF,
    4409: CloseAction.STOP_REPLACED,
    4429: CloseAction.BACKOFF,
    1012: CloseAction.RECONNECT_NOW,
    1013: CloseAction.BACKOFF,
}


def _relay_codes() -> list[int]:
    return [
        row["code"] for row in fixture("close-codes/host-leg")["codes"] if row["sentBy"] == "relay"
    ]


class TestTheContractNumbers:
    def test_keepalive_and_backoff_numbers_match_the_fixture(self) -> None:
        timing = fixture("timing/keepalive-and-backoff")
        assert PING_INTERVAL_SECONDS == timing["keepalive"]["pingIntervalSeconds"]
        assert SILENCE_REPLACE_SECONDS == timing["keepalive"]["replaceAfterSilenceSeconds"]
        assert list(BACKOFF_STEPS_SECONDS) == timing["backoff"]["stepsSeconds"]
        assert BACKOFF_RESET_AFTER_SECONDS == 60.0
        assert "60 seconds" in timing["backoff"]["resetWhen"]

    def test_every_relay_close_code_has_an_action(self) -> None:
        assert set(_relay_codes()) == set(EXPECTED_ACTION)

    @pytest.mark.parametrize("code", sorted(EXPECTED_ACTION))
    def test_classify_each_relay_code(self, code: int) -> None:
        assert classify(ConnectionEnd(code=code, reached_ready=True)) is EXPECTED_ACTION[code]

    def test_hello_refusals_stop_rather_than_retry(self) -> None:
        assert (
            classify(ConnectionEnd(code=4401, reason="version_unsupported"))
            is CloseAction.STOP_UPGRADE
        )
        assert (
            classify(ConnectionEnd(code=4401, reason="mode_unsupported"))
            is CloseAction.STOP_PROTOCOL
        )

    def test_a_drain_before_ready_backs_off_instead_of_spinning(self) -> None:
        assert classify(ConnectionEnd(code=1012, reached_ready=False)) is CloseAction.BACKOFF

    @pytest.mark.parametrize(
        "end", [ConnectionEnd(), ConnectionEnd(code=1006), ConnectionEnd(code=4000)]
    )
    def test_any_other_end_is_try_again_later(self, end: ConnectionEnd) -> None:
        # transportCloses: a socket that dies with no close frame is handled like 1013.
        assert "like 1013" in fixture("close-codes/host-leg")["transportCloses"]
        assert classify(end) is CloseAction.BACKOFF

    def test_a_refused_upgrade(self) -> None:
        assert classify(ConnectionEnd(http_status=401)) is CloseAction.RENEW_ONCE
        assert classify(ConnectionEnd(http_status=503)) is CloseAction.BACKOFF


class TestBackoff:
    def test_steps_then_stay_at_the_last(self) -> None:
        class Top(random.Random):
            def uniform(self, a: float, b: float) -> float:
                return b

        backoff = Backoff(rng=Top())
        assert [backoff.next_delay() for _ in range(7)] == [2.0, 5.0, 15.0, 30.0, 60.0, 60.0, 60.0]
        backoff.reset()
        assert backoff.next_delay() == 2.0

    def test_full_jitter_stays_between_zero_and_the_step(self, seeded_rng: random.Random) -> None:
        backoff = Backoff(rng=seeded_rng)
        for step in (2.0, 5.0, 15.0, 30.0, 60.0, 60.0):
            assert 0.0 <= backoff.next_delay() <= step


asyncio_test = pytest.mark.asyncio


@asyncio_test
async def test_the_client_backs_off_through_the_steps() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            for _ in range(6):
                leg = await relay.next_leg()
                await leg.recv_type("hello")
                await leg.close(1013)
            await async_wait_until(lambda: len(client.delays) >= 6)
            assert client.delays[:6] == [2.0, 5.0, 15.0, 30.0, 60.0, 60.0]
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_the_backoff_starts_again_after_a_connection_held_ready() -> None:
    now = [1000.0]
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(
            relay, crew, sleep=RecordingSleep(), monotonic=lambda: now[0]
        )
        await client.start()
        try:
            for _ in range(2):
                leg = await relay.next_leg()
                await leg.recv_type("hello")
                await leg.close(1013)
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            now[0] += BACKOFF_RESET_AFTER_SECONDS + 1
            await leg.close(1013)
            await relay.next_leg()
            assert client.delays[:3] == [2.0, 5.0, 2.0]
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_the_client_pings_on_its_interval_and_answers_pings() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, timings=RelayTimings(ping_interval=0.05))
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            first = await leg.recv_type("ping")
            second = await leg.recv_type("ping")
            assert first["nonce"] != second["nonce"]
            assert len(first["nonce"]) <= 64
            await leg.send({"type": "pong", "nonce": first["nonce"]})
            assert client.state == STATE_CONNECTED
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_a_silent_socket_is_replaced() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(
            relay,
            crew,
            timings=RelayTimings(ping_interval=3600, silence=0.3),
            sleep=RecordingSleep(),
        )
        await client.start()
        try:
            silent = await relay.next_leg()
            await silent.handshake()
            # The relay sends nothing more; the client gives the socket up and redials.
            replacement = await relay.next_leg()
            await replacement.recv_type("hello")
            assert silent.closed.is_set()
            assert client.delays == [2.0]
        finally:
            await stop(client, ingress)


async def _connected(relay: FakeRelay, client) -> object:
    leg = await relay.next_leg()
    await leg.handshake()
    await async_wait_until(lambda: client.state == STATE_CONNECTED)
    return leg


@asyncio_test
@pytest.mark.parametrize("code", [4408, 4429, 1013])
async def test_try_again_later_codes_back_off_and_redial(code: int) -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            leg = await _connected(relay, client)
            await leg.close(code)
            await (await relay.next_leg()).recv_type("hello")
            assert client.delays == [2.0]
            assert source.renewals == []
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_a_dead_socket_backs_off_and_redials() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            leg = await _connected(relay, client)
            leg.abort()
            await (await relay.next_leg()).recv_type("hello")
            assert client.delays == [2.0]
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_a_drain_redials_at_once() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            leg = await _connected(relay, client)
            await leg.close(1012)
            await (await relay.next_leg()).recv_type("hello")
            assert client.delays == []
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_an_expired_host_token_renews_then_backs_off() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            leg = await _connected(relay, client)
            await leg.close(4403)
            await (await relay.next_leg()).recv_type("hello")
            assert source.renewals == [True]
            assert client.delays == [2.0]
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_a_refused_host_token_renews_once_then_stops() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            leg = await _connected(relay, client)
            await leg.close(4401)
            retry = await relay.next_leg()
            assert source.renewals == [True]
            assert client.delays == []
            await retry.recv_type("hello")
            # The renewed token is refused too: the client stops and says why.
            await retry.close(4401)
            await async_wait_until(lambda: client.state == STATE_ERROR)
            assert source.renewals == [True]
            assert "refused the host token" in client.snapshot()["error"]
            assert relay.dials == 2
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_a_source_that_cannot_renew_yet_does_not_spend_the_one_renewal() -> None:
    from kiro_crew.relay_client.token_source import HostTokenRetryLater

    async with FakeRelay() as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew, sleep=RecordingSleep())
        real_renew = source.renew
        calls: list[str] = []

        async def renew(*, host_token_requested: bool):
            calls.append("renew")
            if len(calls) == 1:
                raise HostTokenRetryLater("the backend is down")
            return await real_renew(host_token_requested=host_token_requested)

        source.renew = renew  # type: ignore[method-assign]
        await client.start()
        try:
            leg = await _connected(relay, client)
            await leg.close(4401)
            # The renewal could not happen: back off and dial with the same token.
            second = await relay.next_leg()
            await second.recv_type("hello")
            assert client.delays == [2.0]
            await second.close(4401)
            # This refusal still gets its one renewal, and the client recovers.
            third = await relay.next_leg()
            await third.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            assert calls == ["renew", "renew"]
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_another_instance_holding_the_crew_stops_the_client() -> None:
    lost: list[bool] = []

    async def on_disconnect() -> None:
        lost.append(True)

    async with FakeRelay() as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew, sleep=RecordingSleep())
        client.register_callbacks(on_connect=None, on_disconnect=on_disconnect)
        await client.start()
        try:
            leg = await _connected(relay, client)
            await leg.close(4409)
            await async_wait_until(lambda: client.state == STATE_ERROR)
            assert "another instance" in client.snapshot()["error"]
            assert lost == [True]
            assert client.public_url == ""
            assert relay.dials == 1 and source.renewals == []
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_a_version_refusal_stops_and_asks_for_an_upgrade() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.recv_type("hello")
            await leg.send(valid_message("frames/error", 1))
            await leg.close(4401, "version_unsupported")
            await async_wait_until(lambda: client.state == STATE_ERROR)
            assert "newer protocol version" in client.snapshot()["error"]
            assert source.renewals == [] and relay.dials == 1
        finally:
            await stop(client, ingress)


@asyncio_test
async def test_a_malformed_ready_drops_the_socket_and_backs_off() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.recv_type("hello")
            await leg.send(fixture("frames/ready")["invalid"][0]["message"])
            await leg.wait_closed()
            await (await relay.next_leg()).recv_type("hello")
            assert client.delays == [2.0]
        finally:
            await stop(client, ingress)
