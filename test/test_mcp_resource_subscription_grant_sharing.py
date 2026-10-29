"""A second connection of one session shares that session's resource grant.

The resource-subscription holder (``rfc-mcp-resource-subscriptions.md`` §4.4) is
a second gateway connection a session's subscriptions ride on, next to the
connection of the session's own kiro-cli stub. On a server that accepts caller
identity the gateway keeps one grant per caller, and three survivor checks keep
a grant alive while another stub holds it under an EQUAL ``CallerContext``.

These tests run a real gatewayd, a real stub process standing in for kiro-cli's
(claimed with its session token, as ``AcpClient`` claims it), and a second
connection opened from the test process -- which is what a connection the Kiro
Crew server opens for itself looks like to the daemon. They pin three things:

* that each route gives that second connection a grant caller equal to the
  kiro-cli stub's, shown by the survivor check that settles an unsubscribe
  locally only while another holder shares an equal caller;
* that the pid-indexed claim route reaches OTHER connections under the same
  process, so the holder must not be attributed that way; and
* that with an equal caller the existing survivor checks -- an unsubscribe, a
  claim rekey, a detach -- never release the grant the second connection holds.

Every check reads what the fake server recorded, what reached a connection, or
what the daemon answers on its socket, never ``Backend`` state.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from fake_pool_mcp_server import recorded
from mcp_resource_subscription_helpers import (
    INIT_PARAMS,
    RawConnection,
    StubProcess,
    register_payload_like_the_stub,
    running_daemon,
    spawn_stub,
    updated_for,
)

from kiro_crew import platform_compat as pc
from kiro_crew.mcp_gateway import transport
from kiro_crew.mcp_gateway.claim import build_claim_frame, mint_stub_session_token, send_claim
from kiro_crew.testing.wait import async_wait_until, finished

pytestmark = [
    pytest.mark.xdist_group("mcp_gateway"),
    pytest.mark.skipif(pc.IS_WINDOWS, reason="claims resolve the peer's ancestry via SO_PEERCRED"),
]

SESSION = "dashboard:held-session"
CHANNEL = "C_HELD"
URI = "crux://reviews/CR-1"


def holder_register(
    socket_path: Path, work_dir: Path, session_key: str = SESSION
) -> dict[str, Any]:
    """The Register of a holder attributed inline, the route Phase 2 builds.

    The caller block is the one the session's claim frame would carry, so the
    daemon builds the caller from the same four fields. No token and no
    ancestor pids: nothing pid-indexed can reach the connection afterwards.
    """
    payload = register_payload_like_the_stub(socket_path, work_dir)
    caller = build_claim_frame(os.getpid(), session_key, CHANNEL)["caller"]
    payload["caller"] = caller
    payload.update(
        session_key=caller["session_key"],
        session_type=caller["session_type"],
        principal_id=caller["principal_id"],
        channel_id=caller["channel_id"],
    )
    payload["ancestor_pids"] = []
    payload.pop("parent_pid", None)
    return payload


def claimed_register(socket_path: Path, work_dir: Path, token: str) -> dict[str, Any]:
    """The Register of a second connection attributed by a claim, the RFC's route."""
    payload = register_payload_like_the_stub(socket_path, work_dir)
    payload["stub_session_token"] = token
    return payload


def upstream(log: Path, verb: str) -> list[str]:
    return [line for line in recorded(log) if line.split(" ", 1)[0] == verb]


async def attached_stubs(socket_path: Path) -> list[int]:
    """The attached-stub count of each pooled backend, from the daemon's ``stats`` answer.

    The same frame the dashboard's metrics panel sends.
    """
    reader, writer = await transport.connect(socket_path)
    try:
        writer.write(b'{"type":"stats"}\n')
        await writer.drain()
        reply = json.loads(await finished(reader.readline(), "stats reply"))
    finally:
        writer.close()
        await finished(writer.wait_closed(), "stats connection close")
    assert reply.get("type") == "stats", reply
    return [row["stubs"] for row in reply["backends"]]


@pytest.fixture
def paths(tmp_path: Path, short_sock_dir: Path) -> dict[str, Path]:
    work = tmp_path / "ws"
    work.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    return {
        "sock": short_sock_dir / "gw.sock",
        "work": work,
        "home": home,
        "launches": tmp_path / "launches.txt",
        "subscribes": tmp_path / "subscribes.txt",
        "callers": tmp_path / "callers.txt",
    }


def _daemon(monkeypatch: pytest.MonkeyPatch, p: dict[str, Path]) -> Any:
    return running_daemon(
        monkeypatch, p["sock"], p["work"], p["launches"], p["subscribes"], p["callers"]
    )


async def _kiro_cli_stub(p: dict[str, Path], token: str) -> StubProcess:
    """A real stub claimed for SESSION the way a kiro-cli session's is.

    The claim names this process, which is the stub's parent, and binds the
    session token before the stub registers -- the normal ordering.
    """
    await send_claim(str(p["sock"]), os.getpid(), SESSION, CHANNEL, token)
    stub = StubProcess(await spawn_stub(p["sock"], p["work"], p["home"], session_token=token))
    try:
        reply = await stub.request(1, "initialize", INIT_PARAMS)
        assert "result" in reply, reply
        return stub
    except BaseException:
        await stub.close()
        raise


@pytest.mark.asyncio
async def test_both_routes_give_the_second_connection_the_stubs_grant_caller(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """Phase 0(b): no field of the grant caller differs, by either route.

    All three subscribes reach the server as SESSION. Equality of the whole
    caller shows in the survivor check: an unsubscribe settles locally, with
    nothing sent upstream, only while another holder's grant caller is equal.
    The inline holder lets go first, then the claimed one, which leaves only
    the kiro-cli stub to share with; the stub's own unsubscribe, as the last
    holder, is the positive control that a differing caller would have shown
    upstream.
    """
    async with _daemon(monkeypatch, paths):
        kiro = await _kiro_cli_stub(paths, mint_stub_session_token())
        inline = claimed = None
        try:
            assert "result" in await kiro.request(2, "resources/subscribe", {"uri": URI})

            inline = await RawConnection.register(
                paths["sock"], holder_register(paths["sock"], paths["work"])
            )
            await inline.request(1, "initialize", INIT_PARAMS)
            assert "result" in await inline.request(2, "resources/subscribe", {"uri": URI})

            token = mint_stub_session_token()
            await send_claim(str(paths["sock"]), os.getpid(), SESSION, CHANNEL, token)
            claimed = await RawConnection.register(
                paths["sock"], claimed_register(paths["sock"], paths["work"], token)
            )
            await claimed.request(1, "initialize", INIT_PARAMS)
            assert "result" in await claimed.request(2, "resources/subscribe", {"uri": URI})

            assert len(recorded(paths["launches"])) == 1, "the connections did not share a backend"
            assert upstream(paths["subscribes"], "subscribe") == [f"subscribe {URI} {SESSION}"] * 3

            assert "result" in await inline.request(3, "resources/unsubscribe", {"uri": URI})
            assert upstream(paths["subscribes"], "unsubscribe") == [], "inline caller differs"
            assert "result" in await claimed.request(3, "resources/unsubscribe", {"uri": URI})
            assert upstream(paths["subscribes"], "unsubscribe") == [], "claimed caller differs"

            assert "result" in await kiro.request(3, "resources/unsubscribe", {"uri": URI})
            assert upstream(paths["subscribes"], "unsubscribe") == [f"unsubscribe {URI} {SESSION}"]
        finally:
            for conn in (inline, claimed):
                if conn is not None:
                    await conn.close()
            await kiro.close()


@pytest.mark.asyncio
async def test_a_pid_claim_for_a_second_connection_retargets_another_sessions_stub(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """Phase 0(b): why the holder is attributed inline and not by a claim.

    This test records a hazard, not a contract. A claim is applied to every
    connection indexed under the pid it names, and a connection the Kiro Crew
    server opens for itself can only be named by the server's own pid -- the
    pid every kiro-cli stub it spawned is indexed under too. A tokened claim
    spares a stub carrying a different token, but not a tokenless one, so
    another session's tokenless stub is retargeted to the claiming session.
    The inline route touches nothing, which is why the holder is attributed
    inline.

    The last assertion pins that retarget as the daemon does it today, an open
    cross-session misattribution. A change to ``_apply_claim`` that stops the
    retarget should update this test.
    """
    other = "dashboard:another-session"
    async with _daemon(monkeypatch, paths):
        stranger = StubProcess(
            await spawn_stub(paths["sock"], paths["work"], paths["home"], session_key=other)
        )
        inline = claimed = None
        try:
            assert "result" in await stranger.request(1, "initialize", INIT_PARAMS)

            async def callers_after_a_call(req_id: int) -> list[str]:
                assert "result" in await stranger.request(req_id, "tools/call", {"name": "noop"})
                return recorded(paths["callers"])

            assert await callers_after_a_call(2) == [other]

            inline = await RawConnection.register(
                paths["sock"], holder_register(paths["sock"], paths["work"])
            )
            assert await callers_after_a_call(3) == [other, other]

            token = mint_stub_session_token()
            await send_claim(str(paths["sock"]), os.getpid(), SESSION, CHANNEL, token)
            claimed = await RawConnection.register(
                paths["sock"], claimed_register(paths["sock"], paths["work"], token)
            )
            assert await callers_after_a_call(4) == [other, other, SESSION]
        finally:
            for conn in (inline, claimed):
                if conn is not None:
                    await conn.close()
            await stranger.close()


@pytest.mark.asyncio
async def test_the_kiro_cli_stub_leaving_never_releases_the_shared_grant(
    monkeypatch: pytest.MonkeyPatch, paths: dict[str, Path]
) -> None:
    """Phase 1: unsubscribe, claim rekey and detach all leave the grant held.

    Each step waits on something observable from outside the backend: the
    unsubscribe's reply, the claim's acknowledgement (the daemon answers a
    claim only after the rekey's eviction has run) and the next call recorded
    as the new session, and the daemon's ``stats`` answer showing one stub
    left attached. A scripted update must then still reach the second
    connection, and the fake server's log must show no upstream unsubscribe.
    The second connection's own unsubscribe is the positive control that the
    log would have shown one.
    """
    async with _daemon(monkeypatch, paths):
        token = mint_stub_session_token()
        kiro = await _kiro_cli_stub(paths, token)
        holder = None
        try:
            holder = await RawConnection.register(
                paths["sock"], holder_register(paths["sock"], paths["work"])
            )
            await holder.request(1, "initialize", INIT_PARAMS)
            assert "result" in await kiro.request(2, "resources/subscribe", {"uri": URI})
            assert "result" in await holder.request(2, "resources/subscribe", {"uri": URI})

            async def update_reaches_holder(via: Any, req_id: int) -> None:
                start = len(holder.messages)
                await via.request(
                    req_id, "tools/call", {"name": "emit_update", "arguments": {"uri": URI}}
                )
                await holder.wait_for(updated_for(URI), start=start)

            # _handle_resource_subscription: the kiro-cli stub unsubscribes.
            assert "result" in await kiro.request(3, "resources/unsubscribe", {"uri": URI})
            await update_reaches_holder(kiro, 4)
            assert upstream(paths["subscribes"], "unsubscribe") == []

            # evict_stub_subscriptions: a warm-pool rekey hands the runtime on.
            assert "result" in await kiro.request(5, "resources/subscribe", {"uri": URI})
            next_session = "dashboard:next-session"
            assert await send_claim(
                str(paths["sock"]), os.getpid(), next_session, CHANNEL, token
            ), "the rekey retargeted no connection"
            assert "result" in await kiro.request(6, "tools/call", {"name": "noop"})
            assert recorded(paths["callers"]) == [next_session]
            await update_reaches_holder(kiro, 7)
            assert upstream(paths["subscribes"], "unsubscribe") == []

            # detach_stub: the kiro-cli stub goes away, subscribed again first.
            assert await send_claim(str(paths["sock"]), os.getpid(), SESSION, CHANNEL, token)
            assert "result" in await kiro.request(8, "resources/subscribe", {"uri": URI})
            assert await attached_stubs(paths["sock"]) == [2]
            await kiro.close()

            async def only_the_holder_attached() -> bool:
                return await attached_stubs(paths["sock"]) == [1]

            await async_wait_until(
                only_the_holder_attached, describe=lambda: "the kiro-cli stub never detached"
            )
            await update_reaches_holder(holder, 9)
            assert upstream(paths["subscribes"], "unsubscribe") == []

            # Positive control: the last holder of the grant releases it.
            assert "result" in await holder.request(10, "resources/unsubscribe", {"uri": URI})
            assert upstream(paths["subscribes"], "unsubscribe") == [f"unsubscribe {URI} {SESSION}"]
        finally:
            if holder is not None:
                await holder.close()
            await kiro.close()
