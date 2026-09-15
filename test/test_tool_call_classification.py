"""Provider-neutral shell/MCP classification of ACP tool-call frames.

ACP adapters disagree on what ``kind`` an MCP-served tool call carries. kiro-cli
reports the tool's own kind and names the server in ``_meta.kiro.mcpServerName``.
codex-acp reuses its shell builder (``createExecuteToolCallUpdate``), so an MCP
call arrives as ``kind="execute"`` with ``rawInput={server, tool, arguments}`` and
a ``_meta.is_mcp_tool_call`` marker. claude-agent-acp and opencode send
``kind="other"`` and name nothing.

Reading the kind alone therefore classified every codex MCP call as a shell
command; its params carry no ``command``, so ``HookManager.on_tool_call``'s
deny-by-default backstop refused it ("shell command could not be verified").
Only read-only tools, which codex often runs without a permission request,
escaped. These tests pin ``classify_tool_call`` -- the one place the whole frame
is read -- and every consumer of its verdict.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from kiro_crew import cli_chat
from kiro_crew.acp._dispatch import (
    ToolCallIdentity,
    _build_tool_call_event,
    build_permission_event,
    classify_tool_call,
    parse_session_update,
)
from kiro_crew.acp.types import (
    EVENT_PERMISSION_REQUEST,
    EVENT_TOOL_CALL,
    EVENT_TOOL_CALL_UPDATE,
    AcpEvent,
    AcpPromptStats,
    JsonRpcMessage,
)
from kiro_crew.hooks import TOOL_DENY, HookManager

SERVER = "kirocrew-core"
TOOL = "spawn_run"


def _codex_mcp_update(call_id: str = "c1", **overrides: Any) -> dict[str, Any]:
    """The ``tool_call`` update codex-acp emits for an MCP call
    (``createMcpToolCallUpdate`` over ``createExecuteToolCallUpdate``)."""
    update: dict[str, Any] = {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "kind": "execute",
        "title": f"mcp.{SERVER}.{TOOL}",
        "status": "pending",
        "rawInput": {"server": SERVER, "tool": TOOL, "arguments": {"task": "x"}},
        "_meta": {"is_mcp_tool_call": True},
    }
    update.update(overrides)
    return update


def _codex_shell_update(call_id: str = "s1") -> dict[str, Any]:
    """codex-acp's shell frame: the same builder, no marker, a ``command``."""
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "kind": "execute",
        "title": "ls -la",
        "status": "pending",
        "rawInput": {"command": ["ls", "-la"], "cwd": "/tmp"},
    }


def _codex_mcp_approval(request_id: int, call_id: str) -> JsonRpcMessage:
    """The correlated ``session/request_permission`` (``buildMcpPermissionRequest``):
    ``kind="execute"`` again, no rawInput of its own."""
    return JsonRpcMessage(
        id=request_id,
        method="session/request_permission",
        params={
            "sessionId": "s-1",
            "toolCall": {"toolCallId": call_id, "kind": "execute", "status": "pending"},
            "_meta": {"is_mcp_tool_approval": True},
            "options": [
                {"optionId": "allow_once", "name": "Allow", "kind": "allow_once"},
                {"optionId": "cancel", "name": "Cancel", "kind": "reject_once"},
            ],
        },
    )


def _kiro_mcp_update(call_id: str = "k1") -> dict[str, Any]:
    return {
        "sessionUpdate": "tool_call",
        "toolCallId": call_id,
        "kind": "other",
        "title": "Spawning a subagent",
        "rawInput": {"task": "x"},
        "_meta": {"kiro": {"toolName": TOOL, "mcpServerName": SERVER}},
    }


# ── the classifier ───────────────────────────────────────────────────────────


class TestClassifyToolCall:
    """One verdict per adapter shape. Shell and MCP are exclusive, and the kind
    decides only when no adapter-authored MCP marker is present."""

    def test_kiro_mcp_frame_is_mcp_not_shell(self) -> None:
        got = classify_tool_call(_kiro_mcp_update())
        assert got == ToolCallIdentity(
            kind_resolved=True,
            is_shell=False,
            mcp_server_name=SERVER,
            tool_name=TOOL,
            identity_trusted=True,
        )

    def test_kiro_builtin_shell_frame_keeps_tool_name_and_is_shell(self) -> None:
        """kiro-cli's built-in shell tool: ``_meta.kiro`` names the tool but no
        server. The kind decides (shell) and the host-known built-in name is
        still carried for hooks, without any MCP identity being asserted."""
        got = classify_tool_call(
            {
                "kind": "execute",
                "rawInput": {"command": "ls"},
                "_meta": {"kiro": {"toolName": "execute_bash", "mcpServerName": ""}},
            }
        )
        assert got.is_shell is True
        assert got.tool_name == "execute_bash"
        assert got.mcp_server_name == ""
        assert got.identity_trusted is False

    def test_codex_mcp_frame_is_mcp_not_shell_with_trusted_identity(self) -> None:
        got = classify_tool_call(_codex_mcp_update())
        assert got == ToolCallIdentity(
            kind_resolved=True,
            is_shell=False,
            mcp_server_name=SERVER,
            tool_name=TOOL,
            identity_trusted=True,
        )

    def test_codex_shell_frame_is_shell(self) -> None:
        got = classify_tool_call(_codex_shell_update())
        assert got.is_shell is True
        assert got.identity_trusted is False
        assert (got.mcp_server_name, got.tool_name) == ("", "")

    def test_codex_dynamic_tool_frame_stays_shell(self) -> None:
        """``createDynamicToolCallUpdate``: ``kind="execute"`` and no marker. Not an
        MCP call, so it keeps the shell verdict and the deny-by-default gate that
        goes with an unrecoverable command -- the classifier widens nothing here."""
        got = classify_tool_call(
            {"kind": "execute", "rawInput": {"arguments": {"a": 1}}, "title": "my_tool"}
        )
        assert got.is_shell is True
        assert got.identity_trusted is False

    @pytest.mark.parametrize(
        "raw_input",
        [
            {"server": SERVER},  # tool missing
            {"tool": TOOL},  # server missing
            {"server": "", "tool": TOOL},  # empty server
            {"server": 7, "tool": TOOL},  # non-string
            "not a dict",
            None,
        ],
    )
    def test_codex_marker_with_unreadable_pair_resolves_nothing(self, raw_input) -> None:
        """A marker without a readable pair is a malformed frame. It is not shell
        (no command bytes exist) but it must not mint a RESOLVED non-shell
        verdict either: ``kind_resolved`` is False so the shell cache stays
        unwritten and the permission event fails closed to low fidelity."""
        got = classify_tool_call(_codex_mcp_update(rawInput=raw_input))
        assert got.is_shell is False
        assert got.kind_resolved is False
        assert got.identity_trusted is False
        assert (got.mcp_server_name, got.tool_name) == ("", "")

    @pytest.mark.parametrize("marker", ["true", 1, None, {}])
    def test_codex_marker_must_be_literally_true(self, marker) -> None:
        """Anything but ``True`` is not the adapter's marker: the kind decides."""
        got = classify_tool_call(_codex_mcp_update(_meta={"is_mcp_tool_call": marker}))
        assert got.is_shell is True
        assert got.identity_trusted is False

    def test_kiro_identity_outranks_codex_marker(self) -> None:
        """Both markers on one frame: the engine-authored kiro identity wins,
        and with it kiro's rule that the kind stands."""
        update = _codex_mcp_update()
        update["_meta"]["kiro"] = {"toolName": "other_tool", "mcpServerName": "other-server"}
        got = classify_tool_call(update)
        assert (got.mcp_server_name, got.tool_name) == ("other-server", "other_tool")
        assert got.is_shell is True

    def test_kiro_execute_kind_with_server_name_stays_shell(self) -> None:
        """kiro-cli chooses the kind per tool, so its ``execute`` is a shell tool
        whatever else the frame names: the identity is carried, the shell
        verdict is not waived (the rule ``child_mcp_identity_trusted`` pins)."""
        got = classify_tool_call(
            {
                "kind": "execute",
                "title": "Running: ls",
                "_meta": {"kiro": {"mcpServerName": "kb", "toolName": "ask"}},
            }
        )
        assert got.is_shell is True
        assert (got.mcp_server_name, got.tool_name) == ("kb", "ask")
        assert got.identity_trusted is True

    @pytest.mark.parametrize("kind", ["other", "read", "edit", "fetch"])
    def test_unmarked_non_execute_kind_is_neither(self, kind) -> None:
        """claude-agent-acp / opencode MCP shape: no marker, ``kind="other"``.
        Not shell, and no identity -- interactive approval as before."""
        got = classify_tool_call({"kind": kind, "rawInput": {"a": 1}})
        assert got.is_shell is False
        assert got.identity_trusted is False
        assert got.kind_resolved is True

    @pytest.mark.parametrize("update", [{}, {"kind": ""}, {"kind": None}, {"kind": 1}])
    def test_missing_kind_is_unresolved(self, update) -> None:
        got = classify_tool_call(update)
        assert got.kind_resolved is False
        assert got.is_shell is False

    @pytest.mark.parametrize("meta", ["nope", 3, [], {"kiro": "nope"}])
    def test_malformed_meta_falls_back_to_kind(self, meta) -> None:
        got = classify_tool_call({"kind": "execute", "_meta": meta})
        assert got.is_shell is True
        assert got.identity_trusted is False


# ── the tool_call event builder and its caches ───────────────────────────────


class TestBuildToolCallEventCodex:
    def test_codex_mcp_event_is_not_shell_and_carries_trusted_identity(self) -> None:
        shell_cache: dict[str, bool] = {}
        server_cache: dict[str, str] = {}
        tool_cache: dict[str, str] = {}
        event = _build_tool_call_event(
            _codex_mcp_update("c1"),
            None,
            shell_cache=shell_cache,
            mcp_server_name_cache=server_cache,
            tool_name_cache=tool_cache,
        )
        assert event.kind == EVENT_TOOL_CALL
        assert event.is_shell is False
        assert event.mcp_server_name == SERVER
        assert event.tool_name == TOOL
        assert event.mcp_identity_trusted is True
        # The caches the permission event inherits from carry the same verdict.
        assert shell_cache == {"c1": False}
        assert server_cache == {"c1": SERVER}
        assert tool_cache == {"c1": TOOL}

    def test_codex_shell_event_is_still_shell(self) -> None:
        shell_cache: dict[str, bool] = {}
        event = _build_tool_call_event(_codex_shell_update("s1"), None, shell_cache=shell_cache)
        assert event.is_shell is True
        assert event.mcp_identity_trusted is False
        assert shell_cache == {"s1": True}

    def test_codex_mcp_refinement_does_not_flip_cached_verdict_to_shell(self) -> None:
        """codex-acp builds the ``tool_call_update`` with the same shell builder,
        so the refinement carries ``kind="execute"`` too. It must be classified
        by the same rule, or the refresh writes ``True`` over the initial
        frame's ``False`` and the later permission event reads shell."""
        caches: dict[str, Any] = {
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        parse_session_update(_codex_mcp_update("c2"), **caches)
        assert caches["shell_cache"] == {"c2": False}
        refinement = _codex_mcp_update("c2", sessionUpdate="tool_call_update", status="in_progress")
        events = parse_session_update(refinement, **caches)
        assert any(e.kind == EVENT_TOOL_CALL_UPDATE for e in events)
        assert caches["shell_cache"] == {"c2": False}
        for e in events:
            if e.kind == EVENT_TOOL_CALL_UPDATE:
                assert e.is_shell is False

    def test_codex_status_only_update_after_approval_leaves_verdict_alone(self) -> None:
        """After an accepted approval codex-acp sends ``{toolCallId, status}`` with
        no ``kind`` and no marker. An unresolved kind refreshes nothing, so the
        initial frame's MCP verdict survives to the result."""
        caches: dict[str, Any] = {
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        parse_session_update(_codex_mcp_update("c2b"), **caches)
        parse_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": "c2b", "status": "in_progress"},
            **caches,
        )
        assert caches["shell_cache"] == {"c2b": False}


# ── the permission event and the gate ────────────────────────────────────────


def _permission_after_tool_call(update: dict[str, Any], request_id: int = 7) -> AcpEvent:
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
    }
    parse_session_update(update, **caches)
    event, _ = build_permission_event(
        _codex_mcp_approval(request_id, update["toolCallId"]), **caches
    )
    return event


class TestPermissionPathCodex:
    def test_permission_event_inherits_non_shell_and_identity(self) -> None:
        event = _permission_after_tool_call(_codex_mcp_update("c3"))
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.is_shell is False
        assert event.shell_classified is True
        assert event.raw_params_trusted is True
        assert event.mcp_server_name == SERVER
        assert event.tool_name == TOOL
        assert event.mcp_identity_trusted is True
        # An MCP call has no command bytes; that is now not a defect.
        assert event.shell_command is None

    def test_dashboard_gate_no_longer_denies_by_default(self) -> None:
        """End to end through ``HookManager.on_tool_call`` exactly as the dashboard
        runner calls it: a codex MCP write tool is not refused for lacking a
        shell command. It is not auto-approved either -- ``kirocrew-core`` is no
        app-owned server here -- so the call reaches the human prompt."""
        event = _permission_after_tool_call(_codex_mcp_update("c4"))
        result = HookManager().on_tool_call(
            event.title or f"mcp.{SERVER}.{TOOL}",
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
            command=event.shell_command,
            is_shell=event.is_shell,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            mcp_identity_trusted=event.mcp_identity_trusted,
        )
        assert result.action != TOOL_DENY, result.reason

    def test_malformed_codex_marker_leaves_child_permission_low_fidelity(self) -> None:
        """A marker frame whose pair is unreadable writes no shell classification,
        so a child permission event on it keeps ``child_low_fidelity`` True and
        every fidelity-gated auto-approve stays closed."""
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        malformed = _codex_mcp_update("c-mal", rawInput={"server": SERVER, "arguments": {}})
        parse_session_update(malformed, cache_scope="child-a", **caches)
        assert caches["shell_cache"] == {}
        event, _ = build_permission_event(
            _codex_mcp_approval(9, "c-mal"), cache_scope="child-a", **caches
        )
        event.sub_session_id = "child-a"
        assert event.shell_classified is False
        assert event.is_shell is False
        # The identity caches record a hit with EMPTY names (the same shape a
        # kiro built-in writes), so no identity-keyed grant can match.
        assert (event.mcp_server_name, event.tool_name) == ("", "")
        assert event.child_low_fidelity is True
        assert event.child_mcp_identity_trusted is False

    def test_unrecoverable_codex_shell_command_is_still_denied(self) -> None:
        """The control the fix must not weaken: a genuine shell frame whose
        command cannot be recovered keeps the deny-by-default refusal."""
        update = _codex_shell_update("s2")
        update["rawInput"] = {"cwd": "/tmp"}  # no command at all
        event = _permission_after_tool_call(update)
        assert event.is_shell is True
        assert event.shell_command is None
        result = HookManager().on_tool_call(
            "Run a command",
            command=event.shell_command,
            is_shell=event.is_shell,
        )
        assert result.action == TOOL_DENY
        assert "could not be verified" in (result.reason or "")


class TestCliChatUnverifiableShell:
    def test_classified_mcp_call_with_execute_kind_on_payload_is_not_refused(self) -> None:
        """codex-acp labels the MCP approval itself ``kind="execute"``. The CLI
        gate reads that payload kind as a deny signal for a classified
        non-shell call; a proven MCP identity must outrank it."""
        event = _permission_after_tool_call(_codex_mcp_update("c5"))
        assert event.tool_kind == "execute"
        assert event.shell_classified is True and event.is_shell is False
        assert cli_chat._unverifiable_shell(event) is False

    def test_classified_non_shell_without_identity_still_denied_on_execute_kind(self) -> None:
        """Negative control: the payload-kind deny stays armed when no trusted
        identity backs the non-shell classification."""
        event = AcpEvent(
            kind=EVENT_PERMISSION_REQUEST,
            request_id=1,
            title="Read a file",
            tool_kind="execute",
            shell_classified=True,
            is_shell=False,
        )
        assert cli_chat._unverifiable_shell(event) is True


# ── the legacy AcpClient path ────────────────────────────────────────────────


def _bare_client():
    from kiro_crew.acp.client import AcpClient

    client = AcpClient.__new__(AcpClient)  # avoid spawning a real process
    client._tool_call_inputs = {}
    client._tool_call_input_redacted = {}
    client._tool_call_params = {}
    client._tool_call_is_shell = {}
    client._tool_call_mcp_server = {}
    client._tool_call_tool_name = {}
    client._tool_call_diff_path = {}
    client.last_prompt_stats = AcpPromptStats()
    return client


class TestAcpClientCodexPath:
    def test_extract_tool_event_classifies_codex_mcp_as_mcp(self) -> None:
        client = _bare_client()
        msg = JsonRpcMessage(
            method="session/update", params={"sessionId": "s", "update": _codex_mcp_update("c6")}
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.is_shell is False
        assert event.mcp_server_name == SERVER
        assert event.tool_name == TOOL
        assert event.mcp_identity_trusted is True
        assert client._tool_call_is_shell == {"c6": False}
        assert client._tool_call_mcp_server == {"c6": SERVER}
        assert client._tool_call_tool_name == {"c6": TOOL}

    def test_extract_tool_event_does_not_cache_malformed_codex_mcp(self) -> None:
        client = _bare_client()
        msg = JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "s",
                "update": _codex_mcp_update("c-mal", rawInput={"server": SERVER, "arguments": {}}),
            },
        )
        assert client._extract_tool_event(msg) is not None
        assert "c-mal" not in client._tool_call_is_shell

    def test_extract_tool_event_keeps_codex_shell_as_shell(self) -> None:
        client = _bare_client()
        msg = JsonRpcMessage(
            method="session/update", params={"sessionId": "s", "update": _codex_shell_update("s3")}
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.is_shell is True
        assert client._tool_call_is_shell == {"s3": True}

    def test_refinement_does_not_flip_codex_mcp_to_shell(self) -> None:
        client = _bare_client()
        client._extract_tool_event(
            JsonRpcMessage(
                method="session/update",
                params={"sessionId": "s", "update": _codex_mcp_update("c7")},
            )
        )
        refinement = _codex_mcp_update("c7", sessionUpdate="tool_call_update", status="in_progress")
        event = client._extract_tool_call_refinement(
            JsonRpcMessage(method="session/update", params={"sessionId": "s", "update": refinement})
        )
        assert event is not None
        assert event.is_shell is False
        assert client._tool_call_is_shell == {"c7": False}


def test_permission_event_json_input_matches_codex_params() -> None:
    """The permission event's ``tool_input`` is the codex ``rawInput`` verbatim, so
    a reader that keys on ``server``/``tool`` (``_identified_mcp_call``) and this
    classifier agree on which pair the call names."""
    event = _permission_after_tool_call(_codex_mcp_update("c8"))
    assert json.loads(event.tool_input)["server"] == SERVER
    assert json.loads(event.tool_input)["tool"] == TOOL
