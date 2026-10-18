"""Every path that sends a prompt confirms a member's saved spec first.

``AcpClient.stream_events`` and ``stream_command`` run ``ensure_ready``, which can
respawn the child and create a NEW session. Before this gate, only
``AcpProvider.stream`` confirmed that session, so a ``/compact`` or a slash command
arriving first sent a prompt into a session whose consumed spec no one had checked.
Each send path below must refuse before the client is asked to send anything.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_CLAUDE,
    ACP_BACKEND_DEEPSEEK,
    ACP_BACKEND_KIRO,
)
from kiro_crew.providers.acp import AcpProvider


def _provider(calls: list[str], *, confirms: bool) -> AcpProvider:
    provider = AcpProvider.__new__(AcpProvider)
    client = MagicMock(spec=AcpClient)
    client.backend = ACP_BACKEND_CLAUDE

    async def ensure_ready():
        calls.append("ensure_ready")

    async def confirm():
        calls.append("confirm")
        if not confirms:
            raise AcpError(
                "capability_runtime_unverified: saved spec projection could not be confirmed"
            )

    async def send(*_args, **_kwargs):
        calls.append("send")
        if False:  # pragma: no cover -- makes this an async generator
            yield None

    client.ensure_ready = AsyncMock(side_effect=ensure_ready)
    client.confirm_member_projection = AsyncMock(side_effect=confirm)
    client.stream_events = send
    client.stream_command = send
    provider._client = client
    delivery = MagicMock()

    async def deliver(message, sender, _incarnation):
        async for event in sender(message):
            yield event

    delivery.stream = deliver
    provider.essential_delivery = delivery
    provider._compact_result = None
    return provider


async def _drain(gen) -> None:
    async for _ in gen:
        pass


async def _stream(provider: AcpProvider) -> None:
    await _drain(provider.stream("hello"))


async def _stream_command(provider: AcpProvider) -> None:
    await _drain(provider.stream_command("/help"))


async def _compact(provider: AcpProvider) -> None:
    await provider.compact()


SEND_PATHS = [
    pytest.param(_stream, id="stream"),
    pytest.param(_stream_command, id="stream_command"),
    pytest.param(_compact, id="compact"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SEND_PATHS)
async def test_an_unconfirmed_spec_sends_no_prompt(path):
    calls: list[str] = []
    provider = _provider(calls, confirms=False)
    with pytest.raises(AcpError, match="capability_runtime_unverified"):
        await path(provider)
    assert "send" not in calls
    assert calls == ["ensure_ready", "confirm"]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", SEND_PATHS)
async def test_a_confirmed_spec_is_checked_before_the_prompt(path):
    calls: list[str] = []
    provider = _provider(calls, confirms=True)
    await path(provider)
    assert calls.index("confirm") < calls.index("send")


def test_every_send_path_is_covered():
    """A new provider method that calls the client's send must join SEND_PATHS."""
    import ast
    import inspect

    from kiro_crew.providers import acp_turns

    tree = ast.parse(inspect.getsource(acp_turns))
    senders = set()
    for fn in tree.body:
        if not isinstance(fn, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        for node in ast.walk(fn):
            if (
                isinstance(node, ast.Attribute)
                and node.attr in {"stream_events", "stream_command"}
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "_client"
            ):
                senders.add(fn.name)
    assert senders == {p.id for p in SEND_PATHS}


def _starting_client(
    tmp_path, monkeypatch, backend: str, *, refusal: BaseException | None
) -> tuple[AcpClient, list[str]]:
    """A member client with no live child, so ensure_ready runs the real start."""
    calls: list[str] = []
    sent: list[str] = []
    client = AcpClient(work_dir=tmp_path, agent="saved-member", acp_backend=backend)
    client.member_context = True

    async def spawn():
        calls.append("spawn")
        proc = MagicMock(returncode=None)
        proc.stdin.drain = AsyncMock()
        client._process = proc

    async def send(method, _params):
        sent.append(method)
        return len(sent)

    async def wait(req_id, *_args, **_kwargs):
        if sent[req_id - 1] == "session/new":
            return {"sessionId": f"s{calls.count('spawn')}"}
        return {"protocolVersion": 1, "agentCapabilities": {}}

    async def confirm():
        calls.append("confirm")
        if refusal is not None:
            raise refusal

    async def cleanup():
        calls.append("teardown")

    monkeypatch.setattr(client, "_spawn", spawn)
    monkeypatch.setattr(client, "_send_request", send)
    monkeypatch.setattr(client, "_wait_for_response", wait)
    monkeypatch.setattr(client, "_drain_notifications", AsyncMock())
    monkeypatch.setattr(client, "_snapshot_process_tree", AsyncMock())
    monkeypatch.setattr(client, "confirm_member_projection", confirm)
    monkeypatch.setattr(client, "_cleanup_failed_live_spawn", cleanup)
    return client, calls


@pytest.mark.asyncio
async def test_a_respawn_inside_the_client_confirms_before_it_returns(tmp_path, monkeypatch):
    """``stream_events`` runs ``ensure_ready`` again after the provider's gate, and a
    child that died in between is respawned there. The new session is confirmed
    inside the start itself, so no send can follow an unconfirmed respawn."""
    client, calls = _starting_client(tmp_path, monkeypatch, ACP_BACKEND_DEEPSEEK, refusal=None)
    await client.ensure_ready()
    assert calls == ["spawn", "confirm"]
    assert client._session_id == "s1"


@pytest.mark.asyncio
async def test_a_refused_respawn_is_torn_down_not_left_warm(tmp_path, monkeypatch):
    """A refusal ends the child, so the next ``ensure_ready`` judges a fresh session
    instead of taking the warm return into the refused one."""
    refusal = AcpError(
        "capability_runtime_unverified: saved spec projection could not be confirmed"
    )
    client, calls = _starting_client(tmp_path, monkeypatch, ACP_BACKEND_DEEPSEEK, refusal=refusal)
    with pytest.raises(AcpError, match="capability_runtime_unverified"):
        await client.ensure_ready()
    assert calls == ["spawn", "confirm", "teardown"] * 2
    assert client._session_id is None
    with pytest.raises(AcpError, match="capability_runtime_unverified"):
        await client.ensure_ready()
    assert calls.count("confirm") == 4


@pytest.mark.asyncio
async def test_a_cancel_during_confirmation_is_torn_down_not_left_warm(tmp_path, monkeypatch):
    """A cancel skips ensure_ready's error handler, so the start tears down itself."""
    client, calls = _starting_client(
        tmp_path, monkeypatch, ACP_BACKEND_DEEPSEEK, refusal=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await client.ensure_ready()
    assert calls == ["spawn", "confirm", "teardown"]
    assert client._session_id is None


@pytest.mark.asyncio
async def test_a_kiro_start_gains_no_confirmation_call(tmp_path, monkeypatch):
    """harness-parity H13: the kiro construction path gains no call in service of
    an adapter, member or not."""
    client, calls = _starting_client(tmp_path, monkeypatch, ACP_BACKEND_KIRO, refusal=None)
    await client.ensure_ready()
    assert calls == ["spawn"]
    assert client._session_id == "s1"
