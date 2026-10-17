"""Coverage for ``LLMProvider``'s default (non-abstract) behaviour.

Every default here is a contract the gateway relies on when a backend does NOT
override it: "unknown" readings must be conservative (0 tokens, empty session
id, empty served model), ``cancel`` must claim nothing was cancelled, and
``stream_command`` must degrade to ``stream`` rather than dropping the command.
A provider that only implements the abstract surface is exactly what these
assert.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from types import SimpleNamespace

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
from kiro_crew.mcp_gateway.abort import RuntimeAbortTarget
from kiro_crew.providers.base import LLMEvent, LLMProvider


class _MinimalProvider(LLMProvider):
    """Implements ONLY the abstract surface — every other method is the default."""

    def __init__(self) -> None:
        self.started = False
        self.shutdown_called = False
        self.approvals: list[tuple[str | int, bool]] = []
        self.rejections: list[str | int] = []
        self.streamed: list[str] = []

    async def start(self) -> None:
        self.started = True

    async def shutdown(self) -> None:
        self.shutdown_called = True

    async def stream(self, message: str) -> AsyncIterator[LLMEvent]:
        self.streamed.append(message)
        yield LLMEvent(kind=EVENT_TEXT_CHUNK, text=message)
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id: str | int, *, always: bool = False) -> None:
        self.approvals.append((request_id, always))

    async def reject_tool(self, request_id: str | int) -> None:
        self.rejections.append(request_id)

    def context_usage_pct(self) -> float:
        return 12.5


@pytest.fixture
def provider() -> _MinimalProvider:
    return _MinimalProvider()


def test_abstract_surface_cannot_be_instantiated() -> None:
    with pytest.raises(TypeError):
        LLMProvider()  # type: ignore[abstract]


def test_unknown_context_readings_are_conservative(provider: _MinimalProvider) -> None:
    assert provider.context_usage_pct() == 12.5
    assert provider.context_usage_unknown() is False
    assert provider.context_window_tokens() == 0
    assert provider.context_used_tokens() == 0


def test_identity_defaults_are_empty_not_wildcards(provider: _MinimalProvider) -> None:
    assert provider.defer_replay_sid_promotion is False
    assert provider.is_kiro_backend is False
    assert provider.session_id == ""
    assert provider.cwd == ""
    assert provider.served_model == ""
    assert provider.exit_code is None


def test_liveness_defaults_and_process_delegation(provider: _MinimalProvider) -> None:
    assert provider.is_alive() is True
    assert provider.is_process_alive() is True

    class _Dead(_MinimalProvider):
        def is_alive(self) -> bool:
            return False

    assert _Dead().is_process_alive() is False


def test_touch_activity_default(provider: _MinimalProvider) -> None:
    assert provider.touch_activity() is None


def test_a_provider_with_no_runtime_names_no_abort_target(provider: _MinimalProvider) -> None:
    """No runtime of its own means no abort push, and no address to invent."""
    assert provider.runtime_abort_target() is None


def test_the_private_client_walk_is_the_undeclared_providers_fallback() -> None:
    """A provider outside this tree keeps its abort push without declaring one.

    The walk lives here so no consumer reaches through provider internals for a
    pid of its own; a provider in this tree overrides the method instead.
    """
    provider = _MinimalProvider()
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        _pid=99, _mcp_gateway_socket="/tmp/b.sock"
    )
    assert provider.runtime_abort_target() == RuntimeAbortTarget.build(99, "/tmp/b.sock")


@pytest.mark.parametrize(
    "pid, socket_path",
    [
        (1, "/tmp/b.sock"),  # init, never a runtime this gateway spawned
        (0, "/tmp/b.sock"),  # addresses a process group, not a process
        (-5, "/tmp/b.sock"),
        (True, "/tmp/b.sock"),  # a bool is an int and must not pass as pid 1
        ("99", "/tmp/b.sock"),
        (99, ""),  # no gateway socket, so nowhere to send the frame
        (99, None),
    ],
)
def test_an_unroutable_address_yields_no_target(pid: object, socket_path: object) -> None:
    """Refused at mint time, so holding a target IS the answer to "can I abort"."""
    provider = _MinimalProvider()
    provider._client = SimpleNamespace(  # type: ignore[attr-defined]
        _pid=pid, _mcp_gateway_socket=socket_path
    )
    assert provider.runtime_abort_target() is None


def test_a_target_names_the_runtime_it_addresses() -> None:
    """The audit label is the target's own rendering, not the caller's."""
    target = RuntimeAbortTarget.build(4242, "/tmp/gw.sock")
    assert target is not None
    assert target.audit_label == "pid=4242"


@pytest.mark.asyncio
async def test_lifecycle_and_permission_passthrough(provider: _MinimalProvider) -> None:
    await provider.start()
    await provider.approve_tool("req-1", always=True)
    await provider.reject_tool("req-2")
    await provider.shutdown()
    assert provider.started is True
    assert provider.shutdown_called is True
    assert provider.approvals == [("req-1", True)]
    assert provider.rejections == ["req-2"]


@pytest.mark.asyncio
async def test_stream_command_falls_back_to_stream(provider: _MinimalProvider) -> None:
    kinds = [e.kind async for e in provider.stream_command("/usage")]
    assert kinds == [EVENT_TEXT_CHUNK, EVENT_COMPLETE]
    assert provider.streamed == ["/usage"]


@pytest.mark.asyncio
async def test_cleanup_session_and_compact_are_noops(provider: _MinimalProvider) -> None:
    assert await provider.cleanup_session("sess-1") is None
    assert await provider.compact("some context") is None


@pytest.mark.asyncio
async def test_wait_for_compaction_defaults_to_timeout(provider: _MinimalProvider) -> None:
    assert await provider.wait_for_compaction(timeout=0.01) == {"type": "timeout"}


@pytest.mark.asyncio
async def test_cancel_defaults_to_no_turn(provider: _MinimalProvider) -> None:
    assert await provider.cancel(wait_ack_timeout=0.01) == "no_turn"
