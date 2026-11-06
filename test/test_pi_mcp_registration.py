"""The actual pi child's MCP registration receipt, read without a model prompt.

Three outcomes per handed-off server: registered (nothing recorded), confirmed
unregistered (a failed row naming the extension's own cause), and unconfirmed (the
report's unconfirmed bucket, which no "unusable" reader sees).
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness.pi import PiLaunch
from kiro_crew.acp.mcp_session_report import BUCKET_CAP
from kiro_crew.acp.pi_mcp_registration import (
    HANDOFF_REASONS,
    REGISTRATION_ENV,
    REGISTRATION_MARKER,
    REGISTRATION_VERSION,
    UNREGISTERED_REASONS,
    registration_path,
    verify_registration,
)
from kiro_crew.agent_sdk.backends import ACP_BACKEND_PI
from kiro_crew.agent_sdk.mcp_problems import session_mcp_problems
from kiro_crew.subagent import SubagentInfo
from kiro_crew.subagent_manager.run import RunEventCoordinator

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


def write_body(client, body):
    path = registration_path(client.work_scratch_dir, client._pi_gate_nonce)
    path.write_text(json.dumps(body), encoding="utf-8")
    return path


def write_receipt(client, registered, unregistered=None, *, handoff="loaded", nonce=NONCE):
    return write_body(
        client,
        {
            REGISTRATION_MARKER: REGISTRATION_VERSION,
            "nonce": nonce,
            "handoff": handoff,
            "registered": registered,
            "unregistered": unregistered or {},
        },
    )


def failed_reasons(client):
    """Each failed row's reason, as the MCP card reads it off the payload."""
    payload = client._mcp_report.payload() or {}
    return {name: payload["failures"].get(name, "") for name in payload.get("failed", [])}


def unconfirmed(client, caplog):
    """The cause the unconfirmed warning named, or "" when none was logged.

    Unconfirmed is recorded NOWHERE on the report -- the card already shows such
    a server as not reported -- so the warning is the only trace to assert on.
    """
    payload = client._mcp_report.payload() or {}
    assert not any(key.startswith("unconfirmed") for key in payload)
    for record in caplog.records:
        if record.name != "kiro_crew.acp.pi_mcp_registration":
            continue
        message = record.getMessage()
        if "unconfirmed (" in message:
            return message.rsplit("(", 1)[1].rstrip(")")
        if "does not account for" in message:
            return "unaccounted"
    return ""


def provider_for(client):
    provider = MagicMock()
    provider.mcp_session_report.return_value = client._mcp_report
    return provider


# -- registered ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_registration_uses_the_actual_childs_receipt_without_a_prompt(client, caplog):
    path = write_receipt(client, ["calendar", "kirocrew-core"])
    client._send_request = AsyncMock(side_effect=AssertionError("must not submit a prompt"))
    await verify_registration(client)
    client._send_request.assert_not_awaited()
    assert not path.exists()
    assert not any(client._mcp_report.has_reported(name) for name in HANDED_OFF)
    assert unconfirmed(client, caplog) == ""


# -- confirmed unregistered ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("cause", sorted(UNREGISTERED_REASONS))
async def test_a_partly_registered_handoff_names_the_extensions_cause(client, cause, caplog):
    write_receipt(client, ["kirocrew-core"], {"calendar": cause})
    await verify_registration(client)
    assert failed_reasons(client) == {"calendar": UNREGISTERED_REASONS[cause]}
    assert unconfirmed(client, caplog) == ""
    assert "calendar" in client._mcp_report.problem_summary()


@pytest.mark.asyncio
async def test_confirmed_zero_registration_marks_every_server_failed(client):
    write_receipt(client, [], dict.fromkeys(HANDED_OFF, "mcp_json"))
    await verify_registration(client)
    assert failed_reasons(client) == dict.fromkeys(HANDED_OFF, UNREGISTERED_REASONS["mcp_json"])


@pytest.mark.asyncio
@pytest.mark.parametrize("status", sorted(HANDOFF_REASONS))
async def test_a_handoff_the_extension_could_not_load_is_not_blamed_on_mcp_json(client, status):
    """An unreadable or other-session handoff is its own cause, never shadowing."""
    write_receipt(client, [], handoff=status)
    await verify_registration(client)
    reasons = failed_reasons(client)
    assert reasons == dict.fromkeys(HANDED_OFF, HANDOFF_REASONS[status])
    assert all("mcp.json" not in reason for reason in reasons.values())


@pytest.mark.asyncio
async def test_failures_past_the_report_cap_are_counted_apart_from_pruned(client):
    """One registered server plus 65 refused ones overflows the 64-row failed bucket."""
    refused = tuple(f"refused-{index:02d}" for index in range(65))
    client._pi_mcp_server_names = ("kirocrew-core", *refused)
    client._mcp_report.begin_session([{"name": name} for name in client._pi_mcp_server_names])
    client._mcp_report.record_pruned_omitted(2)
    write_receipt(client, ["kirocrew-core"], dict.fromkeys(refused, "refused"))
    await verify_registration(client)
    report = client._mcp_report
    payload = report.payload()
    assert len(payload["failed"]) == BUCKET_CAP
    assert payload["pruned_omitted"] == 2
    assert payload["unregistered_omitted"] == 1
    summary = report.problem_summary()
    assert "+2 more left out as unstartable" in summary
    assert "+1 more the harness did not register" in summary


# -- unconfirmed --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {REGISTRATION_MARKER: 1, "nonce": NONCE, "servers": list(HANDED_OFF)},
        {REGISTRATION_MARKER: 2, "nonce": NONCE, "handoff": "loaded", "registered": "x"},
        {
            REGISTRATION_MARKER: 2,
            "nonce": NONCE,
            "handoff": "later",
            "registered": [],
            "unregistered": {},
        },
        {
            REGISTRATION_MARKER: 2,
            "nonce": NONCE,
            "handoff": "loaded",
            "registered": [],
            "unregistered": {"calendar": "gremlins"},
        },
        {
            REGISTRATION_MARKER: 2,
            "nonce": NONCE,
            "handoff": "loaded",
            "registered": ["calendar"],
            "unregistered": {"calendar": "refused"},
        },
        {
            REGISTRATION_MARKER: 2,
            "nonce": NONCE,
            "handoff": "loaded",
            "registered": [None],
            "unregistered": {},
        },
        ["not", "an", "object"],
    ],
)
async def test_an_invalid_receipt_reports_unconfirmed_never_failed(client, body, caplog):
    path = write_body(client, body)
    await verify_registration(client)
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == "no valid receipt"
    assert not path.exists()


@pytest.mark.asyncio
async def test_an_unparseable_receipt_reports_unconfirmed(client, caplog):
    path = registration_path(client.work_scratch_dir, NONCE)
    path.write_text("{not json", encoding="utf-8")
    await verify_registration(client)
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == "no valid receipt"
    assert not path.exists()


@pytest.mark.asyncio
async def test_another_sessions_receipt_reports_unconfirmed(client, caplog):
    path = write_receipt(client, [], dict.fromkeys(HANDED_OFF, "refused"), nonce="other")
    await verify_registration(client)
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == "no valid receipt"
    assert not path.exists()


@pytest.mark.asyncio
async def test_a_missing_receipt_reports_unconfirmed(client, caplog):
    """pi-acp's ordering is unverified live, so no receipt is no evidence either way."""
    await verify_registration(client)
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == "no valid receipt"


@pytest.mark.asyncio
async def test_a_receipt_silent_on_a_server_leaves_that_one_unconfirmed(client, caplog):
    write_receipt(client, ["kirocrew-core"])
    await verify_registration(client)
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == "unaccounted"


@pytest.mark.asyncio
async def test_no_scratch_window_names_that_cause(client, caplog):
    """launch spawns without scratch on purpose; the report says why nothing confirmed."""
    client._scratch_dir = None
    client._shared_scratch = None
    await verify_registration(client)
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == "no scratch window"


# -- downstream readers -------------------------------------------------------


@pytest.mark.asyncio
async def test_unconfirmed_never_reaches_an_unusable_reader(client):
    """Subagent notice, webhook and task readers all print "unusable": stay silent."""
    await verify_registration(client)
    report = client._mcp_report
    assert report.problem_summary() == ""
    assert report.problem_summary(include_reasons=False) == ""
    assert session_mcp_problems(provider_for(client)) == ""
    coordinator = RunEventCoordinator(MagicMock())
    info = SubagentInfo(id="sub-1", task="call the tool", agent="researcher")
    summary = coordinator._warn_unusable_mcp_servers(info, provider_for(client))
    assert summary == ""
    assert coordinator._spawn_mcp_notice(summary) == ""


@pytest.mark.asyncio
async def test_a_confirmed_refusal_reaches_the_subagent_notice(client):
    write_receipt(client, ["kirocrew-core"], {"calendar": "mcp_json"})
    await verify_registration(client)
    coordinator = RunEventCoordinator(MagicMock())
    info = SubagentInfo(id="sub-1", task="call the tool", agent="researcher")
    summary = coordinator._warn_unusable_mcp_servers(info, provider_for(client))
    assert summary == "failed to start: calendar"
    notice = coordinator._spawn_mcp_notice(summary)
    assert "calendar" in notice and "kirocrew-core" not in notice
    assert "calendar" in session_mcp_problems(provider_for(client))


# -- an empty handoff, retries, launch env, wiring -----------------------------


@pytest.mark.asyncio
async def test_an_empty_handoff_has_nothing_to_confirm(client, caplog):
    client._pi_mcp_server_names = ()
    await verify_registration(client)
    path = write_receipt(client, [])
    await verify_registration(client)
    assert not path.exists()
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == ""


@pytest.mark.asyncio
async def test_load_to_new_retry_clears_the_previous_childs_receipt(client, monkeypatch, caplog):
    path = write_receipt(client, list(HANDED_OFF))
    client._pi_mcp_handoff = ("handoff.json", b"{}")
    published = []
    monkeypatch.setattr(
        "kiro_crew.acp.harness.pi.publish_pi_mcp_handoff",
        lambda *args: published.append(not path.exists()),
    )
    await client._republish_pi_mcp_handoff()
    assert published == [True]
    await verify_registration(client)
    assert unconfirmed(client, caplog) == "no valid receipt"


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
async def test_session_start_without_a_receipt_opens_and_reports_before_the_first_prompt(
    client, tmp_path, caplog
):
    """The wiring: no receipt still opens the session, marks it unknown, drops the handoff."""
    handoff = tmp_path / "handoff.json"
    _session_ready_client(client, handoff)
    await client._initialize_session()
    assert client._session_id == "pi-1"
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == "no valid receipt"
    assert not handoff.exists()


@pytest.mark.asyncio
async def test_session_start_opens_once_the_child_confirms(client, tmp_path, caplog):
    handoff = tmp_path / "handoff.json"
    _session_ready_client(client, handoff, confirms=list(HANDED_OFF))
    await client._initialize_session()
    assert client._session_id == "pi-1"
    assert failed_reasons(client) == {}
    assert unconfirmed(client, caplog) == ""
    assert not handoff.exists()
