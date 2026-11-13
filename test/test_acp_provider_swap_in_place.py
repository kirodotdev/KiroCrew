"""``AcpProvider`` on the shared runtime: the in-place conversation swap.

``swaps_conversation_in_place`` says when ``SessionLifecycleService.swap_conversation``
may start the fresh conversation on the live process, and ``new_conversation`` then
carries the chat's Bot mount and channel onto it, drops the delivered essentials and
re-applies the effort, as a cold start would.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_KAS, ACP_BACKEND_KIRO
from kiro_crew.providers.acp import AcpProvider


def _provider(*, backend: str = ACP_BACKEND_KAS) -> AcpProvider:
    client = MagicMock(spec=AcpSessionProvider)
    client.backend = backend
    client.new_conversation = AsyncMock()
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client  # type: ignore[assignment]
    provider._owning_session_key = lambda: "dashboard:chat-1"  # type: ignore[method-assign]
    return provider


@pytest.mark.parametrize("backend", [ACP_BACKEND_KAS, ACP_BACKEND_KIRO])
def test_a_live_shared_runtime_swaps(backend, monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.session_environment.remote_workspace_for", lambda _k: None)
    assert _provider(backend=backend).swaps_conversation_in_place is True


def test_a_backend_without_session_eviction_does_not_swap(monkeypatch) -> None:
    monkeypatch.setattr("kiro_crew.session_environment.remote_workspace_for", lambda _k: None)
    assert _provider(backend=ACP_BACKEND_CLAUDE).swaps_conversation_in_place is False


def test_a_remote_workspace_does_not_swap(monkeypatch) -> None:
    monkeypatch.setattr(
        "kiro_crew.session_environment.remote_workspace_for", lambda _k: ("env", "/work")
    )
    assert _provider().swaps_conversation_in_place is False


def test_a_per_session_client_does_not_swap() -> None:
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = MagicMock(spec=["backend"])  # type: ignore[assignment]
    assert provider.swaps_conversation_in_place is False


@pytest.mark.asyncio
async def test_a_bot_chat_swap_keeps_its_mount_channel_and_effort() -> None:
    provider = _provider()
    provider._member_session_key = lambda: "member:bot-1"  # type: ignore[method-assign]
    provider._owning_channel_id = lambda: "C123"  # type: ignore[method-assign]
    provider.essential_delivery = MagicMock()
    provider._apply_initial_effort = AsyncMock()  # type: ignore[method-assign]

    await provider.new_conversation()

    provider._client.new_conversation.assert_awaited_once_with(  # type: ignore[attr-defined]
        member_session_key="member:bot-1", channel_id="C123"
    )
    provider.essential_delivery.invalidate.assert_called_once_with()
    provider._apply_initial_effort.assert_awaited_once_with()
