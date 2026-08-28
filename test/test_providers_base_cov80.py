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

import pytest

from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK
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


def test_rate_limit_defaults_to_no_quota(provider: _MinimalProvider) -> None:
    # H8: the capability lands on the ABC with a safe default rather than as a
    # hasattr probe at the call site, so a harness that reports no plan quota —
    # which is most of them — answers None without implementing anything.
    assert provider.rate_limit_payload() is None


def test_identity_defaults_are_empty_not_wildcards(provider: _MinimalProvider) -> None:
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


def test_touch_activity_and_runtime_info_defaults(provider: _MinimalProvider) -> None:
    assert provider.touch_activity() is None
    assert provider.runtime_info() == (None, None)


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
