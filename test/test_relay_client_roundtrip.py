"""The relay client against a fake relay: handshake, requests, streaming, WebSockets, credit.

Every frame the fake relay sends starts from a protocol v1 fixture, so these tests
fail when the client stops speaking the contract the relay is built to.
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
from unittest.mock import MagicMock

import pytest
from relay_client_helpers import (
    OWNER,
    TOKEN,
    FakeCrew,
    FakeRelay,
    RecordingSleep,
    fixture,
    make_client,
    ready_frame,
    stop,
    valid_message,
)

from kiro_crew.dashboard.kiro_relay.caller import RELAY_USER_HEADER
from kiro_crew.dashboard.relay_headers import is_relay_header
from kiro_crew.relay_client import connection as connection_module
from kiro_crew.relay_client.client import STATE_CONNECTED
from kiro_crew.relay_client.connection import HostConnection, RelayTimings
from kiro_crew.relay_client.protocol import DRAIN_BACKSTOP_SECONDS
from kiro_crew.relay_client.streams import HttpStream
from kiro_crew.relay_client.token_source import HostCredential
from kiro_crew.testing.wait import async_wait_until, await_until, until_parked

pytestmark = pytest.mark.asyncio


def _connection(client):
    (conn,) = [c for c in client._live if c.ready.is_set()]
    return conn


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


async def test_handshake_sends_hello_with_the_host_token_and_connects_on_ready() -> None:
    connected: list[str] = []

    async def on_connect(url: str) -> None:
        connected.append(url)

    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        client.register_callbacks(on_connect=on_connect, on_disconnect=None)
        await client.start()
        try:
            leg = await relay.next_leg()
            hello = await leg.recv_type("hello")
            shape = fixture("frames/hello")
            assert set(hello) <= set(shape["fields"])
            assert {k for k, v in shape["fields"].items() if v["required"]} <= set(hello)
            assert hello["protocolVersion"] == 1 and hello["mode"] == "http"
            assert set(hello["features"]) == {"flow-credit", "ws-flow", "request-credit"}
            assert leg.token == TOKEN
            assert leg.user_agent.startswith("kiro-crew/")

            ready = ready_frame()
            await leg.send(ready)
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            url = f"https://{ready['endpointId']}.kiro-relay.us-east-1.on.aws"
            assert connected == [url]
            assert client.public_url == url
            assert client.snapshot()["state"] == "connected"
        finally:
            await stop(client, ingress)
        assert await leg.wait_closed() == 1001


async def test_a_refused_upgrade_never_holds_a_socket() -> None:
    async with FakeRelay(tokens={"another-token"}) as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew)
        await client.start()
        try:
            await async_wait_until(lambda: client.state == "error")
            # A 401 renews the token once and redials; the second 401 stops.
            assert relay.refused == [TOKEN, TOKEN]
            assert source.renewals == [True]
            assert "refused the host token" in client.snapshot()["error"]
        finally:
            await stop(client, ingress)


async def test_request_round_trip_reaches_the_crew_and_streams_back() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message("frames/request")
            # A client-supplied relay header must never reach the web server.
            head["headers"] = {**head["headers"], RELAY_USER_HEADER: "u-0003.spoofed"}
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            response, body = await leg.read_response(head["id"])
            assert response["status"] == 200
            assert response["streaming"] is True
            assert response["headers"]["content-type"].startswith("application/json")
            assert "set-cookie" not in response["headers"]
            assert sorted(c.split("=")[0] for c in response["setCookies"]) == [
                "kc_session",
                "kc_theme",
            ]
            assert json.loads(body) == {"ok": True, "query": "limit=20"}
            (seen,) = crew.seen
            headers = {k.lower(): v for k, v in seen["headers"].items()}
            # The Crew's own Authorization passes through untouched.
            assert headers["authorization"] == "Bearer crew-own-session-credential"
            # The dashboard's relay ingress admitted the hand-off and named the
            # person from the relay's message, not from the spoofed header.
            assert seen["caller"] is not None
            assert seen["caller"].kiro_user_id == head["user"]
            assert seen["caller"].is_owner
            assert seen["remote"] == "kiro-relay"
            assert not any(is_relay_header(name) for name in headers)
        finally:
            await stop(client, ingress)


async def test_document_navigation_round_trip_does_not_redirect_to_localhost() -> None:
    async with FakeRelay() as relay, FakeCrew(canonical_redirect=True) as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message(
                "frames/request",
                method="GET",
                path="/",
                query="",
                headers={"sec-fetch-dest": "document"},
            )
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            response, body = await leg.read_response(head["id"])
            assert response["status"] == 200
            assert "location" not in {name.lower() for name in response["headers"]}
            assert json.loads(body) == {"ok": True, "query": ""}
            (seen,) = crew.seen
            assert seen["path"] == "/"
            assert seen["caller"].kiro_user_id == OWNER
        finally:
            await stop(client, ingress)


async def test_a_personal_crew_refuses_a_relayed_stranger() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message("frames/request", user="u-0003.example-stranger")
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            response, body = await leg.read_response(head["id"])
            assert response["status"] == 403
            assert json.loads(body)["code"] == "relay_not_owner"
            assert crew.seen == []
        finally:
            await stop(client, ingress)


async def test_the_handover_is_retired_when_the_connection_ends() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, sleep=RecordingSleep())
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: crew.gateway.binding is not None)
            assert crew.gateway.binding.owner_user_id == OWNER
            await leg.close(1013)
            await async_wait_until(lambda: crew.gateway.binding is None)
        finally:
            await stop(client, ingress)


async def test_request_body_streams_to_the_crew_in_chunks() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            payload = bytes(range(256)) * 400
            head = valid_message(
                "frames/request",
                1,
                path="/upload",
                user=OWNER,
                headers={
                    "content-type": "application/octet-stream",
                    "content-length": str(len(payload)),
                },
            )
            await leg.send(head)
            for start in range(0, len(payload), 40960):
                chunk = valid_message(
                    "frames/request-chunk", id=head["id"], data=_b64(payload[start : start + 40960])
                )
                await leg.send(chunk)
            await leg.send(valid_message("frames/request-end", id=head["id"]))
            response, body = await leg.read_response(head["id"])
            assert response["status"] == 200
            assert json.loads(body)["bytes"] == len(payload)
        finally:
            await stop(client, ingress)


async def test_a_large_response_streams_in_body_sized_chunks() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        crew.stream_bytes = 300 * 1024
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message("frames/request", path="/stream", query="", headers={})
            # No initialCredit: the relay grants nothing, so nothing gates the body.
            del head["initialCredit"]
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            await leg.recv_type("response")
            sizes = []
            total = b""
            while True:
                frame = await leg.recv()
                if frame["type"] == "response-end":
                    break
                piece = base64.b64decode(frame["data"])
                sizes.append(len(piece))
                total += piece
            assert len(total) == crew.stream_bytes
            assert max(sizes) <= 40960
            assert len(sizes) > 1
        finally:
            await stop(client, ingress)


async def test_credit_stall_and_resume() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        crew.stream_bytes = 200 * 1024
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            initial = 64 * 1024
            head = valid_message(
                "frames/request", path="/stream", query="", headers={}, initialCredit=initial
            )
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            await leg.recv_type("response")
            received = 0
            while received < initial:
                frame = await leg.recv()
                assert frame["type"] == "response-chunk", frame
                received += len(base64.b64decode(frame["data"]))
            # Exactly the initial credit went out, and the stream now waits for more.
            assert received == initial
            stream = _connection(client)._streams[head["id"]]
            assert isinstance(stream, HttpStream)
            await async_wait_until(
                lambda: stream.credit.stalled, describe=lambda: stream.credit.available
            )
            assert leg.inbox.empty()

            grant = valid_message("frames/flow-credit", id=head["id"])
            await leg.send(grant)
            while True:
                frame = await leg.recv()
                if frame["type"] == "response-end":
                    break
                received += len(base64.b64decode(frame["data"]))
                assert received <= initial + grant["credit"]
            assert received == crew.stream_bytes
        finally:
            await stop(client, ingress)


async def test_websocket_echo_both_ways() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            upgrade = valid_message("frames/ws-upgrade")
            await leg.send(upgrade)
            text = valid_message("frames/ws-frame", 0)
            await leg.send(text)
            echo = await leg.recv_type("ws-frame")
            assert echo == {"type": "ws-frame", "id": upgrade["id"], "data": text["data"]}

            binary = valid_message("frames/ws-frame", 1)
            await leg.send(binary)
            echo = await leg.recv_type("ws-frame")
            assert echo["binary"] is True and base64.b64decode(echo["data"]) == b"\x00\x01\x02\x03"

            # A message over one frame's size goes out in partial pieces and back whole.
            big = "x" * 150_000
            pieces = [big[i : i + 50_000] for i in range(0, len(big), 50_000)]
            for index, piece in enumerate(pieces):
                frame = {"type": "ws-frame", "id": upgrade["id"], "data": piece}
                if index < len(pieces) - 1:
                    frame["partial"] = True
                await leg.send(frame)
            echoed = ""
            while True:
                frame = await leg.recv_type("ws-frame")
                echoed += frame["data"]
                if not frame.get("partial"):
                    break
            assert echoed == big

            (seen,) = crew.seen
            assert seen["path"] == "/ws"
            await leg.send(valid_message("frames/ws-close", 0, id=upgrade["id"]))
            await async_wait_until(lambda: not _connection(client)._streams)
        finally:
            await stop(client, ingress)


async def test_held_websockets_do_not_starve_http_or_further_upgrades() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            # Exceed aiohttp's default pool limit without changing stream admission.
            socket_ids = set(range(1, 102))
            for sid in sorted(socket_ids):
                await leg.send(valid_message("frames/ws-upgrade", id=sid))
                frame = {"type": "ws-frame", "id": sid, "data": "opened"}
                await leg.send(frame)
                # An echo proves the upgrade completed, not merely that it was admitted.
                assert await leg.recv_type("ws-frame") == frame

            head = valid_message("frames/request", id=102)
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            response, body = await leg.read_response(head["id"])
            assert response["status"] == 200
            assert json.loads(body)["ok"] is True
            assert socket_ids <= set(_connection(client)._streams)
            # Every held socket still answers after HTTP completes; none freed a slot.
            for sid in sorted(socket_ids):
                frame = {"type": "ws-frame", "id": sid, "data": "still open"}
                await leg.send(frame)
                assert await leg.recv_type("ws-frame") == frame
        finally:
            await stop(client, ingress)


async def test_a_refused_websocket_upgrade_closes_with_4502() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            upgrade = valid_message("frames/ws-upgrade", path="/ws-refused", query="")
            await leg.send(upgrade)
            close = await leg.recv_type("ws-close")
            assert close["id"] == upgrade["id"]
            assert close["code"] == 4502
            assert "403" in close["reason"]
            assert len(close["reason"].encode("utf-8")) <= 123
        finally:
            await stop(client, ingress)


async def test_a_crew_that_drops_the_request_reports_crew_unreachable() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message("frames/request", path="/drop", query="", headers={})
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            error = await leg.recv_type("error")
            assert error["code"] == "crew_unreachable" and error["id"] == head["id"]
            await async_wait_until(lambda: not _connection(client)._streams)
        finally:
            crew.release.set()
            await stop(client, ingress)


async def test_streams_past_the_connection_cap_are_refused(monkeypatch) -> None:
    from kiro_crew.relay_client import connection as connection_mod

    monkeypatch.setattr(connection_mod, "MAX_STREAMS_PER_CONNECTION", 2)
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            for sid in (21, 22):
                head = valid_message("frames/request", id=sid, path="/slow", query="", headers={})
                await leg.send(head)
                await leg.send({"type": "request-end", "id": sid})
            await async_wait_until(lambda: len(crew.seen) == 2)
            third = valid_message("frames/request", id=23, path="/slow", query="", headers={})
            await leg.send(third)
            error = await leg.recv_type("error")
            assert error["code"] == "crew_unreachable" and error["id"] == 23
            conn = _connection(client)
            assert set(conn._streams) == {21, 22}
            assert conn.refused_streams == 1
            # Once one ends, a new stream is taken again.
            crew.release.set()
            await async_wait_until(lambda: not conn._streams)
            fourth = valid_message("frames/request", id=24, path="/api/sessions", query="")
            await leg.send(fourth)
            await leg.send({"type": "request-end", "id": 24})
            # Frames for 21 and 22 finishing may come first; read past them.
            status = None
            while True:
                frame = await leg.recv()
                if frame.get("id") != 24:
                    continue
                if frame["type"] == "response":
                    status = frame["status"]
                if frame["type"] == "response-end":
                    break
            assert status == 200
        finally:
            crew.release.set()
            await stop(client, ingress)


async def test_a_crew_that_cannot_be_reached_ends_only_that_stream() -> None:
    # A bound socket that never listens refuses every connection, and holding it
    # open keeps any other process from taking the port mid-test.
    closed_port = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    closed_port.bind(("127.0.0.1", 0))
    async with FakeRelay() as relay:
        client, _, ingress = make_client(relay, closed_port.getsockname()[1])
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message("frames/request")
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            error = await leg.recv_type("error")
            assert error["code"] == "crew_unreachable" and error["id"] == head["id"]
            assert client.state == STATE_CONNECTED
        finally:
            await stop(client, ingress)
            closed_port.close()


async def test_a_crew_that_never_answers_reports_crew_not_answering() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, timings=RelayTimings(first_byte=0.2))
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message("frames/request", path="/slow", query="", headers={})
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            error = await leg.recv_type("error")
            assert error == {
                "type": "error",
                "code": "crew_not_answering",
                "message": error["message"],
                "id": head["id"],
            }
        finally:
            crew.release.set()
            await stop(client, ingress)


async def test_frames_that_break_the_contract_get_typed_errors() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            for case in fixture("frames/request")["invalid"]:
                if case["expect"].startswith("never sent"):
                    continue
                await leg.send(case["message"])
                error = await leg.recv_type("error")
                assert error["code"] == "invalid_message", case["name"]
            await leg.send(valid_message("frames/opaque"))
            assert (await leg.recv_type("error"))["code"] == "unsupported_frame"
            ping = valid_message("frames/ping")
            await leg.send(ping)
            assert await leg.recv_type("pong") == {"type": "pong", "nonce": ping["nonce"]}
            assert crew.seen == []
            assert client.state == STATE_CONNECTED
        finally:
            await stop(client, ingress)


async def test_a_request_cancel_stops_the_stream_and_sends_nothing_more() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message("frames/request", path="/slow", query="", headers={})
            await leg.send(head)
            await leg.send({"type": "request-end", "id": head["id"]})
            await async_wait_until(lambda: crew.seen)
            await leg.send(valid_message("frames/request-cancel", id=head["id"]))
            await async_wait_until(lambda: not _connection(client)._streams)
            ping = valid_message("frames/ping")
            await leg.send(ping)
            # The next frame after the cancel is the pong: nothing came for the stream.
            assert (await leg.recv(skip_keepalive=False))["type"] == "pong"
        finally:
            crew.release.set()
            await stop(client, ingress)


async def test_a_new_host_token_goes_down_the_connection() -> None:
    from kiro_crew.relay_client.token_source import HostCredential

    async with FakeRelay() as relay, FakeCrew() as crew:
        client, source, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            newer = valid_message("frames/token")["token"]
            await source.publish(HostCredential(newer))
            frame = await leg.recv_type("token")
            assert frame == {"type": "token", "token": newer}
        finally:
            await stop(client, ingress)


async def test_make_before_break_on_reconnect() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            old = await relay.next_leg()
            await old.handshake()
            head = valid_message("frames/request", path="/slow", query="", headers={})
            await old.send(head)
            await old.send({"type": "request-end", "id": head["id"]})
            await async_wait_until(lambda: crew.seen)

            await old.send(valid_message("frames/reconnect", 1))
            new = await relay.next_leg()
            await new.handshake()
            # The old connection stays open while its request is still running.
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            assert not old.closed.is_set()

            crew.release.set()
            response, body = await old.read_response(head["id"])
            assert response["status"] == 200 and body == b"finished"
            assert await old.wait_closed() == 1000
            assert not new.closed.is_set()
            assert client.delays == []
            assert client.state == STATE_CONNECTED
        finally:
            crew.release.set()
            await stop(client, ingress)


class _Clock:
    """The client's monotonic clock, moved on by hand."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def test_a_request_past_a_drains_close_in_seconds_is_answered_on_the_old_connection() -> None:
    # A drain gives the Crew 5 seconds to move. That bounds only the wait for the
    # new connection: a request still running on the old one past it is answered
    # there, and only then does the old one close with 1000.
    clock = _Clock()
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(
            relay, crew, monotonic=clock, timings=RelayTimings(move_grace=0.05)
        )
        await client.start()
        try:
            old = await relay.next_leg()
            await old.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            old_conn = _connection(client)
            head = valid_message("frames/request", path="/slow", query="", headers={})
            await old.send(head)
            await old.send({"type": "request-end", "id": head["id"]})
            await async_wait_until(lambda: crew.seen)

            drain = valid_message("frames/reconnect", 0)
            assert (drain["reason"], drain["closeInSeconds"]) == ("drain", 5)
            await old.send(drain)
            new = await relay.next_leg()
            # The request is still running when the 5 seconds to move are over.
            clock.now += drain["closeInSeconds"] + 1
            await new.handshake()
            await async_wait_until(
                lambda: client._draining is old_conn or old_conn.closed_by_client
            )
            for _ in range(20):
                await asyncio.sleep(0)  # let the drain take its first step
            assert not old_conn.closed_by_client
            assert not old.closed.is_set()

            crew.release.set()
            response, body = await old.read_response(head["id"])
            assert response["status"] == 200 and body == b"finished"
            assert await old.wait_closed() == 1000
            assert not new.closed.is_set()
            assert relay.dials == 2
            assert client.delays == []
            assert client.state == STATE_CONNECTED
        finally:
            crew.release.set()
            await stop(client, ingress)


async def test_a_relay_1012_on_the_draining_connection_ends_it_without_a_redial() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            old = await relay.next_leg()
            await old.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            old_conn = _connection(client)
            head = valid_message("frames/request", path="/slow", query="", headers={})
            await old.send(head)
            await old.send({"type": "request-end", "id": head["id"]})
            await async_wait_until(lambda: crew.seen)

            await old.send(valid_message("frames/reconnect", 1))
            new = await relay.next_leg()
            await new.handshake()
            await async_wait_until(lambda: client._draining is old_conn)
            # The relay ends the moved connection's window with its request open.
            await old.close(1012)
            await async_wait_until(lambda: client._draining is None)
            await async_wait_until(lambda: not client._background)
            assert old_conn.stream_count == 0
            assert relay.dials == 2
            assert client.delays == []
            assert client.state == STATE_CONNECTED
            assert client.relay_connected()
            assert not new.closed.is_set()
        finally:
            crew.release.set()
            await stop(client, ingress)


async def test_the_drain_backstop_is_past_every_window_the_relay_closes_in() -> None:
    timing = fixture("timing/keepalive-and-backoff")
    assert DRAIN_BACKSTOP_SECONDS > timing["drain"]["clientWindowSeconds"]
    assert DRAIN_BACKSTOP_SECONDS > timing["rebalancing"]["closeAfterSeconds"]
    assert RelayTimings().drain_backstop == DRAIN_BACKSTOP_SECONDS


async def test_a_request_that_reaches_the_old_connection_after_a_move_is_answered() -> None:
    # A grace far past the drain backstop, so only the backstop can end the drain:
    # the old connection closing at all proves the backstop caps the grace.
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(
            relay, crew, timings=RelayTimings(move_grace=3600.0, drain_backstop=2.0)
        )
        await client.start()
        try:
            old = await relay.next_leg()
            await old.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            await old.send(valid_message("frames/reconnect", 0, closeInSeconds=2))
            new = await relay.next_leg()
            await new.handshake()
            # The old connection is draining with nothing in flight...
            await async_wait_until(lambda: len(client._background) == 1)
            assert not old.closed.is_set()

            # ...when a request the relay wrote there before its move still arrives.
            head = valid_message("frames/request")
            await old.send(head)
            await old.send({"type": "request-end", "id": head["id"]})
            response, body = await old.read_response(head["id"])
            assert response["status"] == 200 and json.loads(body)["ok"] is True

            assert await old.wait_closed() == 1000
            assert not new.closed.is_set()
            assert client.delays == []
            assert client.state == STATE_CONNECTED
        finally:
            await stop(client, ingress)


@pytest.mark.parametrize("grace", [0.05, 0.0, float("nan")], ids=["short", "none", "nan"])
async def test_a_quiet_old_connection_closes_once_its_grace_passes(grace) -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, timings=RelayTimings(move_grace=grace))
        await client.start()
        try:
            old = await relay.next_leg()
            await old.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            await old.send(valid_message("frames/reconnect", 1))
            new = await relay.next_leg()
            await new.handshake()
            assert await old.wait_closed() == 1000
            await async_wait_until(lambda: not client._background)
            assert client._draining is None
            assert not new.closed.is_set()
        finally:
            await stop(client, ingress)


async def test_a_request_admitted_as_the_grace_runs_out_is_not_cut(monkeypatch) -> None:
    conn = HostConnection(
        session=MagicMock(),
        url="ws://127.0.0.1:9/v1/host",
        credential=HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER),
        ingress=MagicMock(),
        binding=("personal", OWNER),
        client_version="0.0.0-test",
        timings=RelayTimings(),
    )
    real_first_set = connection_module._first_set
    raced: list = []

    async def racing(*events, timeout):
        if conn._request_arrived in events and not raced:
            raced.append(True)
            # The grace timer and a request head land in the same tick: the wait
            # reports a timeout though the request was already admitted.
            conn.http_streams_idle.clear()
            conn._request_arrived.set()
            return False
        return await real_first_set(*events, timeout=timeout)

    monkeypatch.setattr(connection_module, "_first_set", racing)
    draining = asyncio.ensure_future(conn.drain_and_close(quiet=0.05))
    try:
        # The drain waits for the admitted request instead of closing under it.
        await until_parked(conn.http_streams_idle)
        assert raced and conn._closing_code is None
        conn.http_streams_idle.set()  # the request finished
        await await_until(draining, what="the drain to close")
        assert conn._closing_code == 1000
    finally:
        draining.cancel()
        await asyncio.gather(draining, return_exceptions=True)


async def test_a_newer_move_ends_an_older_drain_in_its_grace(monkeypatch) -> None:
    # A grace and a move window far past the test's own bounds: the older drain
    # ending at all proves the newer hand-off ended it, not a timer.
    monkeypatch.setattr(connection_module, "MOVE_WINDOW_MAX_SECONDS", 3600)
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew, timings=RelayTimings(move_grace=3600.0))
        await client.start()
        try:
            first = await relay.next_leg()
            await first.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            first_conn = _connection(client)
            await first.send(valid_message("frames/reconnect", 1, closeInSeconds=3600))
            second = await relay.next_leg()
            await second.handshake()
            await async_wait_until(lambda: client._draining is first_conn)

            await second.send(valid_message("frames/reconnect", 1, closeInSeconds=3600))
            third = await relay.next_leg()
            await third.handshake()
            assert await first.wait_closed() == 1000
            await async_wait_until(lambda: first_conn not in client._live)
            await async_wait_until(lambda: len(client._background) == 1)
            assert not second.closed.is_set() and not third.closed.is_set()
        finally:
            await stop(client, ingress)


async def test_back_to_back_moves_keep_only_one_draining_connection(monkeypatch) -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        handoffs = asyncio.Queue()
        now_connected = client._now_connected

        async def record_handoff(conn):
            await now_connected(conn)
            handoffs.put_nowait(conn)

        monkeypatch.setattr(client, "_now_connected", record_handoff)
        await client.start()
        try:
            first = await relay.next_leg()
            await first.handshake()
            first_conn = await await_until(
                asyncio.ensure_future(handoffs.get()), what="the first host to serve"
            )
            head = valid_message("frames/request", path="/slow", query="", headers={})
            await first.send(head)
            await first.send({"type": "request-end", "id": head["id"]})
            await async_wait_until(lambda: len(crew.seen) == 1)

            await first.send(valid_message("frames/reconnect", 1, closeInSeconds=3600))
            second = await relay.next_leg()
            await second.handshake()
            second_conn = await await_until(
                asyncio.ensure_future(handoffs.get()), what="the first move to hand off"
            )
            assert not first.closed.is_set()
            assert not first_conn.http_streams_idle.is_set()
            await second.send(head)
            await second.send({"type": "request-end", "id": head["id"]})
            await async_wait_until(lambda: len(crew.seen) == 2)

            await second.send(valid_message("frames/reconnect", 1, closeInSeconds=3600))
            third = await relay.next_leg()
            await third.recv_type("hello")
            # Neither existing connection is cut while the replacement is unready.
            assert not first.closed.is_set()
            assert not second.closed.is_set()
            await third.send(ready_frame())
            third_conn = await await_until(
                asyncio.ensure_future(handoffs.get()), what="the second move to hand off"
            )
            # The earlier drain is cut by this hand-off, not by releasing its HTTP request.
            assert first_conn.closed_by_client
            assert await first.wait_closed() == 1000
            await async_wait_until(lambda: first_conn not in client._live)
            assert client._live == {second_conn, third_conn}
            assert client._draining is second_conn
            assert not second.closed.is_set()
            assert not second_conn.http_streams_idle.is_set()
            assert not crew.release.is_set()
            assert client.delays == []

            crew.release.set()
            response, body = await second.read_response(head["id"])
            assert response["status"] == 200 and body == b"finished"
            assert await second.wait_closed() == 1000
            await async_wait_until(lambda: not client._background)
            assert client._draining is None
            assert client._live == {third_conn}
        finally:
            crew.release.set()
            await stop(client, ingress)


async def test_reconnect_clamps_an_oversized_move_window() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            conn = _connection(client)
            await leg.send(valid_message("frames/reconnect", 1, closeInSeconds=3600))
            await await_until(conn.move_requested, what="the reconnect frame to be handled")
            assert conn.move_close_in == 30
        finally:
            await stop(client, ingress)


async def test_stop_during_a_move_leaves_no_connection_live() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        old = await relay.next_leg()
        await old.handshake()
        head = valid_message("frames/request", path="/slow", query="", headers={})
        await old.send(head)
        await old.send({"type": "request-end", "id": head["id"]})
        await async_wait_until(lambda: crew.seen)
        await old.send(valid_message("frames/reconnect", 1))
        new = await relay.next_leg()
        await new.handshake()
        # The old connection is draining in the background when the client stops.
        await async_wait_until(lambda: len(client._background) == 1)
        await stop(client, ingress)
        crew.release.set()
        assert client._live == set()
        assert await old.wait_closed() == 1001
        assert await new.wait_closed() == 1001


async def test_a_move_that_never_gets_ready_keeps_the_old_connection() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            old = await relay.next_leg()
            await old.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            await old.send({"type": "reconnect", "reason": "drain", "closeInSeconds": 0})
            new = await relay.next_leg()
            # No ready on the new one within the window: the client gives it up
            # and keeps the old one.
            assert await new.wait_closed() == 1000
            assert not old.closed.is_set()
            assert client.state == STATE_CONNECTED
            # The relay's 1012 at the end of the window then redials at once.
            await old.close(1012)
            third = await relay.next_leg()
            await third.handshake()
            await async_wait_until(
                lambda: client.state == STATE_CONNECTED and len(client._live) == 1
            )
            assert client.delays == []
        finally:
            await stop(client, ingress)


async def test_the_crew_url_from_the_token_source_wins() -> None:
    connected: list[str] = []

    async def on_connect(url: str) -> None:
        connected.append(url)

    async with FakeRelay() as relay, FakeCrew() as crew:
        lease_url = "https://k7qm3x2vtzr4hbn6cwapfe5dj3.kiro-relay.eu-central-1.on.aws"
        # A lease answer names the URL with a trailing slash; an origin has none.
        client, _, ingress = make_client(relay, crew, crew_url=lease_url + "/")
        client.register_callbacks(on_connect=on_connect, on_disconnect=None)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: connected)
            assert connected == [lease_url]
        finally:
            await stop(client, ingress)


async def test_stop_while_dialling_returns() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        leg = await relay.next_leg()
        await leg.recv_type("hello")
        # Stop before ready: the client closes the socket and the loop ends.
        await await_until(asyncio.ensure_future(client.stop()), what="stop")
        await ingress.aclose()
        assert await leg.wait_closed() == 1001
        assert client.state == "stopped"
        assert not client.running


async def test_empty_partial_frames_end_only_the_overflowing_stream(monkeypatch) -> None:
    from kiro_crew.relay_client import streams

    monkeypatch.setattr(streams, "WS_REASSEMBLY_MAX_PIECES", 4, raising=False)
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            upgrade = valid_message("frames/ws-upgrade")
            sid = upgrade["id"]
            await leg.send(upgrade)
            empty = {"type": "ws-frame", "id": sid, "data": ""}
            await leg.send(empty)
            assert await leg.recv_type("ws-frame") == empty
            for _ in range(5):
                await leg.send({**empty, "partial": True})
            # The pong is a positive processing fence, so the unfixed parent
            # fails on the missing error rather than waiting for a timeout.
            await leg.send({"type": "ping", "nonce": "after-pieces"})
            error = await leg.recv(skip_keepalive=False)
            assert error["type"] == "error"
            assert error["code"] == "message_too_large" and error["id"] == sid
            conn = _connection(client)
            await async_wait_until(lambda: sid not in conn._streams)
            assert conn._reassembler._parts == {}
            assert client.state == STATE_CONNECTED
        finally:
            await stop(client, ingress)


async def test_only_empty_request_chunks_reach_the_crew_as_an_empty_body(monkeypatch) -> None:
    queued = []
    original = HttpStream.feed

    def feed(stream, data):
        before = stream._body.qsize()
        original(stream, data)
        queued.append(stream._body.qsize() - before)

    monkeypatch.setattr(HttpStream, "feed", feed)
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message(
                "frames/request", 1, path="/upload", query="", headers={}, user=OWNER
            )
            await leg.send(head)
            for binary in (False, True):
                await leg.send(
                    {"type": "request-chunk", "id": head["id"], "data": "", "base64": binary}
                )
            await leg.send({"type": "request-end", "id": head["id"]})
            response, body = await leg.read_response(head["id"])
            assert response["status"] == 200
            assert json.loads(body) == {"bytes": 0, "head": ""}
            assert queued == [0, 0]
        finally:
            await stop(client, ingress)


@pytest.mark.parametrize("status", [302, 307])
async def test_websocket_redirect_is_refused_without_contacting_target(monkeypatch, status) -> None:
    from aiohttp import web
    from aiohttp.test_utils import TestServer

    target_requests = []

    async def target(request):
        target_requests.append(request.path)
        return web.Response(status=403)

    app = web.Application()
    app.router.add_get("/target", target)
    async with TestServer(app, host="127.0.0.1", port=0) as destination:
        crew = FakeCrew()

        async def redirect(request):
            crew._record(request)
            return web.Response(
                status=status, headers={"Location": str(destination.make_url("/target"))}
            )

        monkeypatch.setattr(crew, "_refused", redirect)
        async with FakeRelay() as relay, crew:
            client, _, ingress = make_client(relay, crew)
            await client.start()
            try:
                leg = await relay.next_leg()
                await leg.handshake()
                upgrade = valid_message("frames/ws-upgrade", path="/ws-refused", query="")
                await leg.send(upgrade)
                close = await leg.recv_type("ws-close")
                # The completed handshake is the positive fence for this absence.
                assert target_requests == []
                assert close["id"] == upgrade["id"]
                assert close["code"] == 4502
                assert str(status) in close["reason"]
                assert len(crew.seen) == 1
                assert crew.seen[0]["caller"].is_owner
            finally:
                await stop(client, ingress)


async def test_ten_mib_binary_message_is_delivered_to_the_crew(monkeypatch) -> None:
    from aiohttp import WSMsgType, web

    from kiro_crew.relay_client.protocol import WS_MESSAGE_MAX_BYTES, split_ws_data

    payload = b"x" * WS_MESSAGE_MAX_BYTES
    received = []
    crew = FakeCrew()

    async def receiver(request):
        crew._record(request)
        # aiohttp's receive ceiling is exclusive; the relay's 10 MiB limit is inclusive.
        ws = web.WebSocketResponse(max_msg_size=WS_MESSAGE_MAX_BYTES + 1)
        await ws.prepare(request)
        async for message in ws:
            assert message.type == WSMsgType.BINARY
            received.append(message.data)
            await ws.send_str("received")
        return ws

    monkeypatch.setattr(crew, "_ws", receiver)
    async with FakeRelay() as relay, crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            upgrade = valid_message("frames/ws-upgrade")
            await leg.send(upgrade)
            pieces = split_ws_data(_b64(payload))
            for index, piece in enumerate(pieces):
                await leg.send(
                    {
                        "type": "ws-frame",
                        "id": upgrade["id"],
                        "data": piece,
                        "binary": True,
                        **({"partial": True} if index < len(pieces) - 1 else {}),
                    }
                )
            echo = await leg.recv_type("ws-frame")
            assert echo["data"] == "received"
            assert received == [payload]
        finally:
            await stop(client, ingress)


async def test_body_bytes_past_the_content_length_never_reach_the_crew() -> None:
    """Bytes after the declared length could otherwise be read as a second request."""
    smuggled = b"GET /api/sessions HTTP/1.1\r\nHost: x\r\n\r\n"
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message(
                "frames/request",
                1,
                method="POST",
                path="/upload",
                query="",
                headers={"content-type": "text/plain", "content-length": "5"},
                user=OWNER,
            )
            await leg.send(head)
            for piece in (b"hello", smuggled):
                await leg.send(
                    valid_message("frames/request-chunk", id=head["id"], data=_b64(piece))
                )
            error = await leg.recv_type("error")
            assert (error["id"], error["code"]) == (head["id"], "invalid_message")
            # The connection serves the next stream normally once that one has ended.
            follow = valid_message("frames/request", id=2, user=OWNER)
            await leg.send(follow)
            await leg.send(valid_message("frames/request-end", id=follow["id"]))
            response, _ = await leg.read_response(follow["id"])
            assert response["status"] == 200
            paths = [seen["path"] for seen in crew.seen]
            assert paths.count("/api/sessions") == 1
            assert all(seen["caller"] is not None for seen in crew.seen)
        finally:
            await stop(client, ingress)


async def test_a_body_shorter_than_its_content_length_ends_the_stream() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            head = valid_message(
                "frames/request",
                1,
                method="POST",
                path="/upload",
                query="",
                headers={"content-length": "10"},
                user=OWNER,
            )
            await leg.send(head)
            await leg.send(valid_message("frames/request-chunk", id=head["id"], data=_b64(b"hi")))
            await leg.send(valid_message("frames/request-end", id=head["id"]))
            error = await leg.recv_type("error")
            assert (error["id"], error["code"]) == (head["id"], "invalid_message")
            conn = _connection(client)
            await async_wait_until(lambda: head["id"] not in conn._streams)
        finally:
            await stop(client, ingress)


@pytest.mark.parametrize("case", ["list-type", "nested-json", "handler"])
async def test_bad_frames_do_not_stop_the_live_connection(monkeypatch, caplog, case) -> None:
    import gc

    from kiro_crew.relay_client.connection import HostConnection

    async def broken_ping(self, frame):
        raise RuntimeError("handler failed")

    if case == "handler":
        monkeypatch.setattr(HostConnection, "_on_ping", broken_ping)
    text = {
        "list-type": '{"type":[]}',
        "nested-json": "[" * 20000 + "]" * 20000,
        "handler": '{"type":"ping"}',
    }[case]
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    try:
        async with FakeRelay() as relay, FakeCrew() as crew:
            client, _, ingress = make_client(relay, crew)
            await client.start()
            try:
                leg = await relay.next_leg()
                await leg.handshake()
                await async_wait_until(lambda: client.state == STATE_CONNECTED)
                conn = _connection(client)
                binding, url = crew.gateway.binding, client.public_url
                await leg.ws.send_str(text)
                # A stopped client is a positive failure signal, not a receive timeout.
                await async_wait_until(lambda: not leg.inbox.empty() or not client.running)
                assert client.running
                error = await leg.recv_type("error")
                expected = "unsupported_frame" if case == "list-type" else "invalid_message"
                assert error["code"] == expected
                assert "id" not in error
                if case == "handler":
                    assert error["message"] == "the relay client could not handle this frame"
                    assert any(
                        rec.name == "kiro_crew.relay_client.connection" and rec.exc_info
                        for rec in caplog.records
                    )
                head = valid_message("frames/request")
                await leg.send(head)
                await leg.send({"type": "request-end", "id": head["id"]})
                response, body = await leg.read_response(head["id"])
                assert response["status"] == 200
                assert json.loads(body)["ok"] is True
                assert client.state == STATE_CONNECTED
                assert _connection(client) is conn
                assert crew.gateway.binding is binding and client.public_url == url
                assert relay.dials == 1
                assert not leg.closed.is_set()
            finally:
                await stop(client, ingress)
    finally:
        if was_enabled:
            gc.enable()
        gc.collect()


@pytest.mark.parametrize("error_type", [asyncio.CancelledError, ConnectionError])
async def test_dispatch_preserves_cancellation_and_connection_errors(monkeypatch, error_type):
    from kiro_crew.relay_client.connection import HostConnection

    async def broken_ping(self, frame):
        raise error_type("propagate")

    monkeypatch.setattr(HostConnection, "_on_ping", broken_ping)
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.handshake()
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
            with pytest.raises(error_type, match="propagate"):
                await _connection(client)._dispatch('{"type":"ping"}')
        finally:
            await stop(client, ingress)
