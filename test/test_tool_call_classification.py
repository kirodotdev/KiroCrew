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
        assert got.tool_identity_trusted is True
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


# ── KAS: the built-in's id is on the permission frame, not the tool_call ──────


def _kas_builtin_flow(
    tool_id: str,
    *,
    title: str = "Read File",
    kind: str = "read",
    tool_call_meta: dict | None = None,
    raw_input: dict | None = None,
    with_kind_cache: bool = True,
) -> AcpEvent:
    """A KAS built-in as the wire carries it (captured live, kiro-cli 2.24.0):
    the tool_call frame has a display title, a kind and ``_meta.kiro.toolOrigin``
    but NO ``toolName``; the permission request stamps ``_meta.kiro.toolId`` and
    carries NO ``kind``."""
    caches: dict[str, Any] = {
        "tool_input_cache": {},
        "shell_cache": {},
        "raw_params_cache": {},
        "mcp_server_name_cache": {},
        "tool_name_cache": {},
    }
    if with_kind_cache:
        caches["tool_kind_cache"] = {}
    parse_session_update(
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-kas",
            "title": title,
            "kind": kind,
            "status": "pending",
            "rawInput": raw_input if raw_input is not None else {"path": "/tmp/probe-note.txt"},
            "_meta": (
                tool_call_meta
                if tool_call_meta is not None
                else {"kiro": {"toolOrigin": "default"}}
            ),
        },
        **caches,
    )
    msg = JsonRpcMessage(
        id=41,
        method="session/request_permission",
        params={
            "sessionId": "s",
            "toolCall": {"toolCallId": "tc-kas", "status": "pending", "title": title},
            "options": [
                {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
                {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
            ],
            "_meta": {"kiro": {"toolId": tool_id}},
        },
    )
    event, _ = build_permission_event(msg, **caches)
    return event


class TestKasPermissionFrameTakesTheToolCallKind:
    """KAS's ``request_permission`` names no ``kind``; the tool_call did. Without
    the carry-over every KAS call reached the gate's kind-keyed tiers as ``""``,
    so the write-plane routing this PR adds for ``delete_file`` never fired on
    the one harness that has ``delete_file``."""

    @pytest.mark.parametrize("kind", ["read", "edit", "delete", "execute"])
    def test_the_tool_call_kind_reaches_the_permission_event(self, kind: str) -> None:
        event = _kas_builtin_flow("delete_file", title="Delete File", kind=kind)
        assert event.tool_kind == kind

    def test_without_the_cache_the_kind_is_empty_as_before(self) -> None:
        """The control: this is the shape KAS produced at the gate before."""
        event = _kas_builtin_flow(
            "delete_file", title="Delete File", kind="delete", with_kind_cache=False
        )
        assert event.tool_kind == ""

    def test_a_kind_the_permission_frame_states_is_not_overridden(self) -> None:
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
            "tool_kind_cache": {},
        }
        parse_session_update(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-1",
                "title": "t",
                "kind": "edit",
                "status": "pending",
                "rawInput": {"path": "/tmp/x"},
            },
            **caches,
        )
        msg = JsonRpcMessage(
            id=1,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {
                    "toolCallId": "tc-1",
                    "status": "pending",
                    "title": "t",
                    "kind": "read",
                },
                "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
            },
        )
        event, _ = build_permission_event(msg, **caches)
        assert event.tool_kind == "read"

    @pytest.mark.parametrize("bad_kind", [[], {}, 7, None, ["edit"]])
    def test_a_non_string_frame_kind_is_read_as_absent(self, bad_kind) -> None:
        """The frame's ``kind`` is an unvalidated harness value. A non-string is
        not stamped on the event -- it would reach the write-plane membership
        test unhashable and abort the turn -- so the cached tool_call kind
        fills the blank exactly as it does for a frame that omits the field, and
        without a cache the event carries ``""``. The gate then classifies."""
        from kiro_crew.platform.tool_paths import is_edit_call

        def frame(with_cache: bool) -> AcpEvent:
            caches: dict[str, Any] = {
                "tool_input_cache": {},
                "shell_cache": {},
                "raw_params_cache": {},
                "mcp_server_name_cache": {},
                "tool_name_cache": {},
            }
            if with_cache:
                caches["tool_kind_cache"] = {}
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-bad",
                    "title": "t",
                    "kind": "delete",
                    "status": "pending",
                    "rawInput": {"path": "/tmp/x"},
                },
                **caches,
            )
            msg = JsonRpcMessage(
                id=2,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {
                        "toolCallId": "tc-bad",
                        "status": "pending",
                        "title": "t",
                        "kind": bad_kind,
                    },
                    "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
                },
            )
            event, _ = build_permission_event(msg, **caches)
            return event

        with_cache = frame(True)
        assert with_cache.tool_kind == "delete"
        without = frame(False)
        assert without.tool_kind == ""
        assert is_edit_call(without.tool_kind, "") is False

    def test_a_placeholder_kind_is_not_cached(self) -> None:
        caches: dict[str, Any] = {"tool_kind_cache": {}}
        for update in (
            {"kind": "unknown"},
            {"kind": ""},
            {},  # absent: the builder's own "unknown" default
        ):
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-p",
                    "title": "t",
                    "status": "pending",
                    **update,
                },
                **caches,
            )
        assert caches["tool_kind_cache"] == {}

    def test_a_kas_delete_of_a_write_protected_config_is_refused_end_to_end(self) -> None:
        """The claim ``security.md`` makes, driven from the wire: a KAS
        ``delete_file`` permission frame (no kind, ``toolId`` only) targeting a
        write-protected config reaches the hook tier as a delete and is denied;
        the same frame without the kind carry-over is NOT (the pre-fix shape)."""
        from kiro_crew.hooks import TOOL_DENY, HookManager, HooksConfig, hook_gate_kwargs

        raw = {"targetFile": "~/.kiro/crew/config.json", "explanation": "tidy"}
        mgr = HookManager(HooksConfig.from_dict({}))

        event = _kas_builtin_flow("delete_file", title="Delete File", kind="delete", raw_input=raw)
        assert event.tool_kind == "delete"
        r = mgr.on_tool_call(event.title, session_key="cli_chat", **hook_gate_kwargs(event))
        assert r.action == TOOL_DENY
        assert "config.json" in r.reason

        before = _kas_builtin_flow(
            "delete_file", title="Delete File", kind="delete", raw_input=raw, with_kind_cache=False
        )
        assert before.tool_kind == ""
        r = mgr.on_tool_call(before.title, session_key="cli_chat", **hook_gate_kwargs(before))
        assert r.action != TOOL_DENY, "control: the pre-fix shape reached the human unrefused"


class TestTheKindCarryOverIsKasOnly:
    """kiro-cli's permission frames omit ``kind`` too, and its ``grep``/``glob``
    tool_calls carry ``kind: "search"`` -- not a read-only kind -- so carrying it
    over would let the kind veto the host read-only proof that auto-approves a
    kiro-cli search under ``--approval reads`` (harness parity H13). The cache
    therefore exists only on the KAS backend, by a positive backend test; the
    kiro-cli path is unchanged."""

    @staticmethod
    def _handle(acp_backend: str):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle

        rt = MagicMock()
        rt.acp_backend = acp_backend
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        rt.send_notification = AsyncMock()
        rt.supports_image_prompt = False
        return AcpSessionHandle("sA", asyncio.Queue(), rt)

    def test_the_kas_handle_hands_the_cache_to_the_wire(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        h = self._handle(ACP_BACKEND_KAS)
        assert h._tool_kind_cache_for_wire() is h._tool_call_kind

    @pytest.mark.parametrize("backend", ["", "claude", "codex"])
    def test_every_other_handle_hands_none_to_the_wire(self, backend: str) -> None:
        """Construction is identical for every harness (the attribute exists on
        all of them); only what the wire parsers are HANDED differs, per frame."""
        h = self._handle(backend)
        assert h._tool_call_kind == {}
        assert h._tool_kind_cache_for_wire() is None

    def test_a_runtime_double_without_a_backend_is_not_a_member(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle

        rt = MagicMock(spec=["pid", "is_alive", "send_notification", "supports_image_prompt"])
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        rt.send_notification = AsyncMock()
        rt.supports_image_prompt = False
        assert AcpSessionHandle("sA", asyncio.Queue(), rt)._tool_kind_cache_for_wire() is None

    def test_a_kiro_cli_search_stays_kindless_and_read_only_proven(self) -> None:
        """The regression the gate would have had: kiro-cli streams a ``search``
        tool_call then a kindless permission frame; with no cache (the kiro
        path) the event's kind stays ``""`` and the trusted identity proves the
        read; with the cache it would read ``search`` and the proof is vetoed."""
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, hook_gate_kwargs

        def flow(with_cache: bool) -> AcpEvent:
            caches: dict[str, Any] = {
                "tool_input_cache": {},
                "shell_cache": {},
                "raw_params_cache": {},
                "mcp_server_name_cache": {},
                "tool_name_cache": {},
            }
            if with_cache:
                caches["tool_kind_cache"] = {}
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-grep",
                    "title": "grep",
                    "kind": "search",
                    "status": "pending",
                    "rawInput": {"pattern": "TODO", "path": "/tmp/repo"},
                    "_meta": {"kiro": {"toolName": "grep", "mcpServerName": ""}},
                },
                **caches,
            )
            msg = JsonRpcMessage(
                id=5,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {"toolCallId": "tc-grep", "status": "pending", "title": "grep"},
                    "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
                },
            )
            event, _ = build_permission_event(msg, **caches)
            return event

        kiro_path = flow(with_cache=False)
        assert kiro_path.tool_kind == ""
        assert kiro_path.tool_name == "grep" and kiro_path.mcp_identity_trusted is True
        r = HookManager().on_tool_call(
            kiro_path.title, classifier_only=True, **hook_gate_kwargs(kiro_path)
        )
        assert r.action == TOOL_AUTO_APPROVE and r.read_only is True

        # The counterfactual the KAS-only gate exists to prevent on kiro-cli.
        with_cache = flow(with_cache=True)
        assert with_cache.tool_kind == "search"
        r = HookManager().on_tool_call(
            with_cache.title, classifier_only=True, **hook_gate_kwargs(with_cache)
        )
        assert r.action != TOOL_AUTO_APPROVE


class TestKasPermissionFrameCarriesTheBuiltinId:
    def test_tool_id_on_the_permission_frame_becomes_the_tool_name(self) -> None:
        """Without this the only identity the gate sees for a KAS built-in is the
        prose title, and a name-keyed deny (``fs_read``) matches nothing."""
        event = _kas_builtin_flow("read_file")
        assert event.tool_name == "read_file"
        assert event.mcp_server_name == ""

    def test_the_frame_id_cannot_satisfy_a_trust_gated_grant(self) -> None:
        """Server-keyed grants stay closed. The trusted pair the cache attests is
        ``("", "")`` -- a KAS built-in writes a hit with empty names -- so every
        grant keyed on a trusted SERVER still sees none and does not fire. (The id
        does reach the read-only proof, by design, where only a read-only
        allowlisted id can gain; see ``TestClassifierOnlyHostTrustedProof``.)"""
        from kiro_crew.acp._dispatch import identified_mcp_call
        from kiro_crew.hooks import event_is_spawn_run

        event = _kas_builtin_flow("spawn_run", title="spawn_run")
        assert event.tool_name == "spawn_run"
        assert event.mcp_server_name == ""
        assert event_is_spawn_run(event) is False
        assert identified_mcp_call(event) is None

    def test_a_cached_tool_name_wins_over_the_frame_id(self) -> None:
        """kiro-cli's ``toolName`` on the tool_call is the primary channel; the
        frame id fills only an EMPTY name."""
        event = _kas_builtin_flow("read_file", tool_call_meta={"kiro": {"toolName": "fs_read"}})
        assert event.tool_name == "fs_read"

    @pytest.mark.parametrize("bad", [7, "", "   ", None, ["read_file"]])
    def test_a_malformed_frame_id_is_ignored(self, bad) -> None:
        event = _kas_builtin_flow(bad)  # type: ignore[arg-type]
        assert event.tool_name == ""

    def test_a_frame_no_tool_call_preceded_is_not_named_by_its_raw_id(self) -> None:
        """The frame id FILLS a name the preceding tool_call left empty; it does not
        invent one. A request whose toolCallId misses the identity cache -- a
        sub-agent spawn, which KAS sends with no tool_call frame -- is classified
        by ``kas_consent_tool`` alone: a verified consent block yields the Crew
        name rules are written against, a disagreeing one yields nothing, and the
        raw spawn-family id never reaches the gate as a spelling no rule carries."""
        from kiro_crew.acp._dispatch import build_permission_event

        def frame(kiro: dict[str, Any]) -> AcpEvent:
            msg = JsonRpcMessage(
                id=42,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {
                        "toolCallId": "invoke_subagent_toolu_01",
                        "status": "pending",
                        "title": "Sub-agent: my-research",
                    },
                    "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
                    "_meta": {"kiro": kiro},
                },
            )
            event, _ = build_permission_event(
                msg,
                tool_input_cache={},
                shell_cache={},
                raw_params_cache={},
                mcp_server_name_cache={},
                tool_name_cache={},
                kas_consent_meta=True,
            )
            return event

        verified = frame(
            {
                "toolId": "invoke_sub_agent",
                "consent": {"capability": "subagent", "resource": "my-research"},
            }
        )
        assert verified.tool_name == "use_subagent"
        disagreeing = frame({"toolId": "invoke_sub_agent", "consent": {"capability": "shell"}})
        assert disagreeing.tool_name == ""
        # And a built-in id with no preceding tool_call is not named either: the
        # id is trusted as the tool_call's complement, not on its own.
        assert frame({"toolId": "read_file"}).tool_name == ""

    def test_a_kiro_cli_fs_read_deny_binds_to_a_kas_read_file_permission(self) -> None:
        """End to end through the gate: the rule an operator wrote for kiro-cli
        denies the KAS built-in that does the same work, from the frame alone."""
        event = _kas_builtin_flow("read_file")
        from kiro_crew.hooks import HooksConfig

        mgr = HookManager(HooksConfig(auto_deny_tools=["fs_read"]))
        decision = mgr.on_tool_call(
            event.title,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
        )
        assert decision.action == TOOL_DENY


class TestKasMcpServedIdentity:
    """A KAS MCP-served call as the wire carries it (captured live, kiro-cli 2.24.0,
    a stdio server whose one tool is named ``read_file`` on purpose): the
    tool_call frame stamps ``_meta.kiro.serverName`` and no tool name; the
    permission request stamps ``_meta.kiro.mcpTool.identity`` and a
    ``toolId`` of ``mcp_<server>_<tool>``. Both must read as an MCP call with a
    server -- never as the engine's own ``read_file``."""

    _TOOL_CALL = {
        "sessionUpdate": "tool_call",
        "toolCallId": "tc-mcp",
        "title": "@probefs/read_file",
        "kind": "other",
        "status": "pending",
        "rawInput": {"path": "/tmp/probe-note.txt"},
        "_meta": {"kiro": {"serverName": "probefs", "toolOrigin": "client"}},
    }
    _PERMISSION_META = {
        "kiro": {
            "toolId": "mcp_probefs_read_file",
            "agentManagesTrust": True,
            "consent": {"capability": "mcp", "resource": "probefs/read_file"},
            "mcpTool": {
                "version": 1,
                "identity": {"serverName": "probefs", "toolName": "read_file"},
            },
        }
    }

    def _flow(self) -> AcpEvent:
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        parse_session_update(dict(self._TOOL_CALL), **caches)
        msg = JsonRpcMessage(
            id=43,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {
                    "toolCallId": "tc-mcp",
                    "status": "pending",
                    "title": "@probefs/read_file",
                },
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": self._PERMISSION_META,
            },
        )
        event, _ = build_permission_event(msg, **caches)
        return event

    def test_the_tool_call_frame_names_the_server(self) -> None:
        identity = classify_tool_call(dict(self._TOOL_CALL))
        assert identity.mcp_server_name == "probefs"
        assert identity.is_shell is False

    def test_the_permission_event_carries_the_canonical_pair(self) -> None:
        event = self._flow()
        assert (event.mcp_server_name, event.tool_name) == ("probefs", "read_file")

    def test_an_mcp_read_file_is_not_the_builtin_for_the_gate(self) -> None:
        """The reason the server must be read: a built-in ``fs_read`` deny must not
        bind to a server's ``read_file``, and the host read-only proof must not
        treat it as the engine's own."""
        from kiro_crew.hooks import HooksConfig, _is_host_read_only_builtin

        event = self._flow()
        assert (
            _is_host_read_only_builtin(
                event.tool_name, event.mcp_server_name, mcp_identity_trusted=True
            )
            is False
        )
        decision = HookManager(HooksConfig(auto_deny_tools=["fs_read"])).on_tool_call(
            event.title,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
        )
        assert decision.action != TOOL_DENY

    def test_a_per_server_rule_binds_through_the_pair(self) -> None:
        from kiro_crew.hooks import HooksConfig

        event = self._flow()
        decision = HookManager(HooksConfig(auto_deny_tools=["@probefs/read_file"])).on_tool_call(
            event.title,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
        )
        assert decision.action == TOOL_DENY

    def test_a_kiro_cli_frame_still_reads_through_the_first_channel(self) -> None:
        identity = classify_tool_call(
            {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc-k",
                "title": "Look up weather",
                "kind": "other",
                "_meta": {"kiro": {"mcpServerName": "weather", "toolName": "forecast"}},
            }
        )
        assert (identity.mcp_server_name, identity.tool_name) == ("weather", "forecast")
        assert identity.identity_trusted is True
