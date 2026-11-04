"""A client named by ``continue_only`` never opens a new session in ``ensure_ready``.

A cron sub-agent's completion is prompted into its parent's live session. On a direct
client, ``ensure_ready`` would answer a process that exited after the claim by
spawning a replacement and opening a new session there, so the completion would
land in a conversation with none of the orchestration. Within ``continue_only`` that
cold start is refused for the named client only; the warm path, a replacement that
restores the client's own session, and every other client are unaffected.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient, AcpProcessDied
from kiro_crew.acp.continue_only import continue_only, refuses_start


class _Started(Exception):
    """Raised by the stubbed spawn: the cold start reached it."""


def _client(tmp_path, *, alive: bool) -> AcpClient:
    client = AcpClient(work_dir=tmp_path / "ws")
    client._work_dir_ready = True
    if alive:
        client._process = MagicMock()
        client._process.returncode = None
        client._session_id = "sess-live"
    client._spawn = AsyncMock(side_effect=_Started)  # type: ignore[method-assign]
    return client


@pytest.mark.asyncio
async def test_a_named_client_with_no_session_refuses_the_cold_start(tmp_path) -> None:
    client = _client(tmp_path, alive=False)

    with continue_only(client):
        with pytest.raises(AcpProcessDied):
            await client.ensure_ready()

    client._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_named_client_whose_process_exited_refuses_the_cold_start(tmp_path) -> None:
    client = _client(tmp_path, alive=True)
    client._process.returncode = 1  # the child exited after the claim

    with continue_only(client):
        with pytest.raises(AcpProcessDied):
            await client.ensure_ready()

    client._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_provider_named_for_its_client_refuses_it_too(tmp_path) -> None:
    client = _client(tmp_path, alive=False)

    with continue_only(SimpleNamespace(_client=client)):
        assert refuses_start(client)
        with pytest.raises(AcpProcessDied):
            await client.ensure_ready()

    client._spawn.assert_not_awaited()
    assert not refuses_start(client), "the refusal outlived its block"


@pytest.mark.asyncio
async def test_a_live_session_is_prompted_as_usual(tmp_path) -> None:
    client = _client(tmp_path, alive=True)

    with continue_only(client):
        await client.ensure_ready()

    client._spawn.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_replacement_that_restores_the_clients_own_session_is_allowed(
    tmp_path,
) -> None:
    """``relaunch_and_reload`` sets the session as the resume id and refuses a load that
    opened a different session, so its replacement continues the same conversation."""
    client = _client(tmp_path, alive=False)
    client._resume_session_id = "sess-own"

    with continue_only(client):
        with pytest.raises(_Started):
            await client.ensure_ready()


@pytest.mark.asyncio
async def test_another_client_starts_as_usual_inside_the_block(tmp_path) -> None:
    named, other = _client(tmp_path, alive=True), _client(tmp_path, alive=False)

    with continue_only(named):
        with pytest.raises(_Started):
            await other.ensure_ready()


@pytest.mark.asyncio
async def test_a_restore_that_falls_back_to_session_new_is_refused(tmp_path) -> None:
    """A resume id is set (``settle_disposed_error`` sets one too), but the session
    cannot be restored: kiro-cli has no transcript for it, so the load is skipped and
    ``_initialize_session`` would fall back to ``session/new``. That new session is
    refused; it is never requested."""
    from kiro_crew.acp.continue_only import SessionStartRefused
    from kiro_crew.acp.types import METHOD_SESSION_NEW

    client = _client(tmp_path, alive=False)
    client._resume_session_id = "sess-without-a-transcript"
    sent: list[str] = []

    async def send_request(method: str, params: Any = None) -> int:
        sent.append(method)
        return len(sent)

    client._send_request = send_request  # type: ignore[method-assign]
    client._wait_for_response = AsyncMock(  # type: ignore[method-assign]
        return_value={"agentCapabilities": {"loadSession": True}}
    )

    with continue_only(client):
        with pytest.raises(SessionStartRefused):
            await client._initialize_session()

    assert METHOD_SESSION_NEW not in sent


@pytest.mark.asyncio
async def test_a_refused_initialization_ends_its_child_and_is_not_retried(tmp_path) -> None:
    from kiro_crew.acp.continue_only import SessionStartRefused

    client = _client(tmp_path, alive=False)
    client._resume_session_id = "sess-own"

    async def spawn() -> None:
        client._process = MagicMock()
        client._process.returncode = None

    client._spawn = AsyncMock(side_effect=spawn)  # type: ignore[method-assign]
    client._initialize_session = AsyncMock(  # type: ignore[method-assign]
        side_effect=SessionStartRefused("not restored")
    )
    client._cleanup_failed_live_spawn = AsyncMock()  # type: ignore[method-assign]

    with continue_only(client):
        with pytest.raises(SessionStartRefused):
            await client.ensure_ready()

    client._spawn.assert_awaited_once()
    client._cleanup_failed_live_spawn.assert_awaited_once()
    assert client._session_id is None


def test_application_code_names_the_client_through_the_driver(tmp_path) -> None:
    """The gateway reaches ``continue_only`` through the ACP driver, since the
    agent-sdk boundary refuses it a direct ACP import. The driver's block is the
    same refusal, for the named client only and only while it is open."""
    from kiro_crew.agent_sdk.drivers.acp import continue_only as driver_continue_only

    client = _client(tmp_path, alive=False)
    other = _client(tmp_path, alive=False)

    with driver_continue_only(client):
        assert refuses_start(client)
        assert not refuses_start(other)
    assert not refuses_start(client)
