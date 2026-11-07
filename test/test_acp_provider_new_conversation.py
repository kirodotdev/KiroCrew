"""``AcpProvider.new_conversation`` delegates the cheap reset, or says it has none.

``AcpSessionProvider`` resets a conversation on its live process, and so does an
``AcpClient`` in ``ACP_BACKENDS_CLIENT_NEW_CONVERSATION`` (claude). Any other backend on
its own ``AcpClient`` has no such reset, and says so with a ``NotImplementedError``
naming the backend rather than an ``AttributeError``, so the callers' kill-and-respawn
fallback reads as the designed path it is.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_CLAUDE, ACP_BACKEND_OPENCODE
from kiro_crew.providers.acp import AcpProvider


def _provider_over(client: object) -> AcpProvider:
    provider = AcpProvider.__new__(AcpProvider)
    provider._client = client  # type: ignore[assignment]
    return provider


@pytest.mark.asyncio
async def test_a_client_outside_the_set_has_no_reset_and_names_its_backend() -> None:
    provider = _provider_over(AcpClient(acp_backend=ACP_BACKEND_OPENCODE))

    with pytest.raises(NotImplementedError, match="'opencode' backend has no fresh-conversation"):
        await provider.new_conversation()


@pytest.mark.asyncio
async def test_a_client_with_a_reset_is_delegated_to() -> None:
    client = MagicMock(spec=["new_conversation", "backend"])
    client.new_conversation = AsyncMock()
    provider = _provider_over(client)

    await provider.new_conversation()

    client.new_conversation.assert_awaited_once_with()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_mode", "left_on", "deleted"),
    [
        pytest.param("persistent", "sess-2", [], id="persistent-kept"),
        pytest.param("incognito", "sess-2", ["sess-1"], id="disposable-reset"),
        pytest.param("incognito", None, ["sess-1"], id="disposable-failed-reset"),
        pytest.param("incognito", "sess-1", [], id="disposable-refused-before-reset"),
    ],
)
async def test_a_claude_reset_deletes_a_disposable_transcript_once_it_left_it(
    tmp_path, memory_mode, left_on, deleted
) -> None:
    """A disposable session's replaced transcript goes, as ``shutdown`` would delete it.

    A failed reset ends the client's process and clears its id, so nothing else would
    name the old session; one refused before it touched the session leaves the id for
    the caller's own ``shutdown``. The effort ``start`` applies follows a reset only.
    """
    client = AcpClient(acp_backend=ACP_BACKEND_CLAUDE, work_dir=tmp_path)
    client._session_id = "sess-1"

    async def reset() -> None:
        client._session_id = left_on
        if left_on != "sess-2":
            raise AcpError("reset failed")

    client.new_conversation = reset  # type: ignore[method-assign]
    provider = _provider_over(client)
    provider.memory_mode = memory_mode
    provider._apply_initial_effort = AsyncMock()  # type: ignore[method-assign]
    removed: list[str] = []
    provider.cleanup_session = AsyncMock(side_effect=removed.append)  # type: ignore[method-assign]

    if left_on == "sess-2":
        await provider.new_conversation()
        provider._apply_initial_effort.assert_awaited_once_with()
    else:
        with pytest.raises(AcpError):
            await provider.new_conversation()
        provider._apply_initial_effort.assert_not_awaited()
    assert removed == deleted
