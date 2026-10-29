"""The host dial never takes credentials from the environment.

The dial carries the host token, so netrc must never be consulted for it. A proxy
named in the environment is still honoured, resolved per dial and passed explicitly.
"""

from __future__ import annotations

import os
from unittest.mock import MagicMock

import pytest
from relay_client_helpers import (
    OWNER,
    TOKEN,
    FakeCrew,
    FakeRelay,
    make_client,
    ready_frame,
    stop,
)

from kiro_crew.relay_client.client import STATE_CONNECTED
from kiro_crew.relay_client.connection import HostConnection, RelayTimings, env_proxy_for
from kiro_crew.relay_client.token_source import HostCredential
from kiro_crew.testing.wait import async_wait_until

_PROXY_VARS = ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY")


def _clear_proxy_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
        monkeypatch.delenv(name.lower(), raising=False)
    # The stdlib ignores HTTP_PROXY when this is set (a CGI guard); keep it unset.
    monkeypatch.delenv("REQUEST_METHOD", raising=False)


@pytest.mark.asyncio
async def test_a_netrc_default_entry_never_replaces_the_host_token(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    netrc = tmp_path / ".netrc"
    netrc.write_text(
        "machine 127.0.0.1 login other password secret2\n" "default login someone password secret\n"
    )
    os.chmod(netrc, 0o600)
    monkeypatch.setenv("NETRC", str(netrc))
    monkeypatch.setenv("HOME", str(tmp_path))
    _clear_proxy_env(monkeypatch)

    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        await client.start()
        try:
            leg = await relay.next_leg()
            await leg.recv_type("hello")
            assert leg.token == TOKEN
            assert relay.refused == []
            await leg.send(ready_frame())
            await async_wait_until(lambda: client.state == STATE_CONNECTED)
        finally:
            await stop(client, ingress)


@pytest.mark.asyncio
async def test_the_host_dial_session_does_not_trust_the_environment() -> None:
    async with FakeRelay() as relay, FakeCrew() as crew:
        client, _, ingress = make_client(relay, crew)
        session = client._client_session()
        try:
            trust = getattr(session, "trust_env", None)
            if trust is None:
                trust = session._trust_env
            assert trust is False
        finally:
            await session.close()
            await stop(client, ingress)


@pytest.mark.asyncio
async def test_a_value_error_from_the_dial_reads_as_a_failed_dial() -> None:
    session = MagicMock()

    def refuse(*args, **kwargs):
        raise ValueError("bad proxy URL")

    session.ws_connect = refuse
    conn = HostConnection(
        session=session,
        url="ws://127.0.0.1:9/v1/host",
        credential=HostCredential(TOKEN, crew_kind="personal", owner_user_id=OWNER),
        ingress=MagicMock(),
        binding=("personal", OWNER),
        client_version="0.0.0-test",
        timings=RelayTimings(),
    )
    end = await conn.run()
    assert "dial failed" in end.detail
    assert "ValueError" in end.detail


def test_https_proxy_is_used_for_a_wss_relay(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://other.example:8080")
    assert env_proxy_for("wss://relay.example/v1/host") == "http://proxy.example:3128"


def test_http_proxy_is_used_for_a_ws_relay_off_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    assert env_proxy_for("ws://relay.example/v1/host") == "http://proxy.example:8080"


def test_no_proxy_naming_the_host_dials_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "relay.example")
    assert env_proxy_for("wss://relay.example/v1/host") is None


@pytest.mark.parametrize(
    "url", ["ws://127.0.0.1:9/v1/host", "ws://[::1]:9/v1/host", "ws://localhost:9/v1/host"]
)
def test_a_loopback_relay_is_never_proxied(monkeypatch: pytest.MonkeyPatch, url: str) -> None:
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:8080")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://proxy.example:1080")
    assert env_proxy_for(url) is None


def test_no_proxy_environment_dials_directly(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_proxy_env(monkeypatch)
    assert env_proxy_for("wss://relay.example/v1/host") is None
