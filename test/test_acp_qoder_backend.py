"""Qoder is selectable only through the pinned argv route and its runtime witnesses.

These tests pin the measured landing state deliberately:

* the id is offered and resolves to itself;
* ``Routing.PINNED_ASK_RULE`` is enforced, but says only that the spawn pins the
  asking rule and that the driver refuses unmeasured release/mode/execution
  observations — it does not claim a resolved-permission read-back;
* the reclaim knows ``qodercli`` now that Crew can spawn it;
* the adapter emits the exact gate argv and none of the forbidden escape hatches;
* allow_always is never selected;
* a tool that starts without a preceding ask for the same id stops the session.

Live probe-4 corpus entries pin the ask-before-execution order and the unpinned
bypass; other negative tripwire cases remain synthetic.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import acp_tool_gate
from kiro_crew.acp import client as client_mod
from kiro_crew.acp import launch as launch_mod
from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.harness.base import SpawnContext
from kiro_crew.acp.harness.qoder import QoderLaunch
from kiro_crew.acp.transport_errors import AcpAuthRequired, AcpError, AcpToolGateUnroutable
from kiro_crew.acp.types import (
    EVENT_PERMISSION_REQUEST,
    METHOD_AUTHENTICATE,
    METHOD_INITIALIZE,
    METHOD_SESSION_LOAD,
    METHOD_SESSION_NEW,
    OPTION_ALLOW_ALWAYS,
    PROVIDER_LABEL_BY_BACKEND,
    PROVIDER_LABEL_QODER,
    QODER_ALLOW_ALWAYS_OPTION_ID,
    QODER_MEASURED_VERSIONS,
    QODER_REQUIRED_MODE,
    UPDATE_CURRENT_MODE,
    AcpEvent,
    JsonRpcMessage,
)
from kiro_crew.agent_sdk import backend_install
from kiro_crew.agent_sdk import backends as sdk_backends
from kiro_crew.agent_sdk import host_auth, tool_gate
from kiro_crew.agent_sdk.backends import (
    ACP_BACKEND_KIRO,
    ACP_BACKEND_LAUNCH,
    ACP_BACKEND_PROCESS_NAMES,
    ACP_BACKEND_QODER,
    ACP_BACKENDS_KNOWN,
    ACP_BACKENDS_SELF_SERVED_ACP,
    ACP_BACKENDS_SESSION_MCP_ARRAY,
    BASELINE_SELECTABLE_BACKENDS,
    POLICY_ID_BY_BACKEND,
    QODER_FORBIDDEN_ARGV_FLAGS,
    QODER_GATE_ARGV,
    QODER_PINNED_SETTINGS_JSON,
    Routing,
    agent_process_markers,
    launch_for,
    qoder_pinned_argv,
    resolve_selected_backend,
)
from kiro_crew.providers.mirrors.registry import ProjectionKind, projection_for

QODER_CORPUS = Path(__file__).parents[1] / "test/fixtures/acp_frames/qoder"
_ASK_BEFORE_EXECUTION_FIXTURES = (
    "project-allow-ask-live.jsonl",
    "session-mcp-ask-live.jsonl",
    "empty-project-ask-live.jsonl",
)


def _qoder_fixture(name: str) -> list[dict]:
    """Read a Qoder fixture through the corpus's own provenance checker."""
    from acp_frame_replay_harness import read_fixture

    meta, frames = read_fixture(QODER_CORPUS / name)
    assert meta["backend"] == ACP_BACKEND_QODER
    assert meta["agent_version"] in {"1.1.17", "1.1.67"}
    return frames


def _response_frames(frames: list[dict], request_id: int) -> list[dict]:
    return [f for f in frames if f.get("id") == request_id and "method" not in f]


def _qoder_client() -> AcpClient:
    """A minimal client for a method that reads state and raises, with no transport."""
    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_QODER
    client._agent_version = "1.1.67"
    client._agent_accepts_image = False
    client._available_mode_ids = []
    client._modes_advertised = False
    client._session_id = "s1"
    client._session_key = "k1"
    client._qoder_asked_ids = set()
    client._qoder_request_tool = {}
    client._qoder_denied_ids = set()
    client._qoder_executed_ids = set()
    client._qoder_approved_ids = set()
    client._qoder_bookkeeping_overflow = False
    client._qoder_live_prompt_seen = True
    client._mcp_notifications = []
    client._permission_options = {}
    client._permission_gate_events = {}
    client._tool_call_inputs = {}
    client._tool_call_input_redacted = {}
    client._tool_call_is_shell = {}
    client._tool_call_params = {}
    client._tool_call_mcp_server = {}
    client._tool_call_tool_name = {}
    client._tool_call_diff_path = {}
    client._tool_call_harness_tool_name = {}
    client._pi_gate_nonce = ""
    return client


def _other_client() -> AcpClient:
    client = _qoder_client()
    client._acp_backend = ACP_BACKEND_KIRO
    return client


def _session_response(mode: str | None, *, advertised: bool = True) -> dict:
    resp: dict = {"sessionId": "qoder-session-1"}
    if advertised:
        resp["modes"] = {
            "currentModeId": mode,
            "availableModes": [
                {"id": "default", "name": "Prompts for approval"},
                {"id": "acceptEdits", "name": "Accept edits"},
                {"id": "bypassPermissions", "name": "Bypass permissions"},
                {"id": "plan", "name": "Plan"},
            ],
        }
    return resp


def _permission_frame(tool_call_id: str, request_id: int) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={
            "sessionId": "s1",
            "options": [
                {
                    "optionId": QODER_ALLOW_ALWAYS_OPTION_ID,
                    "name": "Always allow",
                    "kind": "allow_always",
                },
                {"optionId": "proceed_once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "cancel", "name": "Reject", "kind": "reject_once"},
            ],
            "toolCall": {"toolCallId": tool_call_id, "status": "pending"},
        },
    )


def _tool_frame(tool_call_id: str, status: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_call_id,
                "status": status,
            },
        },
    )


def _mode_frame(mode: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {"sessionUpdate": UPDATE_CURRENT_MODE, "currentModeId": mode},
        },
    )


def _tripwire_client(monkeypatch) -> tuple[AcpClient, list]:
    client = _qoder_client()
    killed: list[bool] = []
    client._qoder_live_prompt_seen = True

    async def kill(*, force: bool = False) -> None:
        killed.append(force)

    async def send(_request_id: int, _payload: dict) -> None:
        return None

    monkeypatch.setattr(client, "_kill_process", kill)
    monkeypatch.setattr(client, "_send_response", send)
    return client, killed


def test_the_id_is_offered_and_selects_itself() -> None:
    assert ACP_BACKEND_QODER in ACP_BACKENDS_KNOWN
    assert ACP_BACKEND_QODER in BASELINE_SELECTABLE_BACKENDS
    assert ACP_BACKEND_QODER in sdk_backends.selectable_backends()
    assert resolve_selected_backend(ACP_BACKEND_QODER) == ACP_BACKEND_QODER


def test_every_selectable_non_relay_harness_is_independently_set_up() -> None:
    """The first-run bypass cannot silently omit a new selectable adapter.

    kiro is the host prerequisite and KAS is its relay; every other selectable
    harness today launches and authenticates without Kiro CLI.  A future
    selectable exception must be added to this test deliberately, not hidden by
    omitting one id from ``ACP_BACKENDS_INDEPENDENT_SETUP``.
    """
    relays = {ACP_BACKEND_KIRO, sdk_backends.ACP_BACKEND_KAS}
    expected = BASELINE_SELECTABLE_BACKENDS - relays
    assert sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP == expected
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_INDEPENDENT_SETUP


def test_model_and_effort_channels_are_scoped_to_their_writes() -> None:
    """Model and effort writes are in; capture/fold and pair-splitting stay out."""
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_MODEL_VIA_CONFIG_OPTION
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_ADVERTISED_MODEL_SELECTION
    assert sdk_backends.ACP_BACKENDS_ADVERTISED_MODEL_EXCLUSIVE == {ACP_BACKEND_QODER}
    assert sdk_backends.model_registry_namespace(ACP_BACKEND_QODER) == "qoder"
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert sdk_backends.effort_config_option_id(ACP_BACKEND_QODER) == "reasoning_effort"
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION
    assert ACP_BACKEND_QODER not in sdk_backends.ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
    # No Qoder prompt-failure capture shows the codex messageId-less text/end-turn
    # terminal shape; absent evidence keeps this classification off.
    assert ACP_BACKEND_QODER not in sdk_backends.ACP_BACKENDS_UNATTRIBUTED_TERMINAL_ERROR


def test_the_id_is_nameable_in_a_policy_and_labelled_as_its_own_provider() -> None:
    assert POLICY_ID_BY_BACKEND[ACP_BACKEND_QODER] == "qoder"
    assert PROVIDER_LABEL_BY_BACKEND[ACP_BACKEND_QODER] == PROVIDER_LABEL_QODER == "qoder"


def test_the_launch_is_a_record_row_for_the_flag_help_does_not_list() -> None:
    launch = launch_for(ACP_BACKEND_QODER)
    assert ACP_BACKEND_QODER in ACP_BACKEND_LAUNCH
    assert ACP_BACKEND_QODER in ACP_BACKENDS_SELF_SERVED_ACP
    assert launch.binary == "qodercli"
    assert launch.acp_args == ("--acp",)
    # One declaration, read from the launch row: qodercli answers ``initialize``
    # with an integer ``protocolVersion`` of 1, so it speaks the SPEC dialect.
    assert client_mod._PROTOCOL_VERSION_BY_BACKEND[ACP_BACKEND_QODER] == 1


def test_pinned_ask_rule_is_enforced_but_does_not_claim_a_resolved_readback() -> None:
    assert sdk_backends.ACP_BACKEND_ROUTING[ACP_BACKEND_QODER] is Routing.PINNED_ASK_RULE
    assert tool_gate.routing_for(ACP_BACKEND_QODER) is Routing.PINNED_ASK_RULE
    assert tool_gate.is_enforced(ACP_BACKEND_QODER) is True
    assert tool_gate.ENFORCED_ROUTINGS == frozenset(
        {
            Routing.SESSION_CONFIG,
            Routing.VERIFIED_SEEDED_SETTINGS,
            Routing.VERIFIED_GATE_EXTENSION,
            Routing.PINNED_ASK_RULE,
        }
    )
    verdict, reason = tool_gate.routing_verdict(
        ACP_BACKEND_QODER, observed_version="1.1.67", observed_mode="default"
    )
    assert verdict is tool_gate.Verdict.ROUTED
    assert "pins permissions" in reason
    assert "disableAllHooks" in reason
    assert "resolved" not in reason
    # Without both handshake witnesses the doctor reports the honest unknown.
    verdict, _reason = tool_gate.routing_verdict(ACP_BACKEND_QODER)
    assert verdict is tool_gate.Verdict.INDETERMINATE


def test_pinned_settings_json_is_valid_with_permissions_first_and_hooks_disabled() -> None:
    settings = json.loads(QODER_PINNED_SETTINGS_JSON)
    assert list(settings) == ["permissions", "disableAllHooks"]
    assert settings["permissions"] == {"ask": ["*"], "defaultMode": "default"}
    assert settings["disableAllHooks"] is True


def test_verdict_becomes_routable_only_with_measured_version_and_default_mode() -> None:
    assert (
        tool_gate.pinned_ask_rule_issue(
            ACP_BACKEND_QODER, version="1.1.67", observed_mode="default"
        )
        == ""
    )
    for version in ("1.1.64", "1.1.68", "next", "", None):
        issue = tool_gate.pinned_ask_rule_issue(
            ACP_BACKEND_QODER, version=version, observed_mode="default"
        )
        assert issue
        assert "1.1.67" in issue
        assert "route is measured only on 1.1.67" in issue
        assert "Install it with" not in issue
        assert "qodercli" not in issue
    for mode in ("bypassPermissions", "", None):
        issue = tool_gate.pinned_ask_rule_issue(
            ACP_BACKEND_QODER, version="1.1.67", observed_mode=mode
        )
        assert issue
        assert mode or "<absent>" in issue


def test_qoder_remedy_matches_the_failed_witness() -> None:
    """Version and mode refusals carry only the action that applies to each."""
    remedy = tool_gate.remediation_for(ACP_BACKEND_QODER, reported_version="1.1.68")
    assert remedy == (
        "This copy of Qoder CLI is not the one Kiro Crew was tested with, so the "
        "chat was not started. Update it with npm i -g @qoder-ai/qodercli@1.1.67, "
        "then start a new chat."
    )
    unreadable = tool_gate.remediation_for(ACP_BACKEND_QODER, reported_version=None)
    assert unreadable == (
        "Kiro Crew could not tell which version of Qoder CLI is installed, so the chat "
        "was not started. Update it with npm i -g @qoder-ai/qodercli@1.1.67, "
        "then start a new chat."
    )
    assert (
        tool_gate.remediation_for(ACP_BACKEND_QODER, reported_version="1.1.67")
        == "Start a new chat."
    )


@pytest.mark.parametrize("reported_version", [["1.1.67"], {}, None])
def test_qoder_remedy_survives_a_malformed_handshake_version(reported_version: object) -> None:
    """An unhashable or absent handshake version gets the install remedy, never a raise."""
    remedy = tool_gate.remediation_for(ACP_BACKEND_QODER, reported_version=reported_version)
    assert "npm i -g @qoder-ai/qodercli@1.1.67" in remedy


def test_qoder_remedy_trusts_only_an_exact_measured_version_string() -> None:
    """Only a measured version string earns the bare new-chat remedy; anything else gets the install line."""
    for reported_version in (["1.1.67"], {}, None, ""):
        remedy = tool_gate.remediation_for(ACP_BACKEND_QODER, reported_version=reported_version)
        assert "npm i -g @qoder-ai/qodercli@1.1.67" in remedy
    assert (
        tool_gate.remediation_for(ACP_BACKEND_QODER, reported_version="1.1.67")
        == "Start a new chat."
    )
    unmeasured = tool_gate.remediation_for(ACP_BACKEND_QODER, reported_version="2.0.0")
    # The remedy names what to do, not the release number it read: the version
    # is an internal fact, and the one an operator acts on is the install line.
    assert "not the one Kiro Crew was tested with" in unmeasured
    assert "2.0.0" not in unmeasured
    assert "npm i -g @qoder-ai/qodercli@1.1.67" in unmeasured


def test_a_mode_refusal_on_measured_qoder_names_no_install_or_flags() -> None:
    """A 1.1.67 user whose mode moved needs only a new chat."""
    with pytest.raises(AcpToolGateUnroutable) as refused:
        _qoder_client()._verify_qoder_routing(_session_response("bypassPermissions"))
    assert str(refused.value).endswith("Start a new chat.")
    assert "npm i" not in str(refused.value)
    assert "--allowed-tools" not in str(refused.value)


def test_session_new_and_restore_require_default_mode() -> None:
    _qoder_client()._verify_qoder_routing(_session_response("default"))
    for mode in ("bypassPermissions", ""):
        with pytest.raises(AcpToolGateUnroutable) as refused:
            _qoder_client()._verify_qoder_routing(_session_response(mode))
        assert str(refused.value) == (
            "Qoder stopped asking before it runs a tool, so Kiro Crew stopped this chat. Start a new chat."
        )
    with pytest.raises(AcpToolGateUnroutable):
        _qoder_client()._verify_qoder_routing(_session_response(None, advertised=False))
    # The check is positive identity, not an inferred non-Kiro state.
    _other_client()._verify_qoder_routing(_session_response("bypassPermissions"))


def test_version_gate_refuses_measured_range_miss_and_absent_version() -> None:
    with pytest.raises(AcpToolGateUnroutable) as refused:
        stub = _qoder_client()
        stub._agent_version = "1.1.64"
        stub._verify_qoder_version()
    assert "1.1.67" in str(refused.value)
    with pytest.raises(AcpToolGateUnroutable):
        absent = _qoder_client()
        absent._agent_version = ""
        absent._verify_qoder_version()
    _qoder_client()._verify_qoder_version()


def test_shared_handshake_skips_qoder_version_gate_for_kiro(monkeypatch) -> None:
    """A Kiro client's initialize cannot enter Qoder-only route witnesses (H13).

    ``_initialize_session`` is driven to the exact shared call site: a Kiro
    client's Qoder verifier is a live bomb, and the next shared step raises the
    probe's own sentinel. The verifier therefore must not run on the shared path.
    """
    from kiro_crew.acp import client_start

    class ProbeEnded(Exception):
        pass

    client = AcpClient.__new__(AcpClient)
    client._acp_backend = ACP_BACKEND_KIRO
    client._derived_spec_snapshot = object()

    def _raise_at_shared_tail(*_args, **_kwargs):
        raise ProbeEnded

    def _forbid_qoder_gate():
        raise AssertionError("Kiro handshake ran Qoder's version gate")

    async def _send_request(_method, _params):
        return 1

    async def _wait_for_response(_request_id, **_kwargs):
        return {"agentCapabilities": {}, "promptCapabilities": {}}

    monkeypatch.setattr(client, "_initialize_params", lambda: {})
    monkeypatch.setattr(client, "_send_request", _send_request)
    monkeypatch.setattr(client, "_wait_for_response", _wait_for_response)
    monkeypatch.setattr(client, "_note_pi_adapter_version", lambda: None)
    monkeypatch.setattr(client, "_note_goose_version", lambda: None)
    monkeypatch.setattr(client, "_verify_qoder_version", _forbid_qoder_gate)
    monkeypatch.setattr(client_start, "require_unchanged_derived_spec", _raise_at_shared_tail)
    with pytest.raises(ProbeEnded):
        asyncio.run(client._initialize_session())


def test_wrong_version_refusal_shows_only_the_remedy(caplog: pytest.LogCaptureFixture) -> None:
    """The user reads the install remedy alone; the gate explanation goes to the log."""
    caplog.set_level(logging.WARNING, logger="kiro_crew.acp.client")
    client = _qoder_client()
    client._agent_version = "1.1.17"
    with pytest.raises(AcpToolGateUnroutable) as refused:
        client._verify_qoder_version()
    assert str(refused.value) == (
        "This copy of Qoder CLI is not the one Kiro Crew was tested with, so the "
        "chat was not started. Update it with npm i -g @qoder-ai/qodercli@1.1.67, "
        "then start a new chat."
    )
    for internal in ("security gate", "route", "harness", "measured only"):
        assert internal not in str(refused.value)
    assert refused.value.__cause__ is None
    assert refused.value.__suppress_context__
    warnings = [
        record
        for record in caplog.records
        if record.name == "kiro_crew.acp.client" and record.levelno == logging.WARNING
    ]
    assert len(warnings) == 1
    assert "would not reach Kiro Crew's security gate" in warnings[0].getMessage()
    assert "1.1.17" in warnings[0].getMessage()


def test_a_mode_refusal_shows_the_simple_stop_notice() -> None:
    """The chat reads the outcome and next step; the wire detail stays in the log."""
    with pytest.raises(AcpToolGateUnroutable) as refused:
        _qoder_client()._verify_qoder_routing(_session_response("bypassPermissions"))
    assert str(refused.value) == (
        "Qoder stopped asking before it runs a tool, so Kiro Crew stopped this chat. Start a new chat."
    )


def test_an_empty_remedy_falls_back_to_the_full_gate_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A version refusal never raises an empty error: no remedy means the full explanation."""
    monkeypatch.setattr(acp_tool_gate, "remediation_for", lambda *_args, **_kwargs: "")
    client = _qoder_client()
    client._agent_version = "1.1.17"
    with pytest.raises(AcpToolGateUnroutable) as refused:
        client._verify_qoder_version()
    message = str(refused.value)
    assert "would not reach Kiro Crew's security gate" in message
    assert "1.1.17" in message
    assert "would not be consulted for them." in message


class _QoderHandshake:
    """Answers qodercli's initialize and session/new, and records each method sent."""

    def __init__(self) -> None:
        self.methods: list[str] = []

    async def send_request(self, method: str, _params: dict) -> int:
        self.methods.append(method)
        return len(self.methods)

    async def wait_for_response(
        self, req_id: int, timeout=None, *, method="", expected_mcp=None
    ) -> dict:
        if self.methods[req_id - 1] == METHOD_INITIALIZE:
            return {
                "protocolVersion": 1,
                "agentInfo": {"version": "1.1.67"},
                "agentCapabilities": {"loadSession": True},
            }
        return _session_response("default")


def test_a_normal_qoder_start_never_sends_authenticate(tmp_path) -> None:
    """A valid sign-in starts without ``authenticate``: that call opens a browser."""
    handshake = _QoderHandshake()
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_QODER)
    client._send_request = handshake.send_request  # type: ignore[method-assign]
    client._wait_for_response = handshake.wait_for_response  # type: ignore[method-assign]

    asyncio.run(client._initialize_session())

    assert handshake.methods == [METHOD_INITIALIZE, METHOD_SESSION_NEW]
    assert METHOD_AUTHENTICATE not in handshake.methods


_SIGNED_OUT = {"code": -32000, "message": "Authentication required: Authentication is required."}
_SIGNED_OUT_REMEDY = host_auth.signed_out_message(ACP_BACKEND_QODER)


def test_the_qoder_signed_out_message_is_plain_and_ends_with_resume() -> None:
    """The auth path leaves failure prefixes to the renderer and offers Resume."""
    assert _SIGNED_OUT_REMEDY == (
        "Qoder is not signed in. Run qodercli login in your terminal and "
        "complete its sign-in, then resume or start a new chat."
    )


def _answer_once(tmp_path, backend: str, method: str, error: dict) -> None:
    """Wait for one request whose only answer is *error*; the wait is the code under test."""
    client = AcpClient(work_dir=tmp_path, acp_backend=backend)
    client._read_message = AsyncMock(  # type: ignore[method-assign]
        return_value=JsonRpcMessage(id=7, error=error)
    )
    asyncio.run(client._wait_for_response(7, method=method))


@pytest.mark.parametrize("method", [METHOD_SESSION_NEW, METHOD_SESSION_LOAD])
def test_a_signed_out_qodercli_answer_is_the_sign_in_remedy(tmp_path, method) -> None:
    with pytest.raises(AcpAuthRequired) as refused:
        _answer_once(tmp_path, ACP_BACKEND_QODER, method, _SIGNED_OUT)
    assert str(refused.value) == _SIGNED_OUT_REMEDY
    # Qoder's sign-in is not Kiro's identity, so the Kiro sign-in card stays off.
    assert refused.value.auth_required is False


@pytest.mark.parametrize(
    "error",
    [
        {"code": -32000, "message": "Login timed out after 60 seconds"},
        {"code": -32603, "message": "Authentication required: Authentication is required."},
        {"message": "Authentication required: Authentication is required."},
    ],
)
def test_other_qoder_session_errors_stay_plain_errors(tmp_path, error) -> None:
    """The code and the wording must both match; either alone is another failure."""
    with pytest.raises(AcpError) as refused:
        _answer_once(tmp_path, ACP_BACKEND_QODER, METHOD_SESSION_NEW, error)
    assert not isinstance(refused.value, AcpAuthRequired)


def test_the_signed_out_answer_is_not_mapped_for_another_harness(tmp_path) -> None:
    with pytest.raises(AcpError) as refused:
        _answer_once(tmp_path, ACP_BACKEND_KIRO, METHOD_SESSION_NEW, _SIGNED_OUT)
    assert not isinstance(refused.value, AcpAuthRequired)


def test_the_reclaim_now_knows_the_process_crew_spawns() -> None:
    assert ACP_BACKEND_PROCESS_NAMES[ACP_BACKEND_QODER] == "qodercli"
    assert "qodercli" in agent_process_markers()
    assert ACP_BACKENDS_KNOWN == set(ACP_BACKEND_PROCESS_NAMES)


def test_the_mcp_projection_is_broker_only_until_a_spec_mirror_is_measured() -> None:
    assert ACP_BACKEND_QODER in ACP_BACKENDS_SESSION_MCP_ARRAY
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_ACP_CLIENT_SPAWNABLE
    declared = projection_for(ACP_BACKEND_QODER)
    assert declared.kind is ProjectionKind.BROKER_ONLY
    assert "session/new" in declared.reason
    assert "per-tool deny" in declared.reason


def test_the_install_probe_names_the_binary_and_the_command(monkeypatch) -> None:
    monkeypatch.setattr(backend_install.acp_driver, "self_served_resolves", lambda _b: False)
    state = backend_install._probe_self_served(ACP_BACKEND_QODER)
    assert state.installed == backend_install.MISSING
    assert state.missing_components == ("qodercli",)
    assert state.policy_id == "qoder"
    assert state.install_command == "npm i -g @qoder-ai/qodercli@1.1.67"


def test_spawn_argv_pins_ask_rule_and_default_mode_and_has_no_escape_hatch() -> None:
    session = SimpleNamespace(_prepare_session_mcp=AsyncMock())
    ctx = SpawnContext(
        agent="",
        work_dir="/tmp",
        model=None,
        environ={},
        home=MagicMock(),
        sandbox_mode="auto",
        session=session,  # type: ignore[arg-type]
    )
    resolve = AsyncMock(
        return_value=("/fake/qodercli", ["/fake/qodercli", "--acp"], "qodercli --acp", "qodercli")
    )
    preflight = AsyncMock(return_value=("/hidden",))
    original_resolve = launch_mod.resolve_self_served_launch
    original_preflight = launch_mod._run_preflight_bounded
    launch_mod.resolve_self_served_launch = resolve
    launch_mod._run_preflight_bounded = preflight
    try:
        plan = asyncio.run(QoderLaunch().resolve_spawn(ctx))
    finally:
        launch_mod.resolve_self_served_launch = original_resolve
        launch_mod._run_preflight_bounded = original_preflight

    assert plan.argv == qoder_pinned_argv(["/fake/qodercli", "--acp"])
    assert plan.argv[-5:] == list(QODER_GATE_ARGV)
    settings = json.loads(plan.argv[plan.argv.index("--settings") + 1])
    assert settings == {
        "permissions": {"ask": ["*"], "defaultMode": "default"},
        "disableAllHooks": True,
    }
    assert settings["disableAllHooks"] is True
    assert plan.argv[plan.argv.index("--permission-mode") + 1] == QODER_REQUIRED_MODE
    assert "--strict-mcp-config" in plan.argv
    for forbidden in QODER_FORBIDDEN_ARGV_FLAGS:
        assert forbidden not in plan.argv
    for mode in ("acceptEdits", "bypassPermissions", "plan"):
        assert plan.argv.count("--permission-mode") == 1
        assert mode not in plan.argv
    session._prepare_session_mcp.assert_awaited_once()
    preflight.assert_awaited_once()


def _approving_client(monkeypatch) -> tuple[AcpClient, dict[int, dict]]:
    client = _qoder_client()
    responses: dict[int, dict] = {}

    async def send(request_id: int, payload: dict) -> None:
        responses[request_id] = payload

    monkeypatch.setattr(client, "_send_response", send)
    return client, responses


def test_allow_always_is_never_chosen_by_the_always_flag(monkeypatch) -> None:
    client, responses = _approving_client(monkeypatch)
    frame = _permission_frame("call_1", 11)
    client._note_qoder_asked(frame)
    client._permission_gate_events[11] = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST, request_id=11, title="Bash"
    )
    assert asyncio.run(client.approve_tool(11, always=True)) is False
    assert responses[11]["outcome"]["outcome"] == "cancelled"


def test_allow_always_is_never_chosen_by_its_qoder_option_id(monkeypatch) -> None:
    client, responses = _approving_client(monkeypatch)
    frame = _permission_frame("call_1", 11)
    client._note_qoder_asked(frame)
    client._permission_gate_events[11] = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST, request_id=11, title="Bash"
    )
    client._permission_options[11] = {
        "once": "proceed_once",
        "always": QODER_ALLOW_ALWAYS_OPTION_ID,
        "reject": "cancel",
    }
    assert asyncio.run(client.approve_tool(11, option_id=QODER_ALLOW_ALWAYS_OPTION_ID)) is False
    assert responses[11]["outcome"] == {"outcome": "selected", "optionId": "cancel"}
    assert QODER_ALLOW_ALWAYS_OPTION_ID != OPTION_ALLOW_ALWAYS


def _approve_built_permission(
    monkeypatch, tool_call_id: str, request_id: int, *, always: bool
) -> tuple[AcpClient, list[dict], list[bool], bool]:
    """Drive a real permission frame through the client's approval lifecycle."""
    client = _qoder_client()
    responses: list[dict] = []
    killed: list[bool] = []

    async def send(request: int | str, payload: dict) -> None:
        responses.append(payload)

    async def kill(*, force: bool = False) -> None:
        killed.append(force)

    monkeypatch.setattr(client, "_send_response", send)
    monkeypatch.setattr(client, "_kill_process", kill)
    client._build_permission_event(_permission_frame(tool_call_id, request_id))
    approved = asyncio.run(client.approve_tool(request_id, always=always))
    return client, responses, killed, approved


def test_approved_lifecycle_consumes_one_ask_and_reuse_requires_a_fresh_ask(
    monkeypatch,
) -> None:
    """One request authorizes exactly one execution, even when ids recur."""
    tool_call_id = "call_reused"
    client, responses, killed, approved = _approve_built_permission(
        monkeypatch, tool_call_id, 1, always=False
    )
    assert approved is True
    assert responses == [{"outcome": {"outcome": "selected", "optionId": "proceed_once"}}]

    # The observed approved execution: initial pending, then in_progress.
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "completed")))
    assert killed == []
    assert tool_call_id not in client._qoder_asked_ids

    # Reuse with no pending frame: the old ask must not still be vouching.
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]

    # A second client proves a fresh pending plus fresh ask authorizes the reused id.
    fresh = _qoder_client()
    fresh_kill: list[bool] = []

    async def fresh_kill_process(*, force: bool = False) -> None:
        fresh_kill.append(force)

    async def approve(_request: int | str, _payload: dict) -> None:
        return None

    monkeypatch.setattr(fresh, "_kill_process", fresh_kill_process)
    monkeypatch.setattr(fresh, "_send_response", approve)
    # The real sequence places pending before the new permission frame; that order
    # lets the fresh ask authorize this reused id.
    asyncio.run(fresh._tripwire_qoder_unasked(_tool_frame(tool_call_id, "pending")))
    fresh._build_permission_event(_permission_frame(tool_call_id, 2))
    assert asyncio.run(fresh.approve_tool(2)) is True
    asyncio.run(fresh._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    assert fresh_kill == []


def test_twenty_approved_calls_leave_no_retained_bookkeeping(monkeypatch) -> None:
    """A completed call retires every per-call witness, including its request id."""
    client = _qoder_client()

    async def send(_request: int | str, _payload: dict) -> None:
        return None

    monkeypatch.setattr(client, "_send_response", send)
    for request_id in range(20):
        tool_call_id = f"call_{request_id}"
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "pending")))
        client._build_permission_event(_permission_frame(tool_call_id, request_id))
        assert asyncio.run(client.approve_tool(request_id)) is True
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "completed")))

    assert client._qoder_request_tool == {}
    assert client._qoder_asked_ids == set()
    assert client._qoder_approved_ids == set()
    assert client._qoder_denied_ids == set()


def test_ask_eviction_preserves_an_approved_mapping_for_a_late_reject(
    monkeypatch,
) -> None:
    """A flood of later asks cannot launder a call Crew already approved."""
    tool_call_id = "call_survives_eviction"
    client, _responses, killed, _approved = _approve_built_permission(
        monkeypatch, tool_call_id, 1, always=False
    )
    assert client._qoder_request_tool["1"] == tool_call_id

    for offset in range(300):
        client._note_qoder_asked(_permission_frame(f"flood_{offset}", 100 + offset))

    assert client._qoder_request_tool["1"] == tool_call_id
    asyncio.run(client.reject_tool(1))
    assert tool_call_id in client._qoder_denied_ids
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool you had refused, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_a_flood_of_unanswered_asks_is_bounded(monkeypatch) -> None:
    """Unanswered Qoder asks share the request map's admission bound."""
    client = _qoder_client()
    for offset in range(1000):
        client._note_qoder_asked(_permission_frame(f"flood_{offset}", offset))

    assert len(client._qoder_asked_ids) <= client_mod._MAX_CACHED_TOOL_PARAMS
    assert len(client._qoder_request_tool) <= client_mod._MAX_CACHED_TOOL_PARAMS
    assert "flood_0" not in client._qoder_asked_ids
    assert f"flood_{1000 - 1}" in client._qoder_asked_ids


def test_overflow_when_every_mapping_is_approved_stops_the_harness(monkeypatch) -> None:
    """A protected full map marks overflow, then the answering site kills Qoder."""
    client = _qoder_client()
    killed: list[bool] = []

    async def kill(*, force: bool = False) -> None:
        killed.append(force)

    monkeypatch.setattr(client, "_kill_process", kill)
    client._qoder_approved_ids.add("approved")
    client._qoder_request_tool = {str(request_id): "approved" for request_id in range(257)}
    client._permission_options[999] = {"once": "proceed_once", "reject": "cancel"}
    client._permission_gate_events[999] = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST, request_id=999, title="Bash"
    )

    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._handle_permission(_permission_frame("call_new", 999)))
    assert str(refused.value) == (
        "Too many tool approvals were waiting at once, so Kiro Crew stopped this "
        "chat to keep your permissions safe. Start a new chat."
    )
    assert killed == [True]
    assert 999 not in client._permission_options
    assert 999 not in client._permission_gate_events
    assert "999" not in client._qoder_request_tool


@pytest.mark.parametrize("status", ["cancelled", "canceled", "refused", "failed"])
def test_a_noncompleted_terminal_cannot_vouch_for_a_reused_id(monkeypatch, status: str) -> None:
    """Terminal cleanup retires the execution witness; reuse needs a fresh ask."""
    tool_call_id = f"call_{status}"
    client, _responses, killed, _approved = _approve_built_permission(
        monkeypatch, tool_call_id, 1, always=False
    )
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, status)))
    if status == "failed":
        assert killed == []

    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_a_duplicate_cancelled_terminal_stops_the_session(monkeypatch) -> None:
    """Terminal cleanup means even another terminal cannot reuse the old verdict."""
    tool_call_id = "call_duplicate_cancelled"
    client, _responses, killed, _approved = _approve_built_permission(
        monkeypatch, tool_call_id, 1, always=False
    )
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "cancelled")))
    assert killed == []

    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "cancelled")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_reused_pending_frame_clears_a_stale_ask(monkeypatch) -> None:
    """A pending frame alone cannot inherit the prior call's approval."""
    tool_call_id = "call_reused_pending"
    client, _responses, killed, _approved = _approve_built_permission(
        monkeypatch, tool_call_id, 1, always=False
    )
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "completed")))

    asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "pending")))
    assert client._qoder_asked_ids == set()
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "completed")))
    assert killed == [True]


def test_denied_call_reaching_in_progress_trips(monkeypatch) -> None:
    """A denial is consumed only by its own execution verdict, not silently."""
    tool_call_id = "call_denied"
    client, responses, killed, approved = _approve_built_permission(
        monkeypatch, tool_call_id, 1, always=False
    )
    assert approved is True
    # A real consumer rejects after the permission-floor view; this path records
    # the denial through the client's own reject method.
    asyncio.run(client.reject_tool(1))
    # approve_tool consumed the recorded options, so reject_tool uses its bounded
    # cancelled fallback; the important wire fact is that it answers denial.
    assert responses[-1] == {"outcome": {"outcome": "cancelled"}}
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool you had refused, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


class _RecordingStdin:
    """Process stdin double: records each write and drains at once."""

    def __init__(self) -> None:
        self.writes: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.writes.append(data)

    async def drain(self) -> None:
        return None

    def is_closing(self) -> bool:
        return False


def test_cancel_records_the_denial_so_a_failed_terminal_is_not_a_bypass(
    monkeypatch, tmp_path
) -> None:
    """Stop's cancelled answer is a denial, not a missing Qoder verdict."""
    client = AcpClient(work_dir=tmp_path, acp_backend=ACP_BACKEND_QODER)
    client._session_id = "s1"
    client._qoder_live_prompt_seen = True
    client._process = MagicMock()
    stdin = _RecordingStdin()
    client._process.stdin = stdin
    responses: list[dict] = []

    async def send(_request: int | str, payload: dict) -> None:
        responses.append(payload)

    killed: list[bool] = []

    async def kill(*, force: bool = False) -> None:
        killed.append(force)

    monkeypatch.setattr(client, "_send_response", send)
    monkeypatch.setattr(client, "_kill_process", kill)
    client._build_permission_event(_permission_frame("call_cancelled", 7))
    assert "call_cancelled" in client._qoder_asked_ids

    asyncio.run(client.cancel_session())
    assert "call_cancelled" in client._qoder_denied_ids
    assert responses == [{"outcome": {"outcome": "cancelled"}}]
    assert len(stdin.writes) == 1
    assert json.loads(stdin.writes[0])["method"] == "session/cancel"

    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_cancelled", "failed")))
    assert killed == []
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_cancelled", "completed")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_a_second_failed_terminal_after_a_denial_trips(monkeypatch) -> None:
    """The failed terminal consumes the denial, so a duplicate failed has no verdict left."""
    client, killed = _tripwire_client(monkeypatch)
    client._qoder_asked_ids.add("call_twice_failed")
    client._qoder_denied_ids.add("call_twice_failed")

    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_twice_failed", "failed")))
    assert killed == []
    assert "call_twice_failed" not in client._qoder_denied_ids
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_twice_failed", "failed")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_a_failed_terminal_without_a_verdict_still_trips(monkeypatch) -> None:
    """Cancel's exception applies only to the request Crew actually denied."""
    client, killed = _tripwire_client(monkeypatch)
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("never_asked", "failed")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_refused_allow_always_records_the_denial_and_a_later_run_trips(
    monkeypatch,
) -> None:
    """Crew's refusal to persist allow_always is still a wire denial."""
    tool_call_id = "call_allow_always"
    client, responses, killed, approved = _approve_built_permission(
        monkeypatch, tool_call_id, 1, always=True
    )
    assert approved is False
    # The permission frame advertised a reject option, so the refusal answers it.
    assert responses == [{"outcome": {"outcome": "selected", "optionId": "cancel"}}]
    assert tool_call_id in client._qoder_denied_ids

    # The harness honours failed, but runs in_progress/completed despite Crew.
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "failed")))
    assert killed == []
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def _refused_allow_always_client(
    monkeypatch, tool_call_id: str, request_id: int, options: dict[str, str]
) -> tuple[AcpClient, dict[int, dict], list[bool]]:
    """A recorded allow_always ask that Crew must refuse, with the given recorded options."""
    client, responses = _approving_client(monkeypatch)
    killed: list[bool] = []

    async def kill(*, force: bool = False) -> None:
        killed.append(force)

    monkeypatch.setattr(client, "_kill_process", kill)
    client._note_qoder_asked(_permission_frame(tool_call_id, request_id))
    client._permission_gate_events[request_id] = AcpEvent(
        kind=EVENT_PERMISSION_REQUEST, request_id=request_id, title="Bash"
    )
    client._permission_options[request_id] = options
    return client, responses, killed


def test_refused_allow_always_answers_the_advertised_reject_option(monkeypatch) -> None:
    """The refusal answers the reject option the harness offered, not a bare cancel."""
    tool_call_id = "call_reject_offered"
    client, responses, killed = _refused_allow_always_client(
        monkeypatch,
        tool_call_id,
        11,
        {"once": "proceed_once", "always": QODER_ALLOW_ALWAYS_OPTION_ID, "reject": "cancel"},
    )
    assert asyncio.run(client.approve_tool(11, always=True)) is False
    assert responses[11] == {"outcome": {"outcome": "selected", "optionId": "cancel"}}
    assert tool_call_id in client._qoder_denied_ids

    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame(tool_call_id, "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool you had refused, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_refused_allow_always_without_a_reject_option_is_cancelled_and_denied(
    monkeypatch,
) -> None:
    """With no reject option advertised, the refusal is a cancel, and still a recorded denial."""
    tool_call_id = "call_no_reject_offered"
    client, responses, _killed = _refused_allow_always_client(
        monkeypatch,
        tool_call_id,
        11,
        {"once": "proceed_once", "always": QODER_ALLOW_ALWAYS_OPTION_ID},
    )
    assert asyncio.run(client.approve_tool(11, always=True)) is False
    assert responses[11] == {"outcome": {"outcome": "cancelled"}}
    assert tool_call_id in client._qoder_denied_ids


def test_refused_allow_always_releases_the_request_mapping(monkeypatch) -> None:
    """The refusal consumes its request's envelope, so no mapping outlives the answer."""
    client, _responses, _killed = _refused_allow_always_client(
        monkeypatch,
        "call_mapping_released",
        11,
        {"once": "proceed_once", "always": QODER_ALLOW_ALWAYS_OPTION_ID, "reject": "cancel"},
    )
    assert client._qoder_request_tool["11"] == "call_mapping_released"
    assert asyncio.run(client.approve_tool(11, always=True)) is False
    assert "11" not in client._qoder_request_tool


def test_a_current_mode_move_buffered_before_the_session_opens_refuses_it() -> None:
    """A mode move buffered while waiting for session/new is checked when the session opens."""
    client = _qoder_client()
    client._mcp_notifications = [_mode_frame("bypassPermissions")]
    with pytest.raises(AcpToolGateUnroutable) as refused:
        client._verify_qoder_routing(_session_response("default"))
    assert str(refused.value) == (
        "Qoder stopped asking before it runs a tool, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert str(refused.value).endswith("Start a new chat.")


def test_a_config_mode_move_buffered_before_the_session_opens_refuses_it() -> None:
    client = _qoder_client()
    client._mcp_notifications = [_config_mode_frame("bypassPermissions")]
    with pytest.raises(AcpToolGateUnroutable) as refused:
        client._verify_qoder_routing(_session_response("default"))
    assert str(refused.value) == (
        "Qoder stopped asking before it runs a tool, so Kiro Crew stopped this chat. Start a new chat."
    )


def test_buffered_default_mode_frames_do_not_refuse_the_session() -> None:
    client = _qoder_client()
    client._mcp_notifications = [
        _tool_frame("call_1", "pending"),
        _mode_frame("default"),
        _config_mode_frame("default"),
    ]
    client._verify_qoder_routing(_session_response("default"))


def test_a_non_qoder_client_never_reads_a_buffered_mode_move() -> None:
    client = _other_client()
    client._mcp_notifications = [
        _mode_frame("bypassPermissions"),
        _config_mode_frame("bypassPermissions"),
    ]
    client._verify_qoder_routing(_session_response("default"))


def test_kiro_init_drain_consumes_a_buffered_qoder_mode_move_without_refusing() -> None:
    """The Kiro init drain carries no mode check, so a buffered bypass frame is only drained."""
    from kiro_crew.acp.mcp_session_report import McpSessionReport

    client = _other_client()
    client._mcp_report = McpSessionReport()
    client._mcp_notifications = [_mode_frame("bypassPermissions")]
    client._mcp_report_frame_floor = 0
    asyncio.run(client._drain_notifications(duration=0.0, idle_exit=0.0))
    assert client._mcp_notifications == []


def test_qoder_spawn_env_removes_gate_relocation_and_permission_names(tmp_path) -> None:
    """Auth remains, but no inherited Qoder variable can rewrite the pin."""
    from kiro_crew.acp.harness.qoder import QODER_REMOVED_ENV_VARS, QoderLaunch

    env = {
        "PATH": "/usr/bin",
        "HOME": str(tmp_path / "operator-home"),
        "QODER_PERSONAL_ACCESS_TOKEN": "measured-auth",
        "QODER_CONFIG_DIR": "/elsewhere/config",
        "QODER_CONFIG_DIR_NAME": "elsewhere",
        "QODER_USER_CONFIG_DIR": "/elsewhere/user",
        "QODER_GLOBAL_CONFIG_FILE": "/elsewhere/settings.json",
        "QODER_PROFILE_DIR": "/elsewhere/profile",
        "QODER_MCP_CONFIG": "/elsewhere/mcp.json",
        "QODER_PERMISSION_MODE": "bypassPermissions",
        "QODER_DISABLE_READONLY_SHELL_AUTO_ALLOW": "1",
        "QODER_SANDBOX": "off",
        "QODER_HOME": "/elsewhere/home",
        "QODER_ENV": "global",
        "QODER_ENV_FILE": "/elsewhere/env",
        "QODER_ENV_MODE": "bypass",
        "QODER_BOOTSTRAP_AUTH_STATE": "{}",
        "QODER_SDK_AUTH_PAYLOAD_FILE": "/elsewhere/auth.json",
        "QODER_AUTH_STDIN": "1",
        "QODER_PROJECT_DIR": "/project",
        "QODER_MODEL": "qfmodel",
    }
    QoderLaunch().apply_spawn_env(env)
    assert all(name not in env for name in QODER_REMOVED_ENV_VARS)
    assert env["QODER_PERSONAL_ACCESS_TOKEN"] == "measured-auth"
    assert env["QODER_AUTH_STDIN"] == "1"
    assert env["QODER_PROJECT_DIR"] == "/project"
    assert env["QODER_MODEL"] == "qfmodel"


def test_mode_update_off_default_stops_the_harness(monkeypatch) -> None:
    client, killed = _tripwire_client(monkeypatch)
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_mode(_mode_frame("bypassPermissions")))
    assert str(refused.value) == (
        "Qoder stopped asking before it runs a tool, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]
    # A re-assert of the required mode is a no-op.
    asyncio.run(client._tripwire_qoder_mode(_mode_frame("default")))
    assert killed == [True]


def test_a_mid_session_mode_move_on_measured_qoder_names_no_install(monkeypatch) -> None:
    """A 1.1.67 user whose mode moves mid-session needs only a new chat, not a reinstall."""
    client, killed = _tripwire_client(monkeypatch)
    assert client.agent_version == "1.1.67"
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_mode(_mode_frame("bypassPermissions")))
    assert str(refused.value).endswith("Start a new chat.")
    assert "npm i" not in str(refused.value)
    assert killed == [True]


def test_in_progress_or_completed_call_without_an_ask_stops_the_harness(monkeypatch) -> None:
    for status in ("in_progress", "completed"):
        client, killed = _tripwire_client(monkeypatch)
        with pytest.raises(AcpToolGateUnroutable) as refused:
            asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_1", status)))
        assert (
            str(refused.value)
            == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
        )
        assert killed == [True]


def test_answered_approved_terminal_completes_and_consumes_its_ask(monkeypatch) -> None:
    client, _responses, killed, approved = _approve_built_permission(
        monkeypatch, "call_1", 1, always=False
    )
    assert approved is True
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_1", "completed")))
    assert killed == []
    assert "call_1" not in client._qoder_asked_ids
    assert "call_1" not in client._qoder_executed_ids
    # A direct completed terminal closed the execution. A later in-progress frame
    # with no fresh pending/ask is a reused call, not the same execution.
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_1", "in_progress")))
    assert killed == [True]


def test_a_denied_call_that_starts_executing_stops_the_harness(monkeypatch) -> None:
    client = _qoder_client()
    killed: list[bool] = []

    async def kill(*, force: bool = False) -> None:
        killed.append(force)

    async def send(_request_id: int, _payload: dict) -> None:
        return None

    monkeypatch.setattr(client, "_kill_process", kill)
    monkeypatch.setattr(client, "_send_response", send)
    client._build_permission_event(_permission_frame("call_1", 1))
    asyncio.run(client.reject_tool(1))
    assert "call_1" in client._qoder_denied_ids
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_1", "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool you had refused, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_a_pending_frame_starts_a_fresh_verdict_for_a_reused_id(monkeypatch) -> None:
    client = _qoder_client()

    async def kill(*, force: bool = False) -> None:
        return None

    async def send(_request_id: int, _payload: dict) -> None:
        return None

    monkeypatch.setattr(client, "_kill_process", kill)
    monkeypatch.setattr(client, "_send_response", send)
    client._qoder_executed_ids.add("call_0")
    client._qoder_denied_ids.add("call_0")
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_0", "pending")))
    assert "call_0" not in client._qoder_executed_ids
    assert "call_0" not in client._qoder_denied_ids
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_0", "completed")))


def test_credential_declaration_is_the_inferred_whole_auth_directory() -> None:
    declaration = host_auth.declaration_for(ACP_BACKEND_QODER)
    assert declaration.credential_leaves == (".qoder/.auth",)
    assert declaration.adapter_own_leaves == declaration.credential_leaves
    assert ".qoder/.auth" in tool_gate.ADAPTER_OWN_CREDENTIAL_LEAVES[ACP_BACKEND_QODER]
    assert ".qoder/.auth" in host_auth.credential_leaves()
    assert QODER_MEASURED_VERSIONS == {"1.1.67"}


@pytest.mark.parametrize("fixture_name", _ASK_BEFORE_EXECUTION_FIXTURES)
def test_live_ask_fixture_requests_before_any_execution(fixture_name: str) -> None:
    """A real pinned/ask-rule slice must ask before a non-pending tool update.

    The fixture's own client response is the selected reject_once answer. The
    client's per-call state consumes the terminal `failed` update without a kill,
    so this is also the replay-side check that a rejected call does not become a
    stale denial for a later call.
    """
    frames = _qoder_fixture(fixture_name)
    pending_at: dict[str, int] = {}
    asked_at: dict[str, int] = {}
    executed_at: dict[str, int] = {}
    for position, raw in enumerate(frames):
        method = raw.get("method")
        params = raw.get("params") if isinstance(raw.get("params"), dict) else {}
        if method == "session/update":
            update = params.get("update") if isinstance(params.get("update"), dict) else {}
            if update.get("sessionUpdate") not in {"tool_call", "tool_call_update"}:
                continue
            call_id = update.get("toolCallId")
            assert isinstance(call_id, str)
            status = update.get("status")
            if status == "pending":
                pending_at[call_id] = position
            elif status in {"in_progress", "completed"}:
                executed_at[call_id] = position
                assert (
                    call_id in asked_at
                ), f"{fixture_name}: {call_id} executed at {position} before a permission ask"
                assert asked_at[call_id] > pending_at[call_id]
        elif method == "session/request_permission":
            tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            call_id = tool_call.get("toolCallId")
            assert isinstance(call_id, str)
            asked_at[call_id] = position

    answers = [
        raw
        for raw in _response_frames(frames, 0)
        if raw.get("result", {}).get("outcome", {}).get("optionId") == "cancel"
    ]
    assert answers, f"{fixture_name}: no client reject_once answer"
    assert pending_at and asked_at
    assert set(asked_at) == set(pending_at)
    # These reject-once captures end failed, not executed.
    assert not executed_at

    client = _qoder_client()
    killed: list[bool] = []

    async def kill(*, force: bool = False) -> None:
        killed.append(force)

    client._kill_process = kill
    for raw in frames:
        msg = JsonRpcMessage.from_dict(raw)
        if msg.method == "session/request_permission":
            client._note_qoder_asked(msg)
        elif msg.method is None and msg.id == 0:
            client._note_qoder_denied(msg.id)
        elif msg.method == "session/update":
            asyncio.run(client._tripwire_qoder_unasked(msg))
    assert killed == []


def test_live_unpinned_control_trips_the_unasked_call_tripwire(monkeypatch) -> None:
    """The real no-request control is exactly the shape the runtime tripwire refuses."""
    frames = _qoder_fixture("unpinned-allow-bypass-control-live.jsonl")
    calls = [
        raw
        for raw in frames
        if raw.get("method") == "session/update"
        and raw["params"]["update"].get("sessionUpdate") in {"tool_call", "tool_call_update"}
    ]
    assert len(calls) == 2
    assert calls[0]["params"]["update"]["status"] == "pending"
    assert calls[1]["params"]["update"]["status"] == "completed"

    client, killed = _tripwire_client(monkeypatch)
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(JsonRpcMessage.from_dict(calls[1])))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_live_v167_handshake_passes_version_and_default_mode_gates() -> None:
    """The real 1.1.67 pinned initialize/session-new pair satisfies both witnesses."""
    frames = _qoder_fixture("v167-handshake-live.jsonl")
    init_result = _response_frames(frames, 1)[0]["result"]
    session_result = _response_frames(frames, 3)[0]["result"]

    client = _qoder_client()
    client._agent_version = client_mod.agent_version_from_init(init_result)
    client._verify_qoder_version()
    client._verify_qoder_routing(session_result)
    assert client.agent_version == "1.1.67"


def test_version_gate_refuses_every_unmeasured_release() -> None:
    """The exact set stays exact, including neighbouring releases and majors."""
    for version in ("1.1.17", "1.1.66", "1.1.68", "2.0.0", ""):
        with pytest.raises(AcpToolGateUnroutable) as refused:
            client = _qoder_client()
            client._agent_version = version
            client._verify_qoder_version()
        if version:
            assert "This copy of Qoder CLI is not the one Kiro Crew was tested with" in str(
                refused.value
            )
        else:
            assert "Kiro Crew could not tell which version of Qoder CLI is installed" in str(
                refused.value
            )
        assert "then start a new chat." in str(refused.value)
        assert "npm i -g @qoder-ai/qodercli@1.1.67" in str(refused.value)
        assert "qodercli update" not in str(refused.value)
    assert (
        tool_gate.pinned_ask_rule_issue(
            ACP_BACKEND_QODER, version="1.1.67", observed_mode="default"
        )
        == ""
    )


def test_effort_uses_qoder_config_option_and_session_owned_levels() -> None:
    """The measured accepted/reflected write enables exactly the channel claims."""
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_EFFORT_VIA_CONFIG_OPTION
    assert sdk_backends.effort_config_option_id(ACP_BACKEND_QODER) == "reasoning_effort"
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_EFFORT_FROM_ADVERTISED_OPTION
    assert ACP_BACKEND_QODER not in sdk_backends.ACP_BACKENDS_MODEL_EFFORT_PAIR_IDS
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_ADVERTISED_MODEL_SELECTION


def test_load_replay_enables_harness_owned_sessions_only() -> None:
    """session/load replay is ownership/restore; eviction and resume stay out."""
    assert ACP_BACKEND_QODER in sdk_backends.ACP_BACKENDS_HARNESS_OWNED_SESSIONS
    assert ACP_BACKEND_QODER not in sdk_backends.ACP_BACKENDS_LOAD_WITHOUT_MODES
    assert ACP_BACKEND_QODER not in sdk_backends.ACP_BACKENDS_RESUME_WITHOUT_LOAD
    assert ACP_BACKEND_QODER not in sdk_backends.ACP_BACKENDS_SESSION_EVICTION
    assert ACP_BACKEND_QODER not in sdk_backends.ACP_BACKENDS_SESSION_SHARING


def test_live_v167_load_replay_and_handshake_provenance() -> None:
    replay = _qoder_fixture("v167-load-replay-live.jsonl")
    updates = [raw["params"]["update"] for raw in replay if raw.get("method") == "session/update"]
    assert [row["sessionUpdate"] for row in updates].count("user_message_chunk") == 2
    assert [row["sessionUpdate"] for row in updates].count("agent_thought_chunk") == 2
    assert [row["sessionUpdate"] for row in updates].count("agent_message_chunk") == 2
    methods = [raw.get("method") for raw in replay]
    assert "session/close" in methods and "session/load" in methods
    load_result = _response_frames(replay, 8)[0]["result"]
    assert load_result["modes"]["currentModeId"] == QODER_REQUIRED_MODE


def _config_mode_frame(value: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        id=None,
        method="session/update",
        params={
            "sessionId": "s1",
            "update": {
                "sessionUpdate": "config_option_update",
                "configOptions": [
                    {"id": "model", "category": "model", "currentValue": "qfmodel"},
                    {"id": "mode", "category": "mode", "currentValue": value},
                ],
            },
        },
    )


def _send_one_live_prompt(client: AcpClient) -> None:
    """Arm the replay gate through the client's real prompt-request method."""
    client._send_request = AsyncMock(return_value=99)  # type: ignore[method-assign]
    asyncio.run(client._send_prompt("live turn", allow_image=False))
    assert client._qoder_live_prompt_seen is True


def test_approved_answer_is_required_before_execution(monkeypatch) -> None:
    """S1: a request that was never answered cannot vouch for in_progress."""
    client, killed = _tripwire_client(monkeypatch)
    client._build_permission_event(_permission_frame("call_answer", 1))
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_answer", "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_deny_then_reused_ask_still_requires_a_new_answer(monkeypatch) -> None:
    """S2: a fresh ask clears the old denial but does not itself authorize."""
    client, killed = _tripwire_client(monkeypatch)
    client._build_permission_event(_permission_frame("call_reanswer", 1))
    asyncio.run(client.reject_tool(1))
    client._build_permission_event(_permission_frame("call_reanswer", 2))
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_reanswer", "in_progress")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_reject_after_execution_stops_the_harness(monkeypatch) -> None:
    """A late reject cannot launder a call Qoder already started."""
    client, _responses, killed, approved = _approve_built_permission(
        monkeypatch, "call_late_reject", 1, always=False
    )
    assert approved is True
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_late_reject", "in_progress")))
    assert client._qoder_executed_ids == {"call_late_reject"}
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client.reject_tool(1))
    assert (
        str(refused.value)
        == "Qoder ran a tool you had refused, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_config_option_mode_select_off_default_stops_the_harness(monkeypatch) -> None:
    client, killed = _tripwire_client(monkeypatch)
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_mode(_config_mode_frame("bypassPermissions")))
    assert str(refused.value) == (
        "Qoder stopped asking before it runs a tool, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]
    # A model select is not a mode witness; required mode through the mode select
    # is likewise a no-op.
    asyncio.run(client._tripwire_qoder_mode(_config_mode_frame("default")))
    assert killed == [True]


def _replay_history_client(monkeypatch) -> tuple[AcpClient, list[bool]]:
    client = _qoder_client()
    client._qoder_live_prompt_seen = False
    killed: list[bool] = []

    async def kill(*, force: bool = False) -> None:
        killed.append(force)

    async def send_response(_request_id: int | str, _payload: dict) -> None:
        return None

    monkeypatch.setattr(client, "_kill_process", kill)
    monkeypatch.setattr(client, "_send_response", send_response)
    return client, killed


def test_replayed_completed_tool_call_does_not_poison_the_first_live_call(
    monkeypatch,
) -> None:
    """Load history may contain a terminal tool frame that never asked here."""
    client, killed = _replay_history_client(monkeypatch)
    replayed = _tool_frame("call_from_history", "completed")
    asyncio.run(client._tripwire_qoder_unasked(replayed))
    assert client._qoder_executed_ids == set()
    assert client._qoder_approved_ids == set()

    _send_one_live_prompt(client)
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_from_history", "pending")))
    client._build_permission_event(_permission_frame("call_from_history", 1))
    assert asyncio.run(client.approve_tool(1)) is True
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_from_history", "in_progress")))
    assert killed == []


def test_replay_after_the_load_response_is_ignored_until_the_first_prompt(
    monkeypatch,
) -> None:
    """1.1.67 can emit restored tool frames after the load response too."""
    client, killed = _replay_history_client(monkeypatch)
    # The load response has returned; the next live prompt has not been sent.
    assert client._qoder_live_prompt_seen is False
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_after_load", "completed")))
    asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_after_load", "pending")))
    assert client._qoder_executed_ids == set()

    _send_one_live_prompt(client)
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_after_load", "completed")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_after_the_first_live_prompt_an_unasked_terminal_still_trips(monkeypatch) -> None:
    client, killed = _replay_history_client(monkeypatch)
    _send_one_live_prompt(client)
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_live", "completed")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]


def test_untriaged_qoder_environment_strings_have_recorded_dispositions() -> None:
    """Names that can redirect home/config/env/auth state cannot reach the child."""
    from kiro_crew.acp.harness.qoder import QODER_REMOVED_ENV_VARS

    removed = {
        # QODER_ENV_HOME is the bundle's source identifier for QODER_HOME.
        "QODER_ENV_HOME",
        "QODER_HOME",
        # QODER_ENV_ENV is the source identifier for QODER_ENV.
        "QODER_ENV_ENV",
        "QODER_ENV",
        "QODER_ENV_FILE",
        "QODER_ENV_MODE",
        "QODER_BOOTSTRAP_AUTH_STATE",
        "QODER_SDK_AUTH_PAYLOAD_FILE",
    }
    assert removed <= QODER_REMOVED_ENV_VARS
    # Auth-token-like inputs and the project placeholder stay ordinary variables.
    for kept in (
        "QODER_PERSONAL_ACCESS_TOKEN",
        "QODER_AUTH_MANAGED_TOKEN",
        "QODER_AUTH_STDIN",
        "QODER_PROJECT_DIR",
        "QODER_USER_INFO",
        "QODER_ENV_PRODUCT",
    ):
        assert kept not in QODER_REMOVED_ENV_VARS


def test_live_v167_toolcall_replay_fixture_arms_only_after_a_prompt(monkeypatch) -> None:
    """The real R4 restore has a terminal tool frame and no ask in this session."""
    frames = _qoder_fixture("v167-load-replay-toolcall-live.jsonl")
    updates = [raw["params"]["update"] for raw in frames if raw.get("method") == "session/update"]
    assert [row["sessionUpdate"] for row in updates].count("tool_call") == 1
    assert not any(raw.get("method") == "session/request_permission" for raw in frames)

    client, killed = _replay_history_client(monkeypatch)
    for raw in frames:
        if raw.get("method") == "session/update":
            asyncio.run(client._tripwire_qoder_unasked(JsonRpcMessage.from_dict(raw)))
    assert client._qoder_executed_ids == set()
    _send_one_live_prompt(client)
    with pytest.raises(AcpToolGateUnroutable):
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_live", "completed")))
    assert killed == [True]


def test_after_the_first_live_prompt_an_unanswered_failed_terminal_trips(
    monkeypatch,
) -> None:
    client, killed = _replay_history_client(monkeypatch)
    _send_one_live_prompt(client)
    with pytest.raises(AcpToolGateUnroutable) as refused:
        asyncio.run(client._tripwire_qoder_unasked(_tool_frame("call_failed_live", "failed")))
    assert (
        str(refused.value)
        == "Qoder ran a tool without asking you first, so Kiro Crew stopped this chat. Start a new chat."
    )
    assert killed == [True]
