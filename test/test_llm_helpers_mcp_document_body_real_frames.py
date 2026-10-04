"""The ``knowledge_add_document`` body exemption on REAL kiro-cli 2.27.1 frames.

``test_llm_helpers_mcp_document_body`` builds its events by hand with
``shell_classified=True``. Real kiro-cli frames never give that: the MCP
``tool_call`` frame carries no ``kind`` (only later updates do), so the shell
cache stays unwritten. The exemption must still apply to a document that
merely quotes ``rm -rf``.

These tests replay frames captured from ``kiro-cli acp`` (default engine,
Tool Search resident and deferred, and KAS ``--agent-engine v3``) through the
real ``_dispatch`` builders and the real ``_resolve_permission``. Session ids,
toolCallIds and a workspace path are anonymised; nothing else is changed.

The default engine stores the document. Everything else keeps
the full deny scan: another tool, another server, a non-body field, a shell
kind, a frame whose identity is only on the permission payload, and KAS (whose identity this
change does not parse, so it stays fail-closed).
"""

from __future__ import annotations

import copy
from unittest.mock import MagicMock, patch

import pytest

import kiro_crew.sel as sel_mod
from kiro_crew.acp import _dispatch as d
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.llm_helpers import ToolApprovalPolicy, _resolve_permission

_BODY = "Cleanup example: rm -rf /tmp/example"
_TCID = "toolu_bdrk_01E9iotykDeJMw8UKDLFzCvn"
_ARGS = {
    "__tool_use_purpose": "R2PURPOSE add doc",
    "title": "r2 probe doc",
    "source_uri": "file:///tmp/r2probe-doc.md",
    "content": _BODY,
}

# kiro-cli 2.27.1, default engine. Resident and deferred are byte-identical in
# shape: under Tool Search the loader call raises no permission request and the
# real call arrives with plain top-level arguments.
_DEFAULT_TOOL_CALL = {
    "sessionUpdate": "tool_call",
    "toolCallId": _TCID,
    "title": "Running: @kirocrew-core/knowledge_add_document",
    "rawInput": _ARGS,
    "_meta": {"kiro": {"toolName": "knowledge_add_document", "mcpServerName": "kirocrew-core"}},
}
_DEFAULT_PERMISSION = {
    "jsonrpc": "2.0",
    "id": "b7fbece2-06ab-4358-8a0e-be60c470b2dd",
    "method": "session/request_permission",
    "params": {
        "sessionId": "s-default",
        "toolCall": {
            "toolCallId": _TCID,
            "title": "Running: @kirocrew-core/knowledge_add_document",
            "rawInput": _ARGS,
        },
        "options": [
            {"optionId": "allow_once", "name": "Yes", "kind": "allow_once"},
            {"optionId": "allow_always", "name": "Always", "kind": "allow_always"},
            {"optionId": "reject_once", "name": "No", "kind": "reject_once"},
        ],
        "_meta": {
            "mcpToolIdentity": {"serverName": "kirocrew-core", "toolName": "knowledge_add_document"}
        },
    },
}

# KAS (``--agent-engine v3``). The tool_call names the server under
# ``serverName`` (not ``mcpServerName``); deferred nests the arguments under
# the meta-tool's ``arguments``.
_KAS_PERM_META = {
    "kiro": {
        "toolId": "mcp_kirocrew_core_knowledge_add_document",
        "agentManagesTrust": True,
        "consent": {
            "capability": "mcp",
            "resource": "kirocrew-core/knowledge_add_document",
            "askType": "implicit",
            "workspaceRoot": "/workspace",
        },
        "consentRound": 1,
        "mcpTool": {
            "version": 1,
            "identity": {"serverName": "kirocrew-core", "toolName": "knowledge_add_document"},
        },
    }
}
_KAS_ARGS = {k: v for k, v in _ARGS.items() if k != "__tool_use_purpose"}
_KAS_TOOL_CALLS = {
    "resident": {
        "sessionUpdate": "tool_call",
        "toolCallId": _TCID,
        "title": "@kirocrew-core/knowledge_add_document",
        "kind": "other",
        "status": "pending",
        "rawInput": {
            **_KAS_ARGS,
            "_meta": {
                "_isValid": True,
                "_activePath": [],
                "_completedPaths": [["title"], ["source_uri"], ["content"]],
            },
        },
        "_meta": {"kiro": {"serverName": "kirocrew-core", "toolOrigin": "client"}},
    },
    "deferred": {
        "sessionUpdate": "tool_call",
        "toolCallId": _TCID,
        "title": "@kirocrew-core/knowledge_add_document",
        "kind": "other",
        "status": "pending",
        "rawInput": {"tool_id": "kirocrew-core::knowledge_add_document", "arguments": _ARGS},
        "_meta": {"kiro": {"serverName": "kirocrew-core", "toolOrigin": "default"}},
    },
}
_KAS_PERMISSION = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "session/request_permission",
    "params": {
        "sessionId": "s-kas",
        "toolCall": {
            "toolCallId": _TCID,
            "status": "pending",
            "title": "@kirocrew-core/knowledge_add_document",
        },
        "options": [
            {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
            {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
        ],
        "_meta": _KAS_PERM_META,
    },
}


def _replay(tool_call: dict | None, permission: dict, *, kas: bool = False):
    caches: dict = dict(
        tool_input_cache={},
        shell_cache={},
        raw_params_cache={},
        mcp_server_name_cache={},
        tool_name_cache={},
        tool_input_redacted_cache={},
        diff_path_cache={},
        harness_tool_name_cache={},
    )
    if tool_call is not None:
        d._build_tool_call_event(copy.deepcopy(tool_call), **caches)
    event, _ = d.build_permission_event(
        JsonRpcMessage.from_dict(copy.deepcopy(permission)), kas_consent_meta=kas, **caches
    )
    return event


class _Provider:
    def __init__(self) -> None:
        self.approved: list = []
        self.rejected: list = []

    async def approve_tool(self, request_id) -> None:
        self.approved.append(request_id)

    async def reject_tool(self, request_id) -> None:
        self.rejected.append(request_id)


async def _resolve(event) -> tuple[bool, str]:
    rows: list[dict] = []
    sel_stub = MagicMock()
    sel_stub.log_tool_invocation.side_effect = lambda **kw: rows.append(kw)
    with patch.object(sel_mod, "sel", lambda: sel_stub):
        approved = await _resolve_permission(
            _Provider(),  # type: ignore[arg-type]
            event,
            ToolApprovalPolicy.AUTO_APPROVE,
            None,
        )
    return approved, " ".join(str(r.get("error") or "") for r in rows)


def _with(frame: dict, **changes) -> dict:
    out = copy.deepcopy(frame)
    out.update(changes)
    return out


def _with_args(frame: dict, permission: dict, **args) -> tuple[dict, dict]:
    tc, perm = copy.deepcopy(frame), copy.deepcopy(permission)
    tc["rawInput"].update(args)
    perm["params"]["toolCall"]["rawInput"].update(args)
    return tc, perm


class TestDefaultEngineStoresADocumentThatQuotesACommand:
    def test_the_real_frame_has_no_kind_and_a_trusted_identity(self) -> None:
        # The shape the fix rests on. If kiro-cli starts sending a kind here,
        # the shell cache answers instead and this precondition moves.
        ev = _replay(_DEFAULT_TOOL_CALL, _DEFAULT_PERMISSION)
        assert "kind" not in _DEFAULT_TOOL_CALL
        assert ev.shell_classified is False
        assert ev.is_shell is False
        assert ev.mcp_identity_trusted is True
        assert (ev.mcp_server_name, ev.tool_name) == ("kirocrew-core", "knowledge_add_document")

    @pytest.mark.asyncio
    async def test_the_body_is_not_read_as_a_command(self) -> None:
        approved, err = await _resolve(_replay(_DEFAULT_TOOL_CALL, _DEFAULT_PERMISSION))
        assert approved is True, err


class TestEverythingElseKeepsTheFullScan:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("field", ["title", "source_uri"])
    async def test_a_non_body_field_is_still_command_scanned(self, field: str) -> None:
        tc, perm = _with_args(_DEFAULT_TOOL_CALL, _DEFAULT_PERMISSION, **{field: _BODY})
        approved, err = await _resolve(_replay(tc, perm))
        assert approved is False
        assert err

    @pytest.mark.asyncio
    async def test_another_core_tool_is_denied(self) -> None:
        tc = _with(
            _DEFAULT_TOOL_CALL,
            _meta={"kiro": {"toolName": "artifact_save", "mcpServerName": "kirocrew-core"}},
        )
        approved, _ = await _resolve(_replay(tc, _DEFAULT_PERMISSION))
        assert approved is False

    @pytest.mark.asyncio
    async def test_the_same_tool_on_another_server_is_denied(self) -> None:
        tc = _with(
            _DEFAULT_TOOL_CALL,
            _meta={"kiro": {"toolName": "knowledge_add_document", "mcpServerName": "notes"}},
        )
        approved, _ = await _resolve(_replay(tc, _DEFAULT_PERMISSION))
        assert approved is False

    @pytest.mark.asyncio
    async def test_a_shell_kind_is_denied(self) -> None:
        tc = _with(_DEFAULT_TOOL_CALL, kind="execute")
        approved, _ = await _resolve(_replay(tc, _DEFAULT_PERMISSION))
        assert approved is False

    @pytest.mark.asyncio
    async def test_a_frame_with_no_identity_is_denied(self) -> None:
        # The permission frame still carries ``_meta.mcpToolIdentity``: that
        # payload is never read as identity, so the scan stays full.
        tc = _with(_DEFAULT_TOOL_CALL, _meta={})
        approved, _ = await _resolve(_replay(tc, _DEFAULT_PERMISSION))
        assert approved is False


class TestKasStaysFailClosed:
    """KAS identity is not parsed by this change, so the scan still denies."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["resident", "deferred"])
    async def test_the_body_is_still_denied(self, mode: str) -> None:
        ev = _replay(_KAS_TOOL_CALLS[mode], _KAS_PERMISSION, kas=True)
        assert ev.mcp_server_name == ""
        approved, _ = await _resolve(ev)
        assert approved is False

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", ["resident", "deferred"])
    async def test_a_benign_body_is_approved(self, mode: str) -> None:
        # Control: the denial above is the body, not the frame.
        tc = copy.deepcopy(_KAS_TOOL_CALLS[mode])
        args = tc["rawInput"].get("arguments", tc["rawInput"])
        args["content"] = "hello world"
        approved, err = await _resolve(_replay(tc, _KAS_PERMISSION, kas=True))
        assert approved is True, err
