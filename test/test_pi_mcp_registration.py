"""The actual pi child must acknowledge its MCP handoff without a model prompt."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness.pi import PiLaunch
from kiro_crew.acp.pi_mcp_registration import (
    REGISTRATION_ENV,
    REGISTRATION_MARKER,
    UNREGISTERED_REASON,
    registration_path,
    verify_registration,
)
from kiro_crew.acp.transport_errors import AcpError
from kiro_crew.agent_sdk.backends import ACP_BACKEND_PI

NONCE = "0123456789abcdef" * 2
HANDED_OFF = ("kirocrew-core", "calendar")


@pytest.fixture
def client(tmp_path):
    instance = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_PI)
    instance._scratch_dir = tmp_path
    instance._pi_gate_nonce = NONCE
    instance._pi_mcp_server_names = HANDED_OFF
    instance._mcp_report.begin_session([{"name": name} for name in HANDED_OFF])
    return instance


def write_receipt(client, names, nonce=NONCE):
    path = registration_path(client.work_scratch_dir, client._pi_gate_nonce)
    path.write_text(
        json.dumps({REGISTRATION_MARKER: 1, "nonce": nonce, "servers": names}),
        encoding="utf-8",
    )
    return path


@pytest.mark.asyncio
async def test_registration_uses_the_actual_childs_receipt_without_a_prompt(client):
    path = write_receipt(client, ["calendar", "kirocrew-core"])
    client._send_request = AsyncMock(side_effect=AssertionError("must not submit a prompt"))
    await verify_registration(client)
    client._send_request.assert_not_awaited()
    assert not path.exists()
    assert not any(client._mcp_report.has_reported(name) for name in HANDED_OFF)


@pytest.mark.asyncio
async def test_a_partly_registered_handoff_reports_the_missing_server_as_failed(client):
    """A shadowed server is the extension's own refusal: the session runs, the card says so."""
    write_receipt(client, ["kirocrew-core"])
    await verify_registration(client)
    report = client._mcp_report
    assert report.has_reported("calendar")
    assert not report.has_reported("kirocrew-core")
    assert "calendar" in report.problem_summary()
    assert UNREGISTERED_REASON[:40] in json.dumps(report.payload())


@pytest.mark.asyncio
async def test_a_session_that_registered_none_is_refused(client):
    path = write_receipt(client, [])
    with pytest.raises(AcpError, match="registered none of the MCP servers"):
        await verify_registration(client)
    assert not path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "names",
    [["wrong"], ["kirocrew-core", "calendar", "extra"], [None], "kirocrew-core", None],
)
async def test_a_malformed_registration_is_refused(client, names):
    path = write_receipt(client, names)
    with pytest.raises(AcpError, match="did not confirm the MCP servers"):
        await verify_registration(client)
    assert not path.exists()


@pytest.mark.asyncio
async def test_an_unparseable_receipt_is_refused(client):
    path = registration_path(client.work_scratch_dir, NONCE)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(AcpError, match="did not confirm the MCP servers"):
        await verify_registration(client)
    assert not path.exists()


@pytest.mark.asyncio
async def test_another_sessions_receipt_is_refused(client):
    path = write_receipt(client, list(HANDED_OFF), nonce="other")
    with pytest.raises(AcpError, match="did not confirm the MCP servers"):
        await verify_registration(client)
    assert not path.exists()


@pytest.mark.asyncio
async def test_a_missing_receipt_is_refused(client):
    with pytest.raises(AcpError, match="did not confirm the MCP servers"):
        await verify_registration(client)


@pytest.mark.asyncio
async def test_no_scratch_window_cannot_confirm_and_is_refused(client):
    client._scratch_dir = None
    client._shared_scratch = None
    with pytest.raises(AcpError, match="did not confirm the MCP servers"):
        await verify_registration(client)


@pytest.mark.asyncio
async def test_an_empty_handoff_has_nothing_to_confirm(client):
    client._pi_mcp_server_names = ()
    await verify_registration(client)
    path = write_receipt(client, [])
    await verify_registration(client)
    assert not path.exists()


@pytest.mark.asyncio
async def test_load_to_new_retry_clears_the_previous_childs_receipt(client, monkeypatch):
    path = write_receipt(client, list(HANDED_OFF))
    client._pi_mcp_handoff = ("handoff.json", b"{}")
    published = []
    monkeypatch.setattr(
        "kiro_crew.acp.harness.pi.publish_pi_mcp_handoff",
        lambda *args: published.append(not path.exists()),
    )
    await client._republish_pi_mcp_handoff()
    assert published == [True]
    with pytest.raises(AcpError, match="did not confirm the MCP servers"):
        await verify_registration(client)


def test_the_launch_uses_allocated_scratch_before_scratch_env_is_injected(client):
    launch = PiLaunch()
    launch._launcher = "verified-launcher"
    launch._nonce = NONCE
    launch._session = client
    env = {"KIROCREW_SCRATCH": "inherited-parent-window", REGISTRATION_ENV: "stale"}
    launch.apply_spawn_env(env)
    assert env[REGISTRATION_ENV] == str(registration_path(client.work_scratch_dir, NONCE))


def _session_ready_client(client, handoff, confirms=None):
    proc = MagicMock()
    proc.returncode = None
    proc.stdin = MagicMock()
    proc.stdin.drain = AsyncMock()
    client._process = proc
    client._next_req_id = MagicMock(side_effect=range(1, 100))
    responses = {1: {"protocolVersion": 1, "agentCapabilities": {}}, 2: {"sessionId": "pi-1"}}

    async def fake_wait(req_id, timeout=50.0, *, method="", expected_mcp=None):
        # pi-acp starts pi inside session/new; its session_start writes the receipt.
        if req_id == 2 and confirms is not None:
            write_receipt(client, confirms)
        return responses.get(req_id, {})

    client._wait_for_response = AsyncMock(side_effect=fake_wait)
    client._drain_notifications = AsyncMock()
    handoff.write_bytes(b"sealed")
    client._pi_mcp_handoff = (str(handoff), b"sealed")
    return client


@pytest.mark.asyncio
async def test_session_start_refuses_before_the_first_prompt_and_still_drops_the_handoff(
    client, tmp_path
):
    """The wiring: without a receipt the session never opens, and the handoff goes anyway."""
    handoff = tmp_path / "handoff.json"
    _session_ready_client(client, handoff)
    with pytest.raises(AcpError, match="did not confirm the MCP servers"):
        await client._initialize_session()
    assert not handoff.exists()


@pytest.mark.asyncio
async def test_session_start_opens_once_the_child_confirms(client, tmp_path):
    handoff = tmp_path / "handoff.json"
    _session_ready_client(client, handoff, confirms=list(HANDED_OFF))
    await client._initialize_session()
    assert client._session_id == "pi-1"
    assert not handoff.exists()
