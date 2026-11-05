"""``AcpProvider.new_conversation`` delegates the cheap reset, or says it has none.

Only ``AcpSessionProvider`` resets a conversation on its live process. A backend on its
own ``AcpClient`` (claude among them) has no such reset, and the provider says so with a
``NotImplementedError`` naming the backend rather than an ``AttributeError``, so the
callers' kill-and-respawn fallback reads as the designed path it is.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE
from kiro_crew.providers.acp import AcpProvider


def _provider_over(client: object) -> AcpProvider:
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client  # type: ignore[assignment]
    return provider


@pytest.mark.asyncio
async def test_a_claude_client_has_no_reset_and_names_its_backend() -> None:
    provider = _provider_over(AcpClient(acp_backend=ACP_BACKEND_CLAUDE))

    with pytest.raises(NotImplementedError, match="'claude' backend has no fresh-conversation"):
        await provider.new_conversation()


@pytest.mark.asyncio
async def test_a_client_with_a_reset_is_delegated_to() -> None:
    client = MagicMock(spec=["new_conversation", "backend"])
    client.new_conversation = AsyncMock()
    provider = _provider_over(client)

    await provider.new_conversation()

    client.new_conversation.assert_awaited_once_with()
