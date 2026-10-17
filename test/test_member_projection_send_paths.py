"""Every path that sends a prompt confirms a member's saved spec first.

``AcpClient.stream_events`` and ``stream_command`` run ``ensure_ready``, which can
respawn the child and create a NEW session. Before this gate, only
``AcpProvider.stream`` confirmed that session, so a ``/compact`` or a slash command
arriving first sent a prompt into a session whose consumed spec no one had checked.
Each send path below must refuse before the client is asked to send anything.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
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
