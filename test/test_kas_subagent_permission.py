"""A KAS sub-agent spawn's permission request is classified, not refused as a shell.

KAS sends no ``tool_call`` frame before a sub-agent spawn and uses a synthetic
``invoke_subagent_<id>`` toolCallId, so every toolCallId-keyed cache misses. The
classification lives on the request itself, in the engine-written ``_meta.kiro``
block. These tests pin that the block is read only on KAS, only to say "not a
shell call", and only when every field agrees -- and that a real shell call with
no recoverable command is still denied.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from kiro_crew import cli_chat
from kiro_crew.acp._dispatch import build_permission_event, kas_consent_tool
from kiro_crew.acp.types import ACP_BACKEND_KAS, ACP_BACKEND_KIRO, AcpPromptStats, JsonRpcMessage
from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig

TOOL_CALL_ID = "invoke_subagent_toolu_01"


def _subagent_params(**kiro_overrides: Any) -> dict[str, Any]:
    """The permission request KAS 2.24 sends for a sub-agent spawn."""
    kiro: dict[str, Any] = {
        "toolId": "invoke_sub_agent",
        "consent": {
            "capability": "subagent",
            "resource": "my-research",
            "askType": "implicit",
        },
        "consentRound": 1,
    }
    kiro.update(kiro_overrides)
    return {
        "sessionId": "s1",
        "toolCall": {
            "toolCallId": TOOL_CALL_ID,
            "status": "pending",
            "title": "Sub-agent: my-research",
        },
        "options": [
            {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
            {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
        ],
        "_meta": {"kiro": kiro},
    }


def _event(params: dict[str, Any], *, kas: bool = True, shell_cache: dict | None = None):
    event, _ = build_permission_event(
        JsonRpcMessage(id=7, method="session/request_permission", params=params),
        tool_input_cache={},
        shell_cache={} if shell_cache is None else shell_cache,
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={},
        kas_consent_meta=kas,
    )
    return event


class TestTheSubagentRequestIsClassified:
    def test_it_resolves_to_a_non_shell_call(self):
        event = _event(_subagent_params())
        assert event.shell_classified is True
        assert event.is_shell is False
        assert cli_chat._unverifiable_shell(event) is False

    def test_the_hook_gate_does_not_read_it_as_an_unrecoverable_shell(self):
        event = _event(_subagent_params())
        result = HookManager().on_tool_call(
            event.title, command=event.shell_command, is_shell=event.is_shell
        )
        assert "could not be verified" not in (result.reason or "")

    def test_it_carries_the_crew_tool_name_and_no_provenance(self):
        """The name deny and governance rules are written against, and nothing
        else: no trusted params, no cache-hit identity flag."""
        event = _event(_subagent_params())
        assert event.tool_name == "use_subagent"
        assert event.raw_params_trusted is False
        assert event.mcp_identity_trusted is False
        assert event.mcp_server_name == ""

    def test_a_deny_rule_on_the_crew_name_binds(self):
        """Without the name, a ``use_subagent`` deny would meet only the title
        ``Sub-agent: my-research`` and the spawn could be approved."""
        event = _event(_subagent_params())
        result = HookManager(HooksConfig(auto_deny_tools=["use_subagent"])).on_tool_call(
            event.title,
            command=event.shell_command,
            is_shell=event.is_shell,
            mcp_tool_name=event.tool_name,
            mcp_server_name=event.mcp_server_name,
            mcp_identity_trusted=event.mcp_identity_trusted,
        )
        assert result.action == TOOL_DENY

    def test_only_the_verified_tool_id_is_known(self):
        assert kas_consent_tool(_subagent_params()) == ("use_subagent", "my-research")
        assert kas_consent_tool(_subagent_params(toolId="orchestrate_subagent")) == ("", "")


class TestEveryOtherShapeStaysUnclassified:
    @pytest.mark.parametrize(
        "overrides",
        [
            {"toolId": "execute_bash", "consent": {"capability": "shell"}},
            {"consent": {"capability": "shell"}},
            {"consent": {"capability": "skill"}},
            {"consent": "subagent"},
            {"consent": None},
            {"toolId": "use_subagent"},
            {"toolId": None},
            {"command": "rm -rf /"},
            {"consent": {"capability": "subagent"}},
            {"consent": {"capability": "subagent", "resource": " "}},
        ],
        ids=[
            "shell-tool",
            "shell-capability",
            "wrong-capability",
            "consent-not-a-dict",
            "no-consent",
            "unknown-tool-id",
            "no-tool-id",
            "command-present",
            "no-target",
            "blank-target",
        ],
    )
    def test_a_disagreeing_field_keeps_the_refusal(self, overrides):
        event = _event(_subagent_params(**overrides))
        assert event.shell_classified is False
        assert event.tool_name == ""
        assert cli_chat._unverifiable_shell(event) is True

    @pytest.mark.parametrize("meta", [None, "kiro", {"kiro": "x"}, {}], ids=repr)
    def test_a_malformed_meta_block_keeps_the_refusal(self, meta):
        params = _subagent_params()
        params["_meta"] = meta
        assert _event(params).shell_classified is False

    def test_another_backend_reads_the_same_frame_as_before(self):
        """kiro-cli (and every non-KAS backend) keeps its behaviour byte-for-byte."""
        event = _event(_subagent_params(), kas=False)
        assert event.shell_classified is False
        assert cli_chat._unverifiable_shell(event) is True

    def test_a_cached_shell_verdict_is_never_overridden(self):
        """The block fills a miss only. A call the tool_call frame classified as a
        shell stays a shell, and its missing command is still denied."""
        event = _event(_subagent_params(), shell_cache={f"{TOOL_CALL_ID}": True})
        assert event.is_shell is True
        assert event.shell_command is None
        result = HookManager().on_tool_call(
            event.title, command=event.shell_command, is_shell=event.is_shell
        )
        assert result.action == TOOL_DENY
        assert "could not be verified" in (result.reason or "")


def test_a_real_shell_with_no_command_is_still_denied():
    result = HookManager().on_tool_call("Run a command", command=None, is_shell=True)
    assert result.action == TOOL_DENY
    assert "could not be verified" in (result.reason or "")


class TestTheTransportsWireTheFlagByBackend:
    @staticmethod
    def _client(backend: str):
        from kiro_crew.acp.client import AcpClient

        client = AcpClient.__new__(AcpClient)
        client._acp_backend = backend
        client._tool_call_inputs = {}
        client._tool_call_input_redacted = {}
        client._tool_call_params = {}
        client._tool_call_is_shell = {}
        client._tool_call_mcp_server = {}
        client._tool_call_tool_name = {}
        client._tool_call_diff_path = {}
        client._permission_options = {}
        client._pi_gate_asked_ids = set()
        client._pi_gate_denied_ids = set()
        client._pi_gate_request_tool = {}
        client.last_prompt_stats = AcpPromptStats()
        return client

    @pytest.mark.parametrize(
        "backend,classified", [(ACP_BACKEND_KAS, True), (ACP_BACKEND_KIRO, False)]
    )
    def test_acp_client(self, backend, classified):
        msg = JsonRpcMessage(
            id=7, method="session/request_permission", params=copy.deepcopy(_subagent_params())
        )
        event = self._client(backend)._build_permission_event(msg)
        assert event.shell_classified is classified
