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
    the one harness that has ``delete_file``. Only the WRITE-PLANE kinds are
    carried: the other kinds KAS stamps (recorded in
    ``test/fixtures/kas_builtin_tool_ids.json`` from the engine's kind map) are
    left ``""`` so a KAS search keeps the read-only proof it has by identity."""

    @pytest.mark.parametrize("kind", ["edit", "delete"])
    def test_a_write_plane_tool_call_kind_reaches_the_permission_event(self, kind: str) -> None:
        event = _kas_builtin_flow("delete_file", title="Delete File", kind=kind)
        assert event.tool_kind == kind

    @pytest.mark.parametrize("kind", ["read", "search", "execute", "fetch", "other"])
    def test_a_non_write_plane_kind_is_not_carried(self, kind: str) -> None:
        """Every kind KAS stamps outside the write plane stays off the permission
        event -- ``search`` would veto the host read-only proof, ``read`` would
        widen the interactive allow-list on a field the frame never stated."""
        event = _kas_builtin_flow("grep_search", title="Grep Search", kind=kind)
        assert event.tool_kind == ""

    def test_the_carried_set_is_exactly_the_write_plane_and_the_fixture_agrees(self) -> None:
        """The exclusion is measured, not assumed: the fixture records the kind
        the engine stamps on every registered built-in, the write-plane kinds
        appear there only on the write tools, and ``search`` is really what the
        engine stamps on its three search-shaped reads."""
        import json
        from pathlib import Path

        from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS

        reg = json.loads(
            (Path(__file__).parent / "fixtures" / "kas_builtin_tool_ids.json").read_text()
        )
        kinds = {k: v for k, v in reg["tool_call_kind"].items() if not k.startswith("_")}
        assert set(kinds) == set(reg["registered"])
        carried = {tool for tool, kind in kinds.items() if kind in WRITE_PLANE_KINDS}
        assert carried == {"delete_file", "fs_append", "fs_write", "str_replace"}
        assert {kinds[t] for t in ("grep_search", "file_search", "list_directory")} == {"search"}
        assert kinds["read_file"] == "read"

    def test_a_kas_search_still_auto_approves_under_the_read_only_proof(self) -> None:
        """The First-Principles condition, driven from the wire with the cache
        PRESENT: a KAS ``grep_search`` tool_call stamps ``search``; its kindless
        permission frame must still auto-approve under ``--approval reads`` by
        the identity fold (``grep_search`` -> ``grep``), which a carried
        ``search`` would veto."""
        from kiro_crew.hooks import TOOL_AUTO_APPROVE, HookManager, hook_gate_kwargs

        for kas_id, title in (
            ("grep_search", "Grep Search"),
            ("file_search", "File Search"),
            ("list_directory", "List Directory"),
        ):
            event = _kas_builtin_flow(
                kas_id,
                title=title,
                kind="search",
                raw_input={"pattern": "TODO", "path": "/tmp/repo"},
            )
            assert event.tool_kind == "", kas_id
            r = HookManager().on_tool_call(
                event.title, classifier_only=True, **hook_gate_kwargs(event)
            )
            assert r.action == TOOL_AUTO_APPROVE and r.read_only is True, kas_id

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

    def test_the_cache_retains_only_the_closed_write_plane_vocabulary(self) -> None:
        """The frame's ``kind`` is an externally-sized string. Only membership in
        ``WRITE_PLANE_KINDS`` is retained, so a stored entry is one of those
        literals and never the frame's own bytes: a 10 MiB ``kind`` per unique
        toolCallId retains nothing, and ``read``/``search``/``execute``/
        ``fetch``/``other`` (kinds the reader never carries) are not written
        either -- growth with no reader."""
        from kiro_crew.platform.tool_paths import WRITE_PLANE_KINDS

        caches: dict[str, Any] = {"tool_kind_cache": {}}
        huge = "k" * (1024 * 1024)
        for i, kind in enumerate(
            [huge, "read", "search", "execute", "fetch", "other", "edit", "delete", "editx"]
        ):
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": f"tc-{i}",
                    "title": "t",
                    "status": "pending",
                    "kind": kind,
                },
                **caches,
            )
        stored = caches["tool_kind_cache"]
        assert set(stored.values()) <= WRITE_PLANE_KINDS
        assert sorted(stored.values()) == ["delete", "edit"]
        assert sum(len(v) for v in stored.values()) < 32


class TestTheProvenanceCachesShareOneBound:
    """The per-turn provenance caches are keyed on the same scoped id and written
    for the same frames, so they are ONE population with ONE admission: a count
    bound over the union of their keys and a size bound on the one externally
    sized field a row carries. A refused frame is written to none of them, its
    scope is recorded, and the handle refuses the permission request for it."""

    @staticmethod
    def _caches() -> dict[str, Any]:
        return {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
            "diff_path_cache": {},
            "tool_kind_cache": {},
            "tool_input_redacted_cache": {},
            "cache_overflow": set(),
        }

    @staticmethod
    def _over(*scopes: str) -> set[str]:
        """The overflow record for *scopes*: fixed-size digests, never the text."""
        from kiro_crew.acp._dispatch import overflow_scope_key

        return {overflow_scope_key(sc) for sc in scopes}

    @staticmethod
    def _call(i: int, **extra: Any) -> dict[str, Any]:
        return {
            "sessionUpdate": "tool_call",
            "toolCallId": f"tc-{i}",
            "title": "Write File",
            "kind": "edit",
            "status": "pending",
            "rawInput": {"path": f"/tmp/f{i}", "text": "x"},
            "_meta": {"kiro": {"toolName": "fs_write", "mcpServerName": ""}},
            **extra,
        }

    @staticmethod
    def _dicts(caches: dict[str, Any]) -> list[dict]:
        return [c for c in caches.values() if isinstance(c, dict)]

    def test_the_count_cap_is_one_constant_over_the_union_of_sibling_keys(self, caplog) -> None:
        import logging

        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        caches = self._caches()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES):
            parse_session_update(self._call(i), cache_scope="s", **caches)
        keys: set[str] = set()
        for c in self._dicts(caches):
            keys.update(c.keys())
        assert len(keys) == TOOL_CALL_CACHE_MAX_ENTRIES
        assert caches["cache_overflow"] == set()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
            parse_session_update(self._call(TOOL_CALL_CACHE_MAX_ENTRIES), cache_scope="s", **caches)
            parse_session_update(
                self._call(TOOL_CALL_CACHE_MAX_ENTRIES + 1), cache_scope="s", **caches
            )
        over = f"s|tc-{TOOL_CALL_CACHE_MAX_ENTRIES}"
        assert all(over not in c for c in self._dicts(caches)), "a refused id got a row"
        assert caches["cache_overflow"] == self._over("s")
        # Overflow is said once per scope, not once per refused frame.
        assert sum("refused a frame" in r.getMessage() for r in caplog.records) == 1

    def test_overflow_is_said_again_on_the_next_snapshot(self, caplog) -> None:
        """The warning is deduplicated against the caller-owned overflow record,
        which the turn reset clears with the caches -- NOT against anything that
        outlives the turn. A scope that overflows on two consecutive snapshots
        is said out loud on both; within one snapshot it is said once."""
        import logging

        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        def overflow_once(caches: dict[str, Any]) -> int:
            caplog.clear()
            with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
                for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 2):
                    parse_session_update(self._call(i), cache_scope="s", **caches)
            return sum("refused a frame" in r.getMessage() for r in caplog.records)

        caches = self._caches()
        assert overflow_once(caches) == 1
        assert caches["cache_overflow"] == self._over("s")
        # Same snapshot, same scope, more refused frames: still said once.
        assert overflow_once(caches) == 0
        # The per-turn reset clears the record with the caches; the next
        # snapshot's overflow is a new fact and is reported again.
        for c in caches.values():
            if isinstance(c, (dict, set)):
                c.clear()
        assert overflow_once(caches) == 1
        assert caches["cache_overflow"] == self._over("s")

    def test_an_oversized_payload_is_refused_whether_the_id_is_new_or_held(self) -> None:
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        caches = self._caches()
        big = "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1)
        parse_session_update(self._call(1, rawInput={"path": "/tmp/f1", "text": big}), **caches)
        assert all("tc-1" not in c for c in self._dicts(caches))
        assert caches["cache_overflow"] == self._over("")
        # A small first frame does not reserve a row a huge refinement then fills.
        caches = self._caches()
        parse_session_update(self._call(2), **caches)
        assert caches["raw_params_cache"]["tc-2"]["text"] == "x"
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "rawInput": {"path": "/tmp/f2", "text": big},
            },
            **caches,
        )
        # The refused refinement evicts the held row: a stale target must not
        # stay trusted for the permission request that follows.
        assert all("tc-2" not in c for c in self._dicts(caches))
        assert caches["cache_overflow"] == self._over("")

    def test_a_large_params_object_beside_a_small_final_rendering_is_measured_on_the_params(
        self,
    ) -> None:
        """The converse hole: a diff block REPLACES the params rendering with a
        usually smaller string, but the row retains the ``raw_params`` dict too.
        A near-limit ``rawInput`` beside a tiny diff must be refused on the
        params' size, on the initial frame and on a refinement of a held id;
        otherwise 256 such frames retain gigabytes the gate never measured."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        big = {"path": "/tmp/f1", "text": "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1)}
        tiny = [{"type": "diff", "path": "/tmp/f1", "oldText": "a", "newText": "b"}]
        caches = self._caches()
        parse_session_update(self._call(1, rawInput=big, content=tiny), **caches)
        assert all("tc-1" not in c for c in self._dicts(caches)), "unmeasured params retained"
        assert caches["cache_overflow"] == self._over("")
        # Refinement of a held id: the earlier row is evicted, not widened.
        refined = self._caches()
        parse_session_update(self._call(2, content=tiny), **refined)
        assert refined["raw_params_cache"]["tc-2"]["path"] == "/tmp/f2"
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "title": "Write File",
                "rawInput": big,
                "content": tiny,
            },
            **refined,
        )
        assert all("tc-2" not in c for c in self._dicts(refined)), "unmeasured params retained"
        # The same params beside a small diff, one under the bound, are admitted,
        # so the refusal is the params' size and not the diff block's presence.
        ok = self._caches()
        fits = {"path": "/tmp/f3", "text": "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS // 2)}
        parse_session_update(self._call(3, rawInput=fits, content=tiny), **ok)
        assert ok["raw_params_cache"]["tc-3"]["path"] == "/tmp/f3"
        assert ok["cache_overflow"] == set()

    def test_the_size_bound_measures_the_final_rendering_not_the_params(self) -> None:
        """A small ``rawInput`` beside a large diff -- the content block, or the
        edit diff derived from an ``insert`` body -- must not pass admission on
        the params' size and then retain the diff unmeasured: the bound is
        applied to the string the cache retains, after diff rendering and
        redaction, in both builders."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        big = "x\n" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS // 2 + 1)
        # Derived diff: an ``insert`` renders its body line-by-line without the
        # unified-diff length cap, so the retained string is the body's size.
        caches = self._caches()
        parse_session_update(
            self._call(
                1,
                rawInput={"path": "/tmp/f1", "command": "insert", "insertLine": 0, "fileText": big},
            ),
            **caches,
        )
        assert all("tc-1" not in c for c in self._dicts(caches))
        assert caches["cache_overflow"] == self._over("")
        # Diff content block on the initial frame.
        caches = self._caches()
        parse_session_update(
            self._call(
                2,
                rawInput={"path": "/tmp/f2"},
                content=[{"type": "diff", "path": "/tmp/f2", "oldText": "", "newText": big}],
            ),
            **caches,
        )
        # The unified-diff renderer caps its output, so this frame is admitted
        # -- and what is retained is the capped rendering, never the block.
        assert len(caches["tool_input_cache"]["tc-2"]) <= TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS
        # Refinement carrying an oversized derived rendering of a held id.
        caches = self._caches()
        parse_session_update(self._call(3), **caches)
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "rawInput": {"path": "/tmp/f3", "text": big},
            },
            **caches,
        )
        assert all("tc-3" not in c for c in self._dicts(caches)), "stale row survived"

    def test_a_small_params_object_beside_a_larger_final_rendering_is_measured_on_the_rendering(
        self, monkeypatch
    ) -> None:
        """The discriminating case: params well under the bound, the diff
        rendered from the content block over it. With the bound lowered below
        the unified-diff renderer's own cap, the only way the frame is refused
        is if the gate measured the FINAL string; a gate that measured the
        params rendering admits it and retains the diff unmeasured."""
        from kiro_crew.acp import _dispatch

        monkeypatch.setattr(_dispatch, "TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS", 2_000)
        new_text = "line\n" * 1_000  # renders to ~6 KiB of diff, under the 64 KiB cap
        # Initial frame: rawInput of ~20 chars, diff block of ~5 KiB.
        caches = self._caches()
        parse_session_update(
            self._call(
                1,
                rawInput={"path": "/tmp/f1"},
                content=[{"type": "diff", "path": "/tmp/f1", "oldText": "", "newText": new_text}],
            ),
            **caches,
        )
        assert all("tc-1" not in c for c in self._dicts(caches))
        assert caches["cache_overflow"] == self._over("")
        # Refinement: the held id's small first frame, then a refinement whose
        # rawInput is tiny but whose diff block renders over the bound.
        caches = self._caches()
        parse_session_update(self._call(2, rawInput={"path": "/tmp/f2"}), **caches)
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-2",
                "rawInput": {"path": "/tmp/f2"},
                "content": [
                    {"type": "diff", "path": "/tmp/f2", "oldText": "", "newText": new_text}
                ],
            },
            **caches,
        )
        assert all("tc-2" not in c for c in self._dicts(caches)), "stale row survived"
        assert caches["cache_overflow"] == self._over("")

    def test_every_retained_rendering_is_within_the_size_bound(self) -> None:
        """Property over the retained display strings: nothing in
        ``tool_input_cache`` ever exceeds the bound, whatever the frame shape."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        big = "y" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 5)
        caches = self._caches()
        frames = [
            self._call(1, rawInput={"path": "/tmp/a", "command": "create", "fileText": big}),
            self._call(
                2,
                rawInput={"path": "/tmp/b", "command": "strReplace", "oldStr": "a", "newStr": big},
            ),
            self._call(
                3,
                rawInput={"path": "/tmp/c", "command": "insert", "insertLine": 1, "fileText": big},
            ),
            self._call(4, rawInput={"path": "/tmp/d"}),
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-4",
                "rawInput": {
                    "path": "/tmp/d",
                    "command": "insert",
                    "insertLine": 1,
                    "fileText": big,
                },
            },
        ]
        for frame in frames:
            parse_session_update(frame, **caches)
        assert all(
            len(v) <= TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS for v in caches["tool_input_cache"].values()
        )

    def test_a_refinement_of_a_held_id_is_admitted_past_the_count_cap(self) -> None:
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        caches = self._caches()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES):
            parse_session_update(self._call(i), **caches)
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-0",
                "rawInput": {"path": "/tmp/f0-refined", "text": "y"},
            },
            **caches,
        )
        assert caches["raw_params_cache"]["tc-0"]["path"] == "/tmp/f0-refined"
        assert caches["cache_overflow"] == set()

    def test_an_oversized_tool_call_id_is_refused_as_overflow(self) -> None:
        """The scoped key is a retained string too: a ``toolCallId`` past the key
        bound is written nowhere and its scope is recorded, while one exactly at
        the bound is admitted. Otherwise a stream of near-limit ids spends the
        count cap on key bytes the size bound never measured."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_KEY_CHARS

        caches = self._caches()
        at_bound = "k" * TOOL_CALL_CACHE_MAX_KEY_CHARS
        over = "k" * (TOOL_CALL_CACHE_MAX_KEY_CHARS + 1)
        parse_session_update(self._call(1, toolCallId=at_bound), **caches)
        assert at_bound in caches["raw_params_cache"]
        parse_session_update(self._call(2, toolCallId=over), **caches)
        assert all(over not in c for c in self._dicts(caches)), "an oversized id got a row"
        assert caches["cache_overflow"] == self._over("")
        # The scope is part of the key: a scope that pushes an in-bound id over
        # the bound is refused the same way (the key is what is stored).
        scoped = self._caches()
        parse_session_update(self._call(3, toolCallId=at_bound), cache_scope="s", **scoped)
        assert all(f"s|{at_bound}" not in c for c in self._dicts(scoped))
        assert scoped["cache_overflow"] == self._over("s")
        # A refinement naming an oversized id is refused too, not just the first frame.
        refined = self._caches()
        parse_session_update(self._call(4), **refined)
        parse_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": over, "rawInput": {"a": 1}},
            **refined,
        )
        assert all(over not in c for c in self._dicts(refined))
        assert refined["cache_overflow"] == self._over("")

    def test_a_non_string_tool_call_id_is_not_a_key(self) -> None:
        """A ``toolCallId`` that is not a string (a list, a dict) never becomes a
        cache key by stringification: the initial frame retains nothing under it
        and the refinement is dropped, as a missing id is."""
        caches = self._caches()
        parse_session_update(self._call(1, toolCallId=["tc", "1"]), **caches)
        assert all(not c for c in self._dicts(caches))
        parse_session_update(
            {"sessionUpdate": "tool_call_update", "toolCallId": {"id": 1}, "rawInput": {"a": 1}},
            **caches,
        )
        assert all(not c for c in self._dicts(caches))
        assert caches["cache_overflow"] == set()

    def test_an_oversized_diff_path_is_refused_as_overflow(self) -> None:
        """``diff_path_cache`` retains the frame's diff-block ``path`` verbatim,
        so a path past the path bound refuses the frame -- initial and
        refinement alike -- rather than being retained or cut; a path at the
        bound is retained whole, and a non-string path is no path."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PATH_CHARS

        def _diff(path: object) -> list[dict]:
            return [{"type": "diff", "path": path, "oldText": "a", "newText": "b"}]

        caches = self._caches()
        at_bound = "/" + "p" * (TOOL_CALL_CACHE_MAX_PATH_CHARS - 1)
        over = "/" + "p" * TOOL_CALL_CACHE_MAX_PATH_CHARS
        parse_session_update(self._call(1, content=_diff(at_bound)), **caches)
        assert caches["diff_path_cache"]["tc-1"] == at_bound
        parse_session_update(self._call(2, content=_diff(over)), **caches)
        assert all("tc-2" not in c for c in self._dicts(caches)), "an oversized path got a row"
        assert caches["cache_overflow"] == self._over("")
        # A held id's refinement carrying an oversized path is refused, and the
        # row it already had is evicted rather than kept as a stale target.
        refined = self._caches()
        parse_session_update(self._call(3, content=_diff("/tmp/f3")), **refined)
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-3",
                "title": "Write File",
                "content": _diff(over),
            },
            **refined,
        )
        assert all("tc-3" not in c for c in self._dicts(refined)), "stale row survived"
        assert refined["cache_overflow"] == self._over("")
        # A non-string path names no file: nothing is retained as the target.
        odd = self._caches()
        parse_session_update(self._call(4, content=_diff(["/tmp", "f4"])), **odd)
        assert "tc-4" not in odd["diff_path_cache"]
        assert "tc-4" in odd["raw_params_cache"]

    def test_every_retained_key_and_path_is_within_its_bound(self) -> None:
        """Property over the retained keys and diff paths, beside the payload
        property: whatever the frames carried, no key exceeds the key bound and
        no retained path exceeds the path bound."""
        from kiro_crew.acp._dispatch import (
            TOOL_CALL_CACHE_MAX_KEY_CHARS,
            TOOL_CALL_CACHE_MAX_PATH_CHARS,
        )

        caches = self._caches()
        long_id = "i" * (TOOL_CALL_CACHE_MAX_KEY_CHARS * 2)
        long_path = "/" + "q" * (TOOL_CALL_CACHE_MAX_PATH_CHARS * 2)
        frames = [
            self._call(1),
            self._call(2, toolCallId=long_id),
            self._call(
                3, content=[{"type": "diff", "path": long_path, "oldText": "", "newText": "z"}]
            ),
            self._call(4, toolCallId="ok-" + long_id[:8]),
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "title": "Write File",
                "content": [{"type": "diff", "path": long_path, "oldText": "", "newText": "z"}],
            },
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": long_id,
                "rawInput": {"path": "/tmp/x"},
            },
        ]
        for frame in frames:
            parse_session_update(frame, cache_scope="scope", **caches)
        for c in self._dicts(caches):
            assert all(len(k) <= TOOL_CALL_CACHE_MAX_KEY_CHARS for k in c)
        assert all(
            len(p) <= TOOL_CALL_CACHE_MAX_PATH_CHARS for p in caches["diff_path_cache"].values()
        )
        assert caches["cache_overflow"] == self._over("scope")

    def test_a_refused_id_s_permission_event_carries_no_provenance(self) -> None:
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        caches = self._caches()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 1):
            parse_session_update(self._call(i), **caches)
        over = f"tc-{TOOL_CALL_CACHE_MAX_ENTRIES}"
        msg = JsonRpcMessage(
            id=9,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {"toolCallId": over, "status": "pending", "title": "Write File"},
                "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
            },
        )
        perm_caches = {k: v for k, v in caches.items() if k != "cache_overflow"}
        event, _ = build_permission_event(msg, **perm_caches)
        assert event.raw_params_trusted is False
        assert event.shell_classified is False
        assert event.mcp_identity_trusted is False
        assert event.tool_kind == ""

    @staticmethod
    def _handle():
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        rt = MagicMock()
        rt.acp_backend = ACP_BACKEND_KAS
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        rt.send_notification = AsyncMock()
        rt.supports_image_prompt = False
        sent: list[tuple] = []

        async def _send_response(request_id, payload):
            sent.append((request_id, payload))

        rt.send_response = _send_response
        return AcpSessionHandle("s-1", asyncio.Queue(), rt), sent

    @staticmethod
    def _frame(update: dict[str, Any]) -> JsonRpcMessage:
        return JsonRpcMessage(
            method="session/update", params={"sessionId": "s-1", "update": update}
        )

    @staticmethod
    def _perm(rid: int, tool_call_id: str) -> JsonRpcMessage:
        return JsonRpcMessage(
            id=rid,
            method="session/request_permission",
            params={
                "sessionId": "s-1",
                "toolCall": {
                    "toolCallId": tool_call_id,
                    "status": "pending",
                    "title": "Delete File",
                },
                "options": [
                    {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
                ],
                "_meta": {"kiro": {"toolId": "delete_file"}},
            },
        )

    @pytest.mark.asyncio
    async def test_the_handle_refuses_a_permission_request_the_caches_never_retained(
        self, caplog
    ) -> None:
        """GPT's scenario: a mounted KAS ``delete_file`` after the cap is reached.
        Its frame is written nowhere, so the permission event has no kind, no
        identity and no target; instead of yielding that to a consumer (whose
        AUTO_APPROVE path would judge the title alone) the handle refuses it.
        The refusal names both bounds, so an operator reading it alone knows
        which limit the call can meet."""
        import logging

        from kiro_crew.acp._dispatch import (
            TOOL_CALL_CACHE_MAX_ENTRIES,
            TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
        )

        handle, sent = self._handle()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 1):
            handle._handle_update(self._frame(self._call(i)))
        over = f"tc-{TOOL_CALL_CACHE_MAX_ENTRIES}"
        msg = self._perm(21, over)
        event = handle._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is False
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.session_handle"):
            assert await handle._refuse_unretained_provenance(msg, event) is True
        assert sent == [(21, {"outcome": {"outcome": "selected", "optionId": "reject"}})]
        refusal = [r.getMessage() for r in caplog.records if "did not retain" in r.getMessage()]
        assert len(refusal) == 1
        assert str(TOOL_CALL_CACHE_MAX_ENTRIES) in refusal[0]
        assert str(TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS) in refusal[0]

    @pytest.mark.asyncio
    async def test_a_retained_id_and_a_scope_that_never_overflowed_are_untouched(self) -> None:
        handle, sent = self._handle()
        handle._handle_update(self._frame(self._call(0)))
        msg = self._perm(22, "tc-0")
        event = handle._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is True
        assert await handle._refuse_unretained_provenance(msg, event) is False
        # An unknown id on a scope the bound never refused is the pre-existing
        # cache-miss shape and goes to the consumer as before.
        msg2 = self._perm(23, "never-seen")
        event2 = handle._build_permission_event(msg2)
        assert event2 is not None and event2.raw_params_trusted is False
        assert await handle._refuse_unretained_provenance(msg2, event2) is False
        assert sent == []

    def test_a_terminal_result_releases_the_call_s_provenance_from_every_store(self) -> None:
        """A settled call's rows are dead weight: the permission request preceded
        execution and nothing reads them afterwards, so the caches hold calls IN
        FLIGHT and the count bound stops being a bound on turn length."""
        handle, _ = self._handle()
        handle._handle_update(self._frame(self._call(0, rawInput={"targetFile": "/tmp/a"})))
        key = "s-1|tc-0"
        stores = (
            handle._tool_call_inputs,
            handle._tool_call_input_redacted,
            handle._tool_call_is_shell,
            handle._tool_call_raw_params,
            handle._tool_call_kind,
            handle._tool_call_tool_name,
        )
        assert all(key in st for st in stores), [key in st for st in stores]
        # A non-terminal refinement releases nothing.
        handle._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": "in_progress"},
            "s-1",
        )
        assert all(key in st for st in stores)
        # A terminal frame under ANOTHER scope releases nothing -- a child's
        # terminal must not release a parent's row.
        handle._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": "completed"},
            "child-scope",
        )
        assert all(key in st for st in stores)
        for status in ("completed", "failed", "cancelled", "canceled", "refused"):
            handle._handle_update(self._frame(self._call(0, rawInput={"targetFile": "/tmp/a"})))
            handle._release_settled_tool_call(
                {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": status},
                "s-1",
            )
            assert not any(key in st for st in stores), status
            assert (
                key not in handle._tool_call_mcp_server and key not in handle._tool_call_diff_path
            )
        # A stray terminal for an id no store holds is a no-op.
        handle._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "never", "status": "completed"},
            "s-1",
        )
        # A first-class harness's handle holds its rows until the turn's clear(),
        # exactly as on main -- the release is the KAS admission's (H13).
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle

        rt = MagicMock()
        rt.acp_backend = "kiro"
        rt.pid = None
        rt.is_alive = MagicMock(return_value=True)
        rt.send_notification = AsyncMock()
        rt.supports_image_prompt = False
        kiro = AcpSessionHandle("s-1", asyncio.Queue(), rt)
        kiro._handle_update(self._frame(self._call(0, rawInput={"targetFile": "/tmp/a"})))
        assert key in kiro._tool_call_raw_params
        kiro._release_settled_tool_call(
            {"sessionUpdate": "tool_call_update", "toolCallId": "tc-0", "status": "completed"},
            "s-1",
        )
        assert key in kiro._tool_call_raw_params and key in kiro._tool_call_is_shell

    @pytest.mark.asyncio
    async def test_a_kas_turn_past_the_cap_keeps_working_when_its_calls_settle(self) -> None:
        """Opus's scenario, the other way round: three hundred calls in one turn,
        each settled by its terminal frame before the next, never overflow -- the
        population is one call in flight -- so the 300th call's permission
        request is admitted with full provenance, not refused."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        handle, sent = self._handle()
        rt = handle._runtime
        frames: list[JsonRpcMessage] = []
        n = TOOL_CALL_CACHE_MAX_ENTRIES + 44
        for i in range(n):
            frames.append(self._frame(self._call(i, rawInput={"targetFile": f"/tmp/f{i}"})))
            frames.append(
                self._frame(
                    {
                        "sessionUpdate": "tool_call_update",
                        "toolCallId": f"tc-{i}",
                        "status": "completed",
                    }
                )
            )
        queue = handle._queue

        async def _send_request(method, params):
            for f in frames:
                queue.put_nowait(f)
            queue.put_nowait(JsonRpcMessage(id=7, result={"stopReason": "end_turn"}))
            return 7

        rt.send_request = _send_request
        rt.mark_turn_active = lambda *_a, **_k: None
        events = [ev async for ev in handle.stream_command("/noop", timeout=5.0)]
        assert events, "the scripted turn produced events"
        assert handle._tool_call_cache_overflow == set()
        assert handle._tool_call_raw_params == {}
        # The next call is admitted with full provenance, as the first was.
        handle._handle_update(self._frame(self._call(n, rawInput={"targetFile": "/tmp/last"})))
        msg = self._perm(31, f"tc-{n}")
        event = handle._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is True
        assert event.tool_kind == "edit", "the tool_call's write-plane kind was carried"
        assert await handle._refuse_unretained_provenance(msg, event) is False
        assert sent == []

    def test_the_liveness_map_holds_only_admitted_calls(self) -> None:
        """The liveness map and the provenance caches bound one population: a
        frame the admission refused (its permission request will be rejected as
        unverifiable) is not attributed in the map, so it can never evict an
        admitted call that may still be running. The frame before the cap is
        the one the oracle keeps tracking."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_ENTRIES

        handle, _ = self._handle()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 1):
            handle._handle_update(self._frame(self._call(i, rawInput={"targetFile": f"/tmp/{i}"})))
        last = f"tc-{TOOL_CALL_CACHE_MAX_ENTRIES}"
        assert f"s-1|{last}" not in handle._tool_call_tool_name, "the admission refused it"
        assert last not in handle._active_tool_calls
        assert len(handle._active_tool_calls) == TOOL_CALL_CACHE_MAX_ENTRIES
        assert handle._active_tool_calls_evicted is False
        assert handle._inflight_tool_call_id == f"tc-{TOOL_CALL_CACHE_MAX_ENTRIES - 1}"
        assert "tc-0" in handle._active_tool_calls, "no admitted call was evicted"

    def test_the_release_runs_after_the_tripwire_and_at_every_parse_site(self) -> None:
        """Pin by source: the main route releases inside the same loop as the
        spec-disabled tripwire and AFTER it (the tripwire reads ``raw_params``
        at the terminal), and each side-effects-only parse site releases right
        after its ``parse_session_update``."""
        import inspect

        from kiro_crew.acp import session_handle

        src = inspect.getsource(session_handle.AcpSessionHandle._dispatch_events)
        trip = src.index("self._tripwire_spec_disabled_tool(ev, msg)")
        rel = src.index("self._release_settled_tool_call(")
        assert trip < rel < src.index("yield ev", trip)
        whole = inspect.getsource(session_handle)
        assert whole.count("self._release_settled_tool_call(") == 4
        for scope in ("upd, ssid)", "update, self._session_id)", "update, frame_sid)"):
            assert f"self._release_settled_tool_call({scope}" in whole, scope

    def test_a_native_child_s_terminal_releases_its_own_rows(self) -> None:
        """A child-routed frame writes the shared stores under the child's scope
        and re-tags only calls and text, so its terminal never reaches the main
        route's release: the child arm releases itself, and only its own row."""
        from kiro_crew.acp.types import EVENT_SUBAGENT_ACTIVITY

        handle, _ = self._handle()
        handle._handle_update(self._frame(self._call(0, rawInput={"targetFile": "/tmp/a"})))
        child = JsonRpcMessage(
            method="session/update",
            params={"sessionId": "child-1", "update": self._call(7, rawInput={"path": "/tmp/c"})},
        )
        out = handle._handle_update(child)
        assert [ev.kind for ev in out] == [EVENT_SUBAGENT_ACTIVITY]
        assert "child-1|tc-7" in handle._tool_call_raw_params
        assert "s-1|tc-0" in handle._tool_call_raw_params
        settled = JsonRpcMessage(
            method="session/update",
            params={
                "sessionId": "child-1",
                "update": {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-7",
                    "status": "completed",
                },
            },
        )
        assert handle._handle_update(settled) == []
        assert "child-1|tc-7" not in handle._tool_call_raw_params
        assert "child-1|tc-7" not in handle._tool_call_is_shell
        assert "child-1|tc-7" not in handle._tool_call_kind
        # The parent's in-flight row is untouched by the child's terminal.
        assert "s-1|tc-0" in handle._tool_call_raw_params

    def test_the_overflow_record_is_cleared_with_the_sibling_caches(self) -> None:
        """The per-turn clear is inline in ``_run_turn``; pin by source: wherever
        ``_tool_call_kind`` is cleared, ``_tool_call_cache_overflow`` is too."""
        import ast
        import inspect

        from kiro_crew.acp import session_handle

        tree = ast.parse(inspect.getsource(session_handle))
        cleared_in: dict[str, set[str]] = {}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(fn):
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "clear"
                    and isinstance(node.func.value, ast.Attribute)
                ):
                    cleared_in.setdefault(fn.name, set()).add(node.func.value.attr)
        owners = {fn for fn, names in cleared_in.items() if "_tool_call_kind" in names}
        assert owners, "the kind cache is cleared somewhere"
        for fn in owners:
            assert "_tool_call_cache_overflow" in cleared_in[fn], fn

    def test_the_overflow_record_holds_fixed_size_digests_never_the_scope(self, caplog) -> None:
        """The scope is the frame's own ``sessionId``, unbounded and backend
        authored -- and the record exists to remember scopes whose frames were
        refused for size. Storing the text would retain what the admission
        refused, so the record stores a 64-character digest whatever the scope
        was, and the once-per-snapshot warning carries the digest's head only."""
        import logging

        from kiro_crew.acp import _dispatch
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_KEY_CHARS, overflow_scope_key

        huge_scope = "s" * (10 * 1024 * 1024)
        caches = self._caches()
        # A scope that pushes any key over the key bound is refused; the scope is
        # what the key was refused FOR, and it must not be what the record keeps.
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp._dispatch"):
            parse_session_update(self._call(1), cache_scope=huge_scope, **caches)
        assert all(not c for c in self._dicts(caches))
        assert caches["cache_overflow"] == {overflow_scope_key(huge_scope)}
        (stored,) = caches["cache_overflow"]
        assert len(stored) == 64 and huge_scope[:TOOL_CALL_CACHE_MAX_KEY_CHARS] not in stored
        assert not hasattr(_dispatch, "_cache_overflow_reported"), "no process-global record"
        warned = [r.getMessage() for r in caplog.records if "refused a frame" in r.getMessage()]
        assert len(warned) == 1 and huge_scope[:TOOL_CALL_CACHE_MAX_KEY_CHARS] not in warned[0]
        # The digest is what the reader consults: the same scope reads as
        # overflowed, a different one does not.
        assert _dispatch.scope_overflowed(caches["cache_overflow"], huge_scope) is True
        assert _dispatch.scope_overflowed(caches["cache_overflow"], "other") is False

    def test_the_overflow_record_is_bounded_by_the_every_scope_marker(self) -> None:
        """A backend that refuses frames under a fresh sessionId each time would
        grow the record one digest per scope; past the shared entry cap the
        record takes the ``OVERFLOW_EVERY_SCOPE`` marker instead, so it never
        exceeds ``MAX_ENTRIES + 1`` entries, and every scope -- recorded or not
        -- then reads as overflowed (the fail-safe direction)."""
        from kiro_crew.acp._dispatch import (
            OVERFLOW_EVERY_SCOPE,
            TOOL_CALL_CACHE_MAX_ENTRIES,
            TOOL_CALL_CACHE_MAX_KEY_CHARS,
            scope_overflowed,
        )

        # Each frame is refused on the KEY bound (its scope alone exceeds it),
        # which costs nothing to render -- the payload bound is pinned by the
        # sibling tests, and refusing 259 frames on a multi-megabyte rendering
        # each was what made this test exceed a loaded shard's per-test budget.
        def scope(i: int) -> str:
            return f"scope-{i}-" + "s" * TOOL_CALL_CACHE_MAX_KEY_CHARS

        caches = self._caches()
        for i in range(TOOL_CALL_CACHE_MAX_ENTRIES + 3):
            parse_session_update(self._call(i), cache_scope=scope(i), **caches)
        record = caches["cache_overflow"]
        assert len(record) == TOOL_CALL_CACHE_MAX_ENTRIES + 1
        assert OVERFLOW_EVERY_SCOPE in record
        assert scope_overflowed(record, scope(0)) is True
        assert scope_overflowed(record, scope(TOOL_CALL_CACHE_MAX_ENTRIES + 2)) is True
        assert scope_overflowed(record, "never-refused") is True
        # Below the cap an unrecorded scope is NOT overflowed.
        small = self._caches()
        parse_session_update(self._call(0), cache_scope=scope(0), **small)
        assert scope_overflowed(small["cache_overflow"], scope(0)) is True
        assert scope_overflowed(small["cache_overflow"], "b") is False

    def test_a_refused_refinement_evicts_the_id_from_every_cache(self) -> None:
        """A held id's earlier frame wrote params, a diff path, an identity and
        a kind; a refinement of that id refused for size must not leave them in
        place -- the permission request would be judged against the SUPERSEDED
        target and marked trusted. The refusal evicts the id from every sibling
        store, the ones a refinement writes and the ones it does not."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        caches = self._caches()
        first = self._call(
            1,
            rawInput={"path": "/tmp/allowed.txt", "text": "x"},
            _meta={"kiro": {"toolId": "str_replace"}},
            content=[{"type": "diff", "path": "/tmp/allowed.txt", "oldText": "a", "newText": "b"}],
        )
        parse_session_update(first, **caches)
        assert caches["raw_params_cache"]["tc-1"]["path"] == "/tmp/allowed.txt"
        assert caches["diff_path_cache"]["tc-1"] == "/tmp/allowed.txt"
        assert caches["tool_kind_cache"]["tc-1"] == "edit"
        held_before = {k: dict(v) for k, v in caches.items() if isinstance(v, dict)}
        assert any("tc-1" in v for v in held_before.values())
        parse_session_update(
            {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc-1",
                "title": "Write File",
                "rawInput": {
                    "path": "/etc/passwd",
                    "text": "z" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1),
                },
            },
            **caches,
        )
        assert all("tc-1" not in c for c in self._dicts(caches)), "a stale row survived"
        assert caches["cache_overflow"] == self._over("")
        # The permission event for the id now carries no trusted provenance.
        msg = JsonRpcMessage(
            id=31,
            method="session/request_permission",
            params={
                "sessionId": "",
                "toolCall": {"toolCallId": "tc-1", "status": "pending", "title": "Write File"},
                "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
            },
        )
        perm_caches = {k: v for k, v in caches.items() if k != "cache_overflow"}
        event, _ = build_permission_event(msg, **perm_caches)
        assert event.raw_params_trusted is False
        assert event.tool_kind == ""

    @pytest.mark.asyncio
    async def test_the_handle_refuses_a_request_whose_refinement_was_refused(self) -> None:
        """GPT's scenario end to end: a benign cached KAS write, then an
        oversized same-id refinement that changes the target. The handle must
        refuse the permission request rather than trust the stale target."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS

        handle, sent = self._handle()
        handle._handle_update(
            self._frame(
                self._call(
                    5,
                    rawInput={"path": "/tmp/allowed.txt", "text": "x"},
                    _meta={"kiro": {"toolId": "str_replace"}},
                )
            )
        )
        pre = handle._build_permission_event(self._perm(40, "tc-5"))
        assert pre is not None and pre.raw_params_trusted is True
        handle._handle_update(
            self._frame(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "tc-5",
                    "title": "Write File",
                    "rawInput": {
                        "path": "/etc/passwd",
                        "text": "z" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1),
                    },
                }
            )
        )
        msg = self._perm(41, "tc-5")
        event = handle._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is False
        assert await handle._refuse_unretained_provenance(msg, event) is True
        assert sent == [(41, {"outcome": {"outcome": "selected", "optionId": "reject"}})]


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

    def test_the_kas_handle_hands_the_overflow_record_to_the_wire(self) -> None:
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        h = self._handle(ACP_BACKEND_KAS)
        assert h._tool_call_cache_overflow_for_wire() is h._tool_call_cache_overflow

    @pytest.mark.parametrize("backend", ["", "claude", "codex"])
    def test_every_other_handle_hands_no_overflow_record(self, backend: str) -> None:
        h = self._handle(backend)
        assert h._tool_call_cache_overflow == set()
        assert h._tool_call_cache_overflow_for_wire() is None

    @pytest.mark.parametrize("backend", ["", "claude", "codex"])
    def test_a_non_member_harness_retains_past_every_bound_and_is_never_refused(
        self, backend: str
    ) -> None:
        """The admission bound is the KAS projection's (H13): a kiro-cli edit over
        the payload bound, and a turn past the count cap, retain their provenance
        exactly as on the base, and the handle's unretained-provenance refusal
        never fires because the record is never written."""
        import asyncio

        from kiro_crew.acp._dispatch import (
            TOOL_CALL_CACHE_MAX_ENTRIES,
            TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS,
        )

        h = self._handle(backend)

        def _frame(i: int, **extra: Any) -> JsonRpcMessage:
            upd = {
                "sessionUpdate": "tool_call",
                "toolCallId": f"tc-{i}",
                "title": "Write File",
                "kind": "edit",
                "status": "pending",
                "rawInput": {"path": f"/tmp/f{i}", "text": "x"},
                **extra,
            }
            return JsonRpcMessage(
                method="session/update", params={"sessionId": "sA", "update": upd}
            )

        from kiro_crew.acp._dispatch import scoped_tool_cache_key as _k

        big = {"path": "/tmp/big", "text": "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1)}
        h._handle_update(_frame(0, rawInput=big))
        assert h._tool_call_raw_params[_k("sA", "tc-0")]["path"] == "/tmp/big"
        for i in range(1, TOOL_CALL_CACHE_MAX_ENTRIES + 2):
            h._handle_update(_frame(i))
        over = f"tc-{TOOL_CALL_CACHE_MAX_ENTRIES + 1}"
        assert (
            h._tool_call_raw_params[_k("sA", over)]["path"]
            == f"/tmp/f{TOOL_CALL_CACHE_MAX_ENTRIES + 1}"
        )
        assert h._tool_call_cache_overflow == set()
        msg = JsonRpcMessage(
            id=7,
            method="session/request_permission",
            params={
                "sessionId": "sA",
                "toolCall": {"toolCallId": "never-seen", "status": "pending", "title": "Write"},
                "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
            },
        )
        event = h._build_permission_event(msg)
        assert event is not None and event.raw_params_trusted is False
        assert asyncio.run(h._refuse_unretained_provenance(msg, event)) is False

    def test_the_kas_handle_is_the_one_the_bound_governs(self) -> None:
        """Counterfactual for the test above: on the KAS handle the same frames
        are refused past the bound and the record is written."""
        from kiro_crew.acp._dispatch import TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS, overflow_scope_key
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        h = self._handle(ACP_BACKEND_KAS)
        big = {"path": "/tmp/big", "text": "x" * (TOOL_CALL_CACHE_MAX_PAYLOAD_CHARS + 1)}
        h._handle_update(
            JsonRpcMessage(
                method="session/update",
                params={
                    "sessionId": "sA",
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCallId": "tc-0",
                        "title": "Write File",
                        "kind": "edit",
                        "status": "pending",
                        "rawInput": big,
                    },
                },
            )
        )
        assert not any(k.endswith("tc-0") for k in h._tool_call_raw_params)
        assert h._tool_call_cache_overflow == {overflow_scope_key("sA")}

    def test_a_kiro_cli_search_stays_kindless_and_read_only_proven(self) -> None:
        """kiro-cli streams a ``search`` tool_call then a kindless permission
        frame. Two independent defenses keep the event's kind ``""`` so the
        trusted identity proves the read: the kiro path hands the parser no
        cache at all (the KAS-only membership), and even a parser handed one
        carries only write-plane kinds, never ``search``."""
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

        # Second defense: a parser handed the cache still leaves ``search`` off
        # the event, so the proof holds even without the membership gate.
        with_cache = flow(with_cache=True)
        assert with_cache.tool_kind == ""
        r = HookManager().on_tool_call(
            with_cache.title, classifier_only=True, **hook_gate_kwargs(with_cache)
        )
        assert r.action == TOOL_AUTO_APPROVE and r.read_only is True

    def test_the_membership_gate_is_what_keeps_a_kiro_cli_edit_kindless(self) -> None:
        """Why the cache is still KAS-only: kiro-cli's ``fs_write`` tool_call
        stamps ``edit``, a write-plane kind the reader WOULD carry. kiro-cli
        routes its edits by the diff content block already, and the kindless
        governance classification is additive (read pairs beside the write
        pairs), so a carried ``edit`` would change what kiro-cli's gate sees
        (harness parity H13). The handle hands the kiro path no cache."""
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }

        def flow(with_cache: bool) -> AcpEvent:
            local = {k: {} for k in caches}
            if with_cache:
                local["tool_kind_cache"] = {}
            parse_session_update(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "tc-edit",
                    "title": "fs_write",
                    "kind": "edit",
                    "status": "pending",
                    "rawInput": {"command": "create", "path": "/tmp/x.md", "fileText": "x"},
                    "_meta": {"kiro": {"toolName": "fs_write", "mcpServerName": ""}},
                },
                **local,
            )
            msg = JsonRpcMessage(
                id=6,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {"toolCallId": "tc-edit", "status": "pending", "title": "fs_write"},
                    "options": [{"optionId": "a", "name": "Allow", "kind": "allow_once"}],
                },
            )
            event, _ = build_permission_event(msg, **local)
            return event

        assert flow(with_cache=False).tool_kind == ""
        assert flow(with_cache=True).tool_kind == "edit"


class TestKasSessionStartWarnsOnVocabularyDrift:
    """The policy fold fails permissive on engine drift, so the kas backend says
    once per engine release at session start -- where an operator sees it
    without running ``kirocrew doctor`` -- that the installed kiro-cli is not
    the one the tables were measured on. Same comparison as the doctor row."""

    @staticmethod
    def _runtime(backend: str):
        from kiro_crew.acp.runtime import AcpRuntime

        rt = object.__new__(AcpRuntime)
        rt._acp_backend = backend
        return rt

    def test_a_different_engine_release_is_logged_once_per_process(self, caplog) -> None:
        import logging

        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.types import ACP_BACKEND_KAS
        from kiro_crew.platform.tool_names import KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI

        newer = ".".join(str(p) for p in (*KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[-1][:2], 99))
        runtime_mod._KAS_DRIFT_WARNED.discard(newer)
        rt = self._runtime(ACP_BACKEND_KAS)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
            rt._warn_kas_vocabulary_drift(newer)
            rt._warn_kas_vocabulary_drift(newer)
            self._runtime(ACP_BACKEND_KAS)._warn_kas_vocabulary_drift(newer)
        hits = [r for r in caplog.records if "tool-name tables" in r.getMessage()]
        assert len(hits) == 1
        assert newer in hits[0].getMessage() and "newer" in hits[0].getMessage()

    def test_the_warn_once_set_is_count_bounded(self, caplog, monkeypatch) -> None:
        """The key is harness-written and the set lives for the process, so the
        bound is on the count as well as the string: at the cap no further
        version is retained, the overflow is said once, and the set stays at
        the cap."""
        import logging

        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        monkeypatch.setattr(runtime_mod, "_KAS_DRIFT_WARNED", set())
        monkeypatch.setattr(runtime_mod, "_KAS_DRIFT_WARNED_OVERFLOWED", False)
        cap = runtime_mod._KAS_DRIFT_WARNED_MAX
        assert isinstance(cap, int) and 0 < cap <= 256
        rt = self._runtime(ACP_BACKEND_KAS)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
            for i in range(cap + 3):
                rt._warn_kas_vocabulary_drift(f"9.{i}.0")
                rt._warn_kas_vocabulary_drift(f"9.{i}.0")
        drift = [r for r in caplog.records if "tool-name tables" in r.getMessage()]
        overflow = [r for r in caplog.records if "its bound" in r.getMessage()]
        assert len(drift) == cap
        assert len(overflow) == 1 and str(cap) in overflow[0].getMessage()
        assert len(runtime_mod._KAS_DRIFT_WARNED) == cap
        assert f"9.{cap}.0" not in runtime_mod._KAS_DRIFT_WARNED
        # A version already retained is still deduplicated at the cap.
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
            rt._warn_kas_vocabulary_drift("9.0.0")
        assert not caplog.records

    def test_the_measured_release_and_an_unparseable_version_are_silent(self, caplog) -> None:
        import logging

        from kiro_crew.acp.types import ACP_BACKEND_KAS
        from kiro_crew.platform.tool_names import KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI

        rt = self._runtime(ACP_BACKEND_KAS)
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
            rt._warn_kas_vocabulary_drift(
                ".".join(str(p) for p in KAS_TOOL_IDS_VERIFIED_ON_KIRO_CLI[0])
            )
            rt._warn_kas_vocabulary_drift("")
            rt._warn_kas_vocabulary_drift("not-a-version")
            # Bounded before parsing: a component past CPython's int digit limit
            # would otherwise raise inside the handshake guard.
            rt._warn_kas_vocabulary_drift("9" * 5000 + ".0.0")
        assert not [r for r in caplog.records if "tool-name tables" in r.getMessage()]

    @pytest.mark.parametrize("backend", ["", "claude", "codex"])
    def test_other_harnesses_never_warn(self, backend: str, caplog) -> None:
        import logging

        from kiro_crew.acp import runtime as runtime_mod

        runtime_mod._KAS_DRIFT_WARNED.discard("9.9.9")
        with caplog.at_level(logging.WARNING, logger="kiro_crew.acp.runtime"):
            self._runtime(backend)._warn_kas_vocabulary_drift("9.9.9")
        assert not [r for r in caplog.records if "tool-name tables" in r.getMessage()]

    @pytest.mark.asyncio
    async def test_the_compared_version_falls_back_to_the_pinned_binary(self, monkeypatch) -> None:
        """KAS's ``initialize`` carries no ``agentInfo`` (the recorded fixture
        says so), so a warning fed only the handshake's version would never fire
        on the harness it exists for. An empty handshake version falls back to
        the pinned kiro-cli's own ``--version`` -- the doctor row's reading; a
        reported version is preferred and does not probe; a non-member harness
        compares nothing and never probes."""
        from kiro_crew.acp import runtime as runtime_mod
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        probes: list[int] = []

        def _installed():
            probes.append(1)
            return (2, 30, 1)

        monkeypatch.setattr(runtime_mod, "installed_kiro_cli_version", _installed)
        rt = self._runtime(ACP_BACKEND_KAS)
        rt._agent_version = ""
        assert await rt._kas_drift_version() == "2.30.1"
        assert probes == [1]
        rt._agent_version = "2.24.1"
        assert await rt._kas_drift_version() == "2.24.1"
        assert probes == [1], "a reported version is not re-probed"
        for backend in ("", "claude", "codex"):
            other = self._runtime(backend)
            other._agent_version = ""
            assert await other._kas_drift_version() == ""
        assert probes == [1], "a non-member harness never probes"
        # An unanswerable probe is unknown, and unknown is silent.
        monkeypatch.setattr(runtime_mod, "installed_kiro_cli_version", lambda: None)
        rt._agent_version = ""
        assert await rt._kas_drift_version() == ""

    def test_the_handshake_hands_the_fallback_version_to_the_warning(self) -> None:
        """Pin by source: the handshake feeds the warning ``_kas_drift_version()``,
        not the raw ``agentInfo.version`` it also records."""
        import inspect

        from kiro_crew.acp.runtime import AcpRuntime

        src = inspect.getsource(AcpRuntime)
        assert "self._warn_kas_vocabulary_drift(await self._kas_drift_version())" in src
        assert "self._warn_kas_vocabulary_drift(self._agent_version)" not in src
        # And positively gated on the KAS set at the call site (H13): the
        # first-class harness's handshake awaits nothing for this.
        gate = "if self._acp_backend in ACP_BACKENDS_PERMISSION_KIND_FROM_TOOL_CALL:"
        call = "self._warn_kas_vocabulary_drift(await self._kas_drift_version())"
        assert src.index(gate, src.index("agent_version_from_init(init_resp)")) < src.index(call)


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

    @pytest.mark.parametrize(
        "bad",
        [7, "", "   ", None, ["read_file"], "k" * 129, "k" * (100 * 1024), "read_*", "a b"],
        ids=["int", "empty", "blank", "none", "list", "over-by-one", "100k", "glob", "space"],
    )
    def test_a_malformed_frame_id_is_ignored(self, bad) -> None:
        """Bounded at retention by the same rule ``_permission_tool_id`` applies:
        over ``_MAX_HARNESS_TOOL_ID_LEN``, a glob metacharacter or whitespace
        reads as absent, so the frame cannot grow the event or the audit log."""
        event = _kas_builtin_flow(bad)  # type: ignore[arg-type]
        assert event.tool_name == ""

    def test_the_frame_id_bound_is_the_permission_tool_id_bound(self) -> None:
        from kiro_crew.acp._dispatch import _MAX_HARNESS_TOOL_ID_LEN

        ok = "k" * _MAX_HARNESS_TOOL_ID_LEN
        assert _kas_builtin_flow(ok).tool_name == ok
        assert _kas_builtin_flow(ok + "k").tool_name == ""

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

    @pytest.mark.parametrize(
        "server",
        ["s" * 513, "s" * (8 * 1024 * 1024), "", None, ["probefs"]],
        ids=["over-bound", "8MiB", "empty", "none", "list"],
    )
    def test_an_oversized_or_malformed_server_name_on_the_frame_is_read_as_absent(
        self, server: object
    ) -> None:
        """The KAS ``serverName`` half is retained per call (``mcp_server_name_cache``)
        under the KAS-only admission, so it is bounded at the point it is read by
        what Crew's own MCP tool surface admits (``mcp_identity_name``, 512
        characters): a repeated 8 MiB ``serverName`` retains nothing. The kiro-cli
        ``mcpServerName`` channel is a first-class path and reads as it always did
        (a string verbatim, anything else absent), gaining no bound (H13)."""
        frame = dict(self._TOOL_CALL)
        frame["_meta"] = {"kiro": {"serverName": server, "toolOrigin": "client"}}
        identity = classify_tool_call(frame)
        assert identity.mcp_server_name == ""
        caches: dict[str, Any] = {"mcp_server_name_cache": {}, "tool_name_cache": {}}
        parse_session_update(frame, **caches)
        assert caches["mcp_server_name_cache"].get("tc-mcp", "") == ""
        kiro = dict(self._TOOL_CALL)
        kiro["_meta"] = {"kiro": {"mcpServerName": server, "toolName": "t"}}
        expected = server if isinstance(server, str) else ""
        assert classify_tool_call(kiro).mcp_server_name == expected
        # The bounded spelling still names the server.
        ok = classify_tool_call(dict(self._TOOL_CALL))
        assert ok.mcp_server_name == "probefs"

    @pytest.mark.parametrize(
        "server",
        ["s" * 512, "s" * 129, "probe*", "probe fs", "probe/fs:v2"],
        ids=["at-bound", "129", "glob", "space", "separators"],
    )
    def test_every_name_the_surface_admits_keeps_its_identity(self, server: str) -> None:
        """The bound is the SURFACE's, not the built-in id's: a server or tool name
        Crew's own MCP tool surface would list -- up to 512 characters, any
        spelling -- is retained, because an exact per-tool deny binds to nothing
        else and emptying it would hand the call to the title-keyed grant loop.
        The name is matched as a name, never as a pattern, so a metacharacter in
        it is inert."""
        from kiro_crew.mcp_gateway.tool_surface import admits_name

        assert admits_name(server)
        frame = dict(self._TOOL_CALL)
        frame["_meta"] = {"kiro": {"serverName": server, "toolOrigin": "client"}}
        assert classify_tool_call(frame).mcp_server_name == server
        caches: dict[str, Any] = {"mcp_server_name_cache": {}, "tool_name_cache": {}}
        parse_session_update(frame, **caches)
        assert caches["mcp_server_name_cache"]["tc-mcp"] == server

    def test_only_the_kas_channel_bounds_its_halves(self) -> None:
        """The bound is a property of the KAS channel row (``bounds_names``): its
        two halves are bounded before retention and goose/kiro-cli/codex -- the
        first-class channels -- retain a string verbatim as they always did, so no
        first-class path gains a bound or a refusal (H13)."""
        from kiro_crew.acp._dispatch import (
            _CODEX_MCP_TOOL_CALL_MARKER,
            _MCP_IDENTITY_META_CHANNELS,
            _meta_identity,
        )

        assert [c.bounds_names for c in _MCP_IDENTITY_META_CHANNELS] == [True, False, False]
        # A kiro-cli built-in (toolName, no server of either spelling) falls
        # through the KAS row to its own, unbounded.
        builtin = {"_meta": {"kiro": {"toolName": "t" * 600}}}
        assert _meta_identity(builtin) == ("", "t" * 600)
        huge = "t" * 4096
        kas = {"_meta": {"kiro": {"serverName": "srv", "toolName": huge}}}
        assert _meta_identity(kas) == ("srv", "")
        kiro = {"_meta": {"kiro": {"mcpServerName": "srv", "toolName": huge}}}
        assert _meta_identity(kiro) == ("srv", huge)
        goose = {"_meta": {"goose": {"toolCall": {"extensionName": "ext", "toolName": huge}}}}
        assert _meta_identity(goose) == ("ext", huge)
        goose_ok = {
            "_meta": {"goose": {"toolCall": {"extensionName": "ext", "toolName": "ext__tool"}}}
        }
        assert _meta_identity(goose_ok) == ("ext", "tool")
        codex = {
            "sessionUpdate": "tool_call",
            "toolCallId": "tc-cx",
            "kind": "execute",
            "rawInput": {"server": huge, "tool": "read"},
            "_meta": {_CODEX_MCP_TOOL_CALL_MARKER: True},
        }
        identity = classify_tool_call(codex)
        assert (identity.mcp_server_name, identity.tool_name) == (huge, "read")
        assert identity.identity_unreadable is False

    def test_the_permission_event_carries_the_canonical_pair(self) -> None:
        event = self._flow()
        assert (event.mcp_server_name, event.tool_name) == ("probefs", "read_file")

    @pytest.mark.parametrize(
        "bad",
        ["k" * 513, "k" * (100 * 1024), ""],
        ids=["over-by-one", "100k", "empty"],
    )
    def test_a_malformed_identity_pair_field_is_read_as_absent(self, bad) -> None:
        """The pair is bounded at retention by the surface's MCP-name bound: an
        oversized ``serverName`` names no server (the cached name stands), and an
        oversized ``toolName`` names no tool (the ``toolId`` stands). Neither is
        ever cut -- a truncated name is a different tool to an exact deny."""
        import copy

        meta = copy.deepcopy(self._PERMISSION_META)
        meta["kiro"]["mcpTool"]["identity"]["serverName"] = bad
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        parse_session_update(dict(self._TOOL_CALL), **caches)
        msg = JsonRpcMessage(
            id=44,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "t"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": meta,
            },
        )
        event, _ = build_permission_event(msg, **caches)
        # The tool_call's ``serverName`` channel already cached the server; the
        # pair with a malformed half names no tool, so the ``toolId`` stands.
        assert event.mcp_server_name == "probefs"
        assert event.tool_name == "mcp_probefs_read_file"

        meta2 = copy.deepcopy(self._PERMISSION_META)
        meta2["kiro"]["mcpTool"]["identity"]["toolName"] = bad
        caches2 = {k: {} for k in caches}
        parse_session_update(dict(self._TOOL_CALL), **caches2)
        msg2 = JsonRpcMessage(id=45, method="session/request_permission", params=dict(msg.params))
        msg2.params["_meta"] = meta2
        event2, _ = build_permission_event(msg2, **caches2)
        assert event2.tool_name == "mcp_probefs_read_file"

    @pytest.mark.parametrize("length", [129, 300, 512], ids=["129", "300", "at-bound"])
    def test_a_long_but_valid_mcp_tool_name_keeps_its_per_tool_deny(self, length: int) -> None:
        """A server may name a tool in up to 512 characters and Crew's own tool
        surface lists it; an operator's exact ``@server/<that name>`` deny must
        still bind. Under the built-in identifier bound the pair read as no
        identity, the deny target was dropped, and the call fell to the
        title-keyed grant loop -- so a 129-character name was auto-approved
        past its own deny."""
        import copy

        from kiro_crew.hooks import HooksConfig

        long_tool = "very_long_tool_name_" + "x" * (length - len("very_long_tool_name_"))
        assert len(long_tool) == length
        meta = copy.deepcopy(self._PERMISSION_META)
        meta["kiro"]["toolId"] = "mcp_probefs_" + long_tool
        meta["kiro"]["mcpTool"]["identity"]["toolName"] = long_tool
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
        }
        parse_session_update(dict(self._TOOL_CALL), **caches)
        msg = JsonRpcMessage(
            id=46,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "harmless"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": meta,
            },
        )
        event, _ = build_permission_event(msg, **caches)
        assert (event.mcp_server_name, event.tool_name) == ("probefs", long_tool)
        decision = HookManager(
            HooksConfig(auto_deny_tools=[f"@probefs/{long_tool}"], auto_approve_tools=["harmless"])
        ).on_tool_call(
            event.title,
            mcp_server_name=event.mcp_server_name,
            mcp_tool_name=event.tool_name,
            tool_kind=event.tool_kind,
            raw_params=event.raw_tool_params,
        )
        assert decision.action == TOOL_DENY

    @pytest.mark.parametrize(
        "channel",
        ["kiro-serverName", "kiro-toolName", "kiro-cli-toolName", "codex-tool", "permission-pair"],
    )
    def test_a_present_but_oversized_identity_half_is_unreadable_not_absent(
        self, channel: str
    ) -> None:
        """A 513-character half on the KAS channel is NOT a frame that names no
        tool: reading it as absent dropped the exact per-tool deny and handed the
        call to the title-keyed grant loop, so the KAS frame is marked unreadable
        and the permission event carries the mark; an absent half is not
        unreadable. The first-class channels (kiro-cli ``toolName``, the codex
        pair) are never marked -- they retain verbatim, as they always did (H13).
        """
        import copy

        from kiro_crew.acp._dispatch import _CODEX_MCP_TOOL_CALL_MARKER

        over = "n" * 513
        caches: dict[str, Any] = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
            "identity_unreadable_cache": {},
        }
        frame = dict(self._TOOL_CALL)
        perm_meta = copy.deepcopy(self._PERMISSION_META)
        if channel == "kiro-serverName":
            frame["_meta"] = {"kiro": {"serverName": over, "toolOrigin": "client"}}
        elif channel == "kiro-toolName":
            frame["_meta"] = {"kiro": {"serverName": "probefs", "toolName": over}}
        elif channel == "kiro-cli-toolName":
            frame["_meta"] = {"kiro": {"mcpServerName": "probefs", "toolName": over}}
        elif channel == "codex-tool":
            frame["kind"] = "execute"
            frame["rawInput"] = {"server": "probefs", "tool": over}
            frame["_meta"] = {_CODEX_MCP_TOOL_CALL_MARKER: True}
        else:
            perm_meta["kiro"]["mcpTool"]["identity"]["toolName"] = over
        kas_marked = channel in ("kiro-serverName", "kiro-toolName")
        first_class = channel in ("kiro-cli-toolName", "codex-tool")
        identity = classify_tool_call(frame)
        assert identity.identity_unreadable is kas_marked
        if first_class:
            assert over in (identity.mcp_server_name, identity.tool_name)
        else:
            assert over not in (identity.mcp_server_name, identity.tool_name)
        parse_session_update(frame, **caches)
        assert caches["identity_unreadable_cache"].get("tc-mcp") is (True if kas_marked else None)
        if first_class:
            return
        msg = JsonRpcMessage(
            id=47,
            method="session/request_permission",
            params={
                "sessionId": "s",
                "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "harmless"},
                "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                "_meta": perm_meta,
            },
        )
        event, _ = build_permission_event(msg, **caches)
        assert event.mcp_identity_unreadable is True
        assert over not in (event.mcp_server_name, event.tool_name)
        # The clean shape is untouched: nothing presented, nothing unreadable.
        clean: dict[str, Any] = {k: {} for k in caches}
        parse_session_update(dict(self._TOOL_CALL), **clean)
        assert clean["identity_unreadable_cache"] == {}
        ok_msg = JsonRpcMessage(id=48, method="session/request_permission", params=dict(msg.params))
        ok_msg.params["_meta"] = self._PERMISSION_META
        ok_event, _ = build_permission_event(ok_msg, **clean)
        assert ok_event.mcp_identity_unreadable is False

    @pytest.mark.parametrize(
        "cached_server, frame_server",
        [("", "probefs"), ("probefs", "other"), ("", "o" * 512)],
        ids=["builtin-then-server", "different-server", "builtin-then-long-server"],
    )
    def test_a_request_naming_a_server_the_tool_call_did_not_is_refused_not_adopted(
        self, cached_server: str, frame_server: str
    ) -> None:
        """The permission frame's ``mcpTool.identity.serverName`` never fills the
        cached server. A KAS built-in's tool_call writes an identity hit with an
        EMPTY server, so a server adopted from the request would carry the hit's
        trust into the server-keyed grants (the app-own MCP auto-approve) for a
        built-in. Agreement is a no-op; a server the tool_call did not name is
        refused on the KAS wire as an unreadable identity and never adopted; a
        first-class harness (no store handed) ignores the half."""
        import copy

        frame = dict(self._TOOL_CALL)
        if cached_server:
            frame["_meta"] = {"kiro": {"serverName": cached_server, "toolOrigin": "client"}}
        else:
            frame["_meta"] = {"kiro": {"toolOrigin": "client"}}
        perm_meta = copy.deepcopy(self._PERMISSION_META)
        perm_meta["kiro"]["mcpTool"]["identity"]["serverName"] = frame_server
        perm_meta["kiro"]["toolId"] = "read_file"
        for handed in (True, False):
            caches: dict[str, Any] = {
                "tool_input_cache": {},
                "shell_cache": {},
                "raw_params_cache": {},
                "mcp_server_name_cache": {},
                "tool_name_cache": {},
            }
            if handed:
                caches["identity_unreadable_cache"] = {}
            parse_session_update(frame, **caches)
            assert caches["mcp_server_name_cache"]["tc-mcp"] == cached_server
            msg = JsonRpcMessage(
                id=49,
                method="session/request_permission",
                params={
                    "sessionId": "s",
                    "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "Read File"},
                    "options": [{"optionId": "accept", "name": "Allow", "kind": "allow_once"}],
                    "_meta": perm_meta,
                },
            )
            event, _ = build_permission_event(msg, **caches)
            # The request's server is never adopted; the cache's stands.
            assert event.mcp_server_name == cached_server, handed
            assert frame_server not in (event.mcp_server_name, event.tool_name)
            assert event.mcp_identity_unreadable is handed
            if not handed:
                # The first-class shape reads as it always did: trusted per the
                # cache hit, serverless when the tool_call was.
                assert event.mcp_identity_trusted is True
        # Agreement stays a no-op and is never marked.
        ok = self._flow()
        assert (ok.mcp_server_name, ok.mcp_identity_unreadable) == ("probefs", False)

    @pytest.mark.asyncio
    async def test_the_handle_refuses_a_request_whose_identity_is_unreadable(self) -> None:
        """Refused before any consumer sees it, on the KAS wire shape and on a
        first-class harness alike (a fact about the frame, not a backend), and
        audited; a frame whose identity is readable is untouched."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.acp.session_handle import AcpSessionHandle
        from kiro_crew.acp.types import ACP_BACKEND_KAS

        for backend in (ACP_BACKEND_KAS,):
            rt = MagicMock()
            rt.acp_backend = backend
            rt.pid = None
            rt.is_alive = MagicMock(return_value=True)
            rt.send_notification = AsyncMock()
            rt.supports_image_prompt = False
            sent: list[tuple] = []

            async def _send_response(request_id, payload, _sent=sent):
                _sent.append((request_id, payload))

            rt.send_response = _send_response
            handle = AcpSessionHandle("s-1", asyncio.Queue(), rt)
            audits: list[tuple] = []
            handle._audit_handle_reject = lambda *a, **k: audits.append((a, k))  # type: ignore[method-assign]
            frame = dict(self._TOOL_CALL)
            frame["_meta"] = {"kiro": {"serverName": "probefs", "toolName": "n" * 513}}
            handle._handle_update(
                JsonRpcMessage(
                    method="session/update", params={"sessionId": "s-1", "update": frame}
                )
            )
            msg = JsonRpcMessage(
                id=49,
                method="session/request_permission",
                params={
                    "sessionId": "s-1",
                    "toolCall": {"toolCallId": "tc-mcp", "status": "pending", "title": "harmless"},
                    "options": [
                        {"optionId": "accept", "name": "Allow", "kind": "allow_once"},
                        {"optionId": "reject", "name": "Deny", "kind": "reject_once"},
                    ],
                },
            )
            event = handle._build_permission_event(msg)
            assert event is not None and event.mcp_identity_unreadable is True
            assert await handle._refuse_unreadable_mcp_identity(event) is True, backend
            assert sent == [(49, {"outcome": {"outcome": "selected", "optionId": "reject"}})]
            assert audits and audits[0][0][1:3] == ("mcp__unreadable", "mcp_identity_unreadable")
            # Readable: not refused by this.
            handle._handle_update(
                JsonRpcMessage(
                    method="session/update",
                    params={"sessionId": "s-1", "update": dict(self._TOOL_CALL)},
                )
            )
            ok = handle._build_permission_event(
                JsonRpcMessage(id=50, method="session/request_permission", params=dict(msg.params))
            )
            assert ok is not None and ok.mcp_identity_unreadable is False
            assert await handle._refuse_unreadable_mcp_identity(ok) is False
        # A first-class harness's handle is handed no store: the KAS-spelled frame
        # would mark, but nothing records it and nothing refuses (H13).
        rt2 = MagicMock()
        rt2.acp_backend = "kiro"
        rt2.pid = None
        rt2.is_alive = MagicMock(return_value=True)
        rt2.send_notification = AsyncMock()
        rt2.supports_image_prompt = False
        kiro_handle = AcpSessionHandle("s-1", asyncio.Queue(), rt2)
        assert kiro_handle._identity_unreadable_cache_for_wire() is None
        kiro_handle._handle_update(
            JsonRpcMessage(method="session/update", params={"sessionId": "s-1", "update": frame})
        )
        assert kiro_handle._tool_call_identity_unreadable == {}
        kiro_event = kiro_handle._build_permission_event(
            JsonRpcMessage(id=51, method="session/request_permission", params=dict(msg.params))
        )
        assert kiro_event is not None and kiro_event.mcp_identity_unreadable is False
        assert await kiro_handle._refuse_unreadable_mcp_identity(kiro_event) is False
        # Wired before the consumer yield, after the unidentifiable-MCP refusal.
        import inspect

        from kiro_crew.acp import session_handle

        src = inspect.getsource(session_handle.AcpSessionHandle._dispatch_events)
        a = src.index("self._refuse_unidentifiable_mcp_approval(msg, _perm_event)")
        b = src.index("self._refuse_unreadable_mcp_identity(_perm_event)")
        c = src.index("self._refuse_unretained_provenance(msg, _perm_event)")
        assert a < b < c < src.index("yield _perm_event")

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
