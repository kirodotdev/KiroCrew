"""Tests for ACP client: tool-call events, edit diffs and permission requests."""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from acp_client_helpers import _native_projection_for_fake_processes  # noqa: F401 - fixture
from acp_client_helpers import _pin_the_installed_kiro_cli_version  # noqa: F401 - fixture

from kiro_crew.acp.client import AcpClient, _make_unified_diff


class TestMakeUnifiedDiff:
    """Tests for _make_unified_diff helper."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            pytest.param("", "", id="both_empty_returns_empty"),
            pytest.param("same\n", "same\n", id="identical_returns_empty"),
        ],
    )
    def test_no_change_returns_empty(self, old, new):
        assert _make_unified_diff(old, new, "file.py") == ""

    def test_addition(self):
        result = _make_unified_diff("", "new line\n", "file.py")
        assert "+new line" in result
        assert "--- file.py" in result

    def test_deletion(self):
        result = _make_unified_diff("old line\n", "", "file.py")
        assert "-old line" in result

    def test_modification(self):
        result = _make_unified_diff("old\n", "new\n", "file.py")
        assert "-old" in result
        assert "+new" in result
        assert "@@" in result

    def test_truncation(self):
        result = _make_unified_diff("", "x\n" * 5000, "file.py", max_len=100)
        assert len(result) <= 100

    def test_truncation_cuts_at_line_boundary_and_is_marked(self):
        """A cut diff ends with the ``\\ diff truncated`` annotation on its own
        line (unified-diff escape convention — renderers skip it), never with a
        garbled half-row, so downstream +/- counting can detect understatement."""
        from kiro_crew.acp._dispatch import DIFF_TRUNCATION_MARK

        result = _make_unified_diff("", "wordwordword\n" * 5000, "file.py", max_len=200)
        assert len(result) <= 200
        assert result.endswith("\n" + DIFF_TRUNCATION_MARK)
        # Every line before the marker is a complete diff row from the
        # original (starts with a diff prefix, never a mid-word fragment).
        body_lines = result.split("\n")[:-1]
        assert all(
            line.startswith(("---", "+++", "@@", "+", "-", " ")) for line in body_lines if line
        )

    def test_under_cap_diff_is_not_marked(self):
        from kiro_crew.acp._dispatch import DIFF_TRUNCATION_MARK

        result = _make_unified_diff("old\n", "new\n", "file.py")
        assert DIFF_TRUNCATION_MARK not in result


class TestDeriveEditDiff:
    """Bare-JSON edit payloads derive a diff from their own arguments, so a
    tool_call with no diff content block still displays what changed."""

    def test_create_content_becomes_addition_diff(self):
        from kiro_crew.acp._dispatch import derive_edit_diff

        diff = derive_edit_diff(
            {"path": "/a/new.py", "command": "create", "fileText": "x = 1\ny = 2\n"}
        )
        assert "+x = 1" in diff
        assert "+y = 2" in diff
        assert "+++ /a/new.py" in diff

    def test_str_replace_pair_becomes_replace_hunk(self):
        from kiro_crew.acp._dispatch import derive_edit_diff

        diff = derive_edit_diff(
            {"path": "/a/b.py", "command": "strReplace", "oldStr": "x = 1\n", "newStr": "x = 2\n"}
        )
        assert "-x = 1" in diff
        assert "+x = 2" in diff

    def test_unrecognized_shapes_yield_empty(self):
        from kiro_crew.acp._dispatch import derive_edit_diff

        assert derive_edit_diff({"path": "/a/b", "command": "create"}) == ""
        assert derive_edit_diff({"command": "create", "fileText": "x"}) == ""  # no path
        assert derive_edit_diff("not a dict") == ""
        assert derive_edit_diff(None) == ""

    def test_non_string_arguments_never_reach_difflib(self):
        """Malformed args (numeric path, dict oldStr) must yield "" instead of
        letting a TypeError out of difflib abort the whole dispatch mid-turn."""
        from kiro_crew.acp._dispatch import derive_edit_diff

        assert (
            derive_edit_diff({"path": 42, "command": "strReplace", "oldStr": "a", "newStr": "b"})
            == ""
        )
        assert (
            derive_edit_diff(
                {"path": "/a/b", "command": "strReplace", "oldStr": {"x": 1}, "newStr": "b"}
            )
            == "--- /a/b\n+++ /a/b\n@@ -0,0 +1 @@\n+b"
        )
        assert derive_edit_diff({"path": ["/a"], "command": "create", "fileText": "x"}) == ""
        assert derive_edit_diff({"path": "/a/b", "command": "create", "fileText": 7}) == ""

    def test_insert_with_line_number_derives_positioned_hunk(self):
        """An insert IS additions-only, so with a line number the derived
        hunk is exact: zero old lines at insertLine, additions after it."""
        from kiro_crew.acp._dispatch import derive_edit_diff

        diff = derive_edit_diff(
            {"path": "/a/b.py", "command": "insert", "insertLine": 4, "content": "x = 1\ny = 2"}
        )
        assert "@@ -4,0 +5,2 @@" in diff
        assert "+x = 1" in diff
        assert "+y = 2" in diff

    def test_insert_without_line_number_derives_nothing(self):
        """Without a line number the hunk position would be a guess — the
        row keeps its fold-proof trace via the file_changes snapshot."""
        from kiro_crew.acp._dispatch import derive_edit_diff

        assert derive_edit_diff({"path": "/a/b.py", "command": "insert", "content": "x = 1"}) == ""

    def test_no_trailing_newline(self):
        result = _make_unified_diff("old", "new", "file.py")
        assert "-old" in result
        assert "+new" in result


# ── Coverage push: tool tracking ──


class TestExtractToolEvent:
    """Tests for _extract_tool_event covering various tool_call shapes."""

    @pytest.mark.parametrize("bad_update", [None, "call", [1], 7])
    def test_non_dict_update_returns_none(self, bad_update):
        """Non-dict update raised AttributeError on update.get() in the
        prompt-turn dispatch path — must be a clean None instead."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(method="session/update", params={"update": bad_update})
        assert client._extract_tool_event(msg) is None  # must not raise

    def test_basic_tool_call(self):
        client = AcpClient()
        from kiro_crew.acp.types import EVENT_TOOL_CALL, JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Read",
                    "kind": "tool_use",
                    "toolCallId": "tc-1",
                    "input": {"path": "/tmp/file.txt"},
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.kind == EVENT_TOOL_CALL
        assert event.title == "Read"

    def test_tool_call_with_diff_content(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "write",
                    "kind": "tool_use",
                    "toolCallId": "tc-2",
                    "input": {},
                    "content": [
                        {"type": "diff", "oldText": "old\n", "newText": "new\n", "path": "f.py"}
                    ],
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert "-old" in event.tool_input or "+new" in event.tool_input
        assert event.diff_path == "f.py"

    def test_tool_call_diff_path_survives_an_inaccurate_kind(self):
        """A diff content block still marks the write plane even when the
        backend's own ``kind`` is not ``edit`` (e.g. ``read`` or unset) --
        ``is_edit_call`` ORs ``diff_path`` with ``kind`` precisely so an
        agent-influenced ``kind`` cannot hide a real file write from
        ``tool.risk``'s caution carve-out."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "write",
                    "kind": "read",
                    "toolCallId": "tc-diffpath",
                    "input": {},
                    "content": [
                        {
                            "type": "diff",
                            "oldText": "old\n",
                            "newText": "new\n",
                            "path": "README.md",
                        }
                    ],
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.diff_path == "README.md"

    def test_tool_call_str_replace_fallback(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "write",
                    "kind": "tool_use",
                    "toolCallId": "tc-3",
                    "input": {
                        "command": "strReplace",
                        "oldStr": "a",
                        "newStr": "b",
                        "path": "x.py",
                    },
                }
            },
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert "-a" in event.tool_input or "+b" in event.tool_input

    def test_non_tool_call_returns_none(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "hi"}}},
        )
        event = client._extract_tool_event(msg)
        assert event is None

    @pytest.mark.parametrize(
        ("update", "expected"),
        [
            pytest.param(
                {
                    "title": "shell",
                    "kind": "tool_use",
                    "toolCallId": "tc-4",
                    "input": {"__tool_use_purpose": "run tests", "command": "pytest"},
                },
                "run tests",
                id="tool_purpose_extracted",
            ),
            # kiro-cli echoes the reserved purpose arg back camelCased on some tool
            # calls. Reading only the snake_case spelling dropped the purpose, which
            # made the dashboard's concise tool pill fall back to the raw command.
            pytest.param(
                {
                    "title": "Running: node kc-shot.mjs",
                    "kind": "execute",
                    "toolCallId": "tc-5",
                    "input": {
                        "__toolUsePurpose": "check harness render errors",
                        "command": "node kc-shot.mjs",
                    },
                },
                "check harness render errors",
                id="tool_purpose_extracted_camel_case_key",
            ),
        ],
    )
    def test_tool_purpose(self, update, expected):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            method="session/update",
            params={"update": {"sessionUpdate": "tool_call", **update}},
        )
        event = client._extract_tool_event(msg)
        assert event is not None
        assert event.tool_purpose == expected


class TestBuildPermissionEvent:
    """Tests for _build_permission_event."""

    def test_basic_permission(self):
        client = AcpClient()
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            id=42,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc-5"},
                "options": [
                    {"id": "allow_once", "label": "Allow once"},
                    {"id": "allow_always", "label": "Allow always"},
                ],
            },
        )
        event = client._build_permission_event(msg)
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.request_id == 42
        assert event.title == "shell"
        assert len(event.options) == 2

    def test_tool_kind_carried_from_toolcall(self):
        # The ACP toolCall carries kind="execute" for Bash; carrying it onto the
        # event lets downstream validation apply the execute-tool exemptions
        # (e.g. the display-name length cap). Without the carried kind a long
        # bash command aborts as "User refused permission".
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=43,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "ls", "kind": "execute", "toolCallId": "tc-k"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert event.tool_kind == "execute"

    def test_tool_kind_defaults_empty_when_absent(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=44,
            method="session/requestPermission",
            params={"toolCall": {"title": "ls"}, "options": []},
        )
        event = client._build_permission_event(msg)
        assert event.tool_kind == ""

    def test_default_options_when_empty(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=10,
            method="session/requestPermission",
            params={"toolCall": {"title": "rm"}, "options": []},
        )
        event = client._build_permission_event(msg)
        assert len(event.options) == 2
        assert event.options[0]["id"] == "allow_once"

    @pytest.mark.parametrize("bad_options", [None, "allow", 42, {"id": "allow_once"}])
    def test_non_list_options_degrade_to_defaults(self, bad_options):
        """The permission payload comes straight from the agent process; a
        non-list options value made the for-loop raise TypeError (or iterate
        dict keys), tearing down the prompt-turn event generator instead of
        degrading to the default allow options."""
        client = AcpClient()
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            id=12,
            method="session/requestPermission",
            params={"toolCall": {"title": "rm"}, "options": bad_options},
        )
        event = client._build_permission_event(msg)  # must not raise
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert len(event.options) == 2
        assert event.options[0]["id"] == "allow_once"

    @pytest.mark.parametrize("bad_toolcall", [None, "shell", ["x"], 7])
    def test_non_dict_toolcall_degrades_to_unknown(self, bad_toolcall):
        """A non-dict toolCall made tool_call.get(...) raise AttributeError."""
        client = AcpClient()
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, JsonRpcMessage

        msg = JsonRpcMessage(
            id=13,
            method="session/requestPermission",
            params={"toolCall": bad_toolcall, "options": []},
        )
        event = client._build_permission_event(msg)  # must not raise
        assert event.kind == EVENT_PERMISSION_REQUEST
        assert event.title == "unknown"

    def test_non_dict_and_non_string_option_entries_skipped(self):
        """Non-dict entries raised AttributeError on o.get(); an int optionId
        crashed opt_id.lower() in the legacy-kind synthesis. Both must be
        skipped while valid entries still parse."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=14,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    "allow",  # non-dict
                    None,  # non-dict
                    {"id": 42, "label": "int id"},  # non-string id
                    {"id": "allow_once", "label": "Allow once"},
                ],
            },
        )
        event = client._build_permission_event(msg)  # must not raise
        assert event.options == [{"id": "allow_once", "label": "Allow once"}]

    def test_cached_tool_input_used(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        client._tool_call_inputs["tc-6"] = '{"cmd": "rm -rf /"}'
        msg = JsonRpcMessage(
            id=11,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell", "toolCallId": "tc-6"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert "rm -rf" in event.tool_input
        # Retained through same-call re-prompts; per-turn clear owns cleanup.
        assert "tc-6" in client._tool_call_inputs

    def test_cached_tool_input_carries_redaction_provenance(self):
        """A secret removed before the permission event must leave a boolean
        provenance bit; the original bytes must not be copied onto the event."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        tool = JsonRpcMessage(
            params={
                "update": {
                    "sessionUpdate": "tool_call",
                    "title": "Run Command",
                    "kind": "execute",
                    "toolCallId": "tc-secret",
                    "rawInput": {"command": "echo AKIAIOSFODNN7EXAMPLE"},
                }
            }
        )
        client._extract_tool_event(tool)
        assert client._tool_call_input_redacted["tc-secret"] is True

        permission = JsonRpcMessage(
            id=111,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "Run Command", "toolCallId": "tc-secret"},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(permission)

        assert event.tool_input_redacted is True
        assert "AKIAIOSFODNN7EXAMPLE" not in event.tool_input
        assert "[REDACTED: credential]" in event.tool_input
        assert client._tool_call_input_redacted["tc-secret"] is True
        repeated = client._build_permission_event(permission)
        assert repeated.tool_input_redacted is True
        assert repeated.tool_input == event.tool_input

    def test_fallback_input_from_tool_call(self):
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=12,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "write", "toolCallId": "tc-7", "input": {"path": "/x"}},
                "options": [{"id": "allow_once", "label": "Allow"}],
            },
        )
        event = client._build_permission_event(msg)
        assert "/x" in event.tool_input

    def test_acp_spec_shape_records_optionids(self):
        """ACP-spec shape (optionId/name/kind) populates _permission_options."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=20,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "allow_always", "name": "Always", "kind": "allow_always"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[20] == {"once": "allow", "always": "allow_always"}

    def test_legacy_kiro_shape_records_optionids(self):
        """Legacy kiro shape (id/label, no kind) is classified by literal id."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=21,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"id": "allow_once", "label": "Allow once"},
                    {"id": "allow_always", "label": "Allow always"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[21] == {
            "once": "allow_once",
            "always": "allow_always",
        }

    def test_reject_option_recorded_even_without_allow(self):
        """A reject option must be recorded so reject_tool can send a clean
        ``selected`` reject (behavior:"deny") instead of ``cancelled`` (which
        the claude-agent-acp adapter turns into "Tool use aborted").
        """
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=22,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[22].get("reject") == "reject_once"

    def test_plain_reject_id_without_kind_recorded(self):
        """A deny-naming id with NO kind (plain "reject"/"deny") must be
        classified as a per-tool reject. Missing it sent reject_tool down the
        ``cancelled`` fallback, which the backend treats as cancelling the
        TURN — every later tool call was auto-denied without prompting."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        for req_id, opt_id in ((24, "reject"), (25, "deny"), (26, "Deny_Once")):
            msg = JsonRpcMessage(
                id=req_id,
                method="session/requestPermission",
                params={
                    "toolCall": {"title": "shell"},
                    "options": [
                        {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                        {"optionId": opt_id, "name": "Deny"},
                    ],
                },
            )
            client._build_permission_event(msg)
            assert client._permission_options[req_id].get("reject") == opt_id, opt_id

    def test_every_deny_vocabulary_value_round_trips(self):
        """Every id in the shared deny table and every deny behavior must be
        recognized by BOTH classification sites — the event builder (recorded
        reject option) and the auto-answer path (reject_option_id). Driven by
        the table itself so a vocabulary addition cannot ship untested."""
        from kiro_crew.acp._dispatch import (
            _DENY_BEHAVIORS,
            _DENY_OPTION_IDS,
            reject_option_id,
        )
        from kiro_crew.acp.types import JsonRpcMessage

        assert _DENY_OPTION_IDS, "derived deny-id table must not be empty"
        client = AcpClient()
        req_id = 9000
        for opt_id in sorted(_DENY_OPTION_IDS):
            params = {
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": opt_id, "name": "Deny"},
                ],
            }
            # Site 1: auto-answer path picks the deny id.
            assert reject_option_id(params) == opt_id, opt_id
            # Site 2: event builder records it as the reject option.
            req_id += 1
            client._build_permission_event(
                JsonRpcMessage(id=req_id, method="session/requestPermission", params=params)
            )
            assert client._permission_options[req_id].get("reject") == opt_id, opt_id
        for behavior in sorted(_DENY_BEHAVIORS):
            params = {
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "yes", "name": "Allow", "behavior": "allow"},
                    {"optionId": "custom-no", "name": "Deny", "behavior": behavior},
                ],
            }
            assert reject_option_id(params) == "custom-no", behavior
            req_id += 1
            client._build_permission_event(
                JsonRpcMessage(id=req_id, method="session/requestPermission", params=params)
            )
            assert client._permission_options[req_id].get("reject") == "custom-no", behavior

    def test_contradictory_kind_wins_over_deny_metadata(self):
        """A valid spec `kind` classifies the option ALONE: an option carrying
        {kind:"allow_once"} with a contradictory deny behavior or deny-naming
        id must never be selected as the reject — answering with an allow
        optionId would APPROVE the tool the caller meant to deny."""
        from kiro_crew.acp._dispatch import reject_option_id

        # behavior:"deny" on an allow-kind option: not a reject candidate.
        params = {
            "options": [
                {"optionId": "yes", "name": "Allow", "kind": "allow_once", "behavior": "deny"},
            ]
        }
        assert reject_option_id(params) is None
        # deny-naming id on an allow-kind option: not a reject candidate.
        params = {
            "options": [
                {"optionId": "deny", "name": "Allow", "kind": "allow_always"},
            ]
        }
        assert reject_option_id(params) is None
        # A genuine reject option alongside the contradictory one still wins.
        params = {
            "options": [
                {"optionId": "yes", "name": "Allow", "kind": "allow_once", "behavior": "deny"},
                {"optionId": "no", "name": "Deny", "behavior": "deny"},
            ]
        }
        assert reject_option_id(params) == "no"

    def test_deny_behavior_without_kind_recorded(self):
        """An option that speaks ``behavior: "deny"`` instead of
        ``kind`` is a per-tool reject whatever its id is called. An allow
        behavior must never be classified as a reject."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=27,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "yes", "name": "Allow", "behavior": "allow"},
                    {"optionId": "no", "name": "Deny", "behavior": "deny"},
                ],
            },
        )
        client._build_permission_event(msg)
        assert client._permission_options[27].get("reject") == "no"
        # The allow-behavior option was NOT misread as a reject.
        assert client._permission_options[27].get("reject") != "yes"

    @pytest.mark.asyncio
    async def test_reject_with_advertised_deny_never_answers_cancelled(self):
        """End-to-end pin: when ANY deny-shaped option was advertised,
        reject_tool answers a per-tool ``selected`` reject — never the
        turn-cancelling ``cancelled`` outcome."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=28,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [
                    {"optionId": "allow", "name": "Allow", "kind": "allow_once"},
                    {"optionId": "deny", "name": "Deny"},
                ],
            },
        )
        client._build_permission_event(msg)
        sent: list[dict] = []

        async def _capture(request_id, payload):
            sent.append(payload)

        client._send_response = _capture  # type: ignore[method-assign]
        await client.reject_tool(28)
        assert len(sent) == 1
        assert sent[0]["outcome"]["outcome"] != "cancelled"
        assert sent[0]["outcome"] == {"outcome": "selected", "optionId": "deny"}

    def test_unknown_legacy_id_not_classified(self):
        """Unknown legacy ids do not get a synthesized kind."""
        client = AcpClient()
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=23,
            method="session/requestPermission",
            params={
                "toolCall": {"title": "shell"},
                "options": [{"id": "weird_custom_id", "label": "?"}],
            },
        )
        client._build_permission_event(msg)
        assert 23 not in client._permission_options


class TestApproveTool:
    """Tests for approve_tool always= and recorded-option dispatch."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _recorded_requests(self, monkeypatch):
        """Every id approved here stands for a request the client built an event for."""
        from kiro_crew.acp.types import EVENT_PERMISSION_REQUEST, AcpEvent

        class _Recorded(dict):
            def pop(self, key, default=None):
                return AcpEvent(kind=EVENT_PERMISSION_REQUEST, request_id=key, title="notes.txt")

        original = AcpClient.__init__

        def _init(self, *args, **kwargs):
            original(self, *args, **kwargs)
            self._permission_gate_events = _Recorded()

        monkeypatch.setattr(AcpClient, "__init__", _init)

    @pytest.mark.asyncio
    async def test_always_uses_recorded_optionid(self, tmp_path):
        from kiro_crew.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[42] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(42, always=True)
        client._send_response.assert_awaited_once_with(
            42,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "allow_always"}},
        )
        assert 42 not in client._permission_options

    @pytest.mark.asyncio
    async def test_once_uses_recorded_optionid(self, tmp_path):
        from kiro_crew.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[43] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(43)
        client._send_response.assert_awaited_once_with(
            43,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "allow"}},
        )

    @pytest.mark.asyncio
    async def test_no_recorded_falls_back_to_literal(self, tmp_path):
        from kiro_crew.acp.types import (
            OPTION_ALLOW_ALWAYS,
            OPTION_ALLOW_ONCE,
            OUTCOME_SELECTED,
        )

        client = AcpClient(work_dir=tmp_path)
        client._send_response = AsyncMock()
        await client.approve_tool(44)
        client._send_response.assert_awaited_with(
            44,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}},
        )
        await client.approve_tool(45, always=True)
        client._send_response.assert_awaited_with(
            45,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ALWAYS}},
        )

    @pytest.mark.asyncio
    async def test_explicit_option_id_skips_recorded_pop(self, tmp_path):
        """Explicit option_id bypasses the recorded entry — defensive retries
        with a recorded entry left intact still send the explicit id."""
        from kiro_crew.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[46] = {"once": "allow", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.approve_tool(46, option_id="custom_id")
        client._send_response.assert_awaited_with(
            46,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "custom_id"}},
        )
        assert client._permission_options[46] == {"once": "allow", "always": "allow_always"}

    @pytest.mark.asyncio
    async def test_reject_only_recorded_falls_back_to_literal_on_approve(self, tmp_path):
        """A request that advertised only a reject option records {"reject": ...}
        with no "once"/"always" keys. Approving it must fall back to the canonical
        allow id rather than KeyError-ing on the missing key."""
        from kiro_crew.acp.types import (
            OPTION_ALLOW_ALWAYS,
            OPTION_ALLOW_ONCE,
            OUTCOME_SELECTED,
        )

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[47] = {"reject": "reject_once"}
        client._send_response = AsyncMock()
        await client.approve_tool(47)
        client._send_response.assert_awaited_with(
            47,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ONCE}},
        )
        assert 47 not in client._permission_options

        client._permission_options[48] = {"reject": "reject_once"}
        await client.approve_tool(48, always=True)
        client._send_response.assert_awaited_with(
            48,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": OPTION_ALLOW_ALWAYS}},
        )


class TestRejectTool:
    """Tests for reject_tool clean-reject vs cancelled dispatch."""

    @pytest.mark.asyncio
    async def test_uses_recorded_reject_optionid(self, tmp_path):
        """When a reject option was advertised, reject_tool sends a clean
        ``selected`` reject so the adapter returns behavior:"deny" rather than
        throwing "Tool use aborted" on a cancelled outcome."""
        from kiro_crew.acp.types import OUTCOME_SELECTED

        client = AcpClient(work_dir=tmp_path)
        # claude-agent-acp advertises optionId "reject" with kind "reject_once"
        client._permission_options[60] = {"once": "allow", "reject": "reject"}
        client._send_response = AsyncMock()
        await client.reject_tool(60)
        client._send_response.assert_awaited_once_with(
            60,
            {"outcome": {"outcome": OUTCOME_SELECTED, "optionId": "reject"}},
        )
        assert 60 not in client._permission_options

    @pytest.mark.asyncio
    async def test_falls_back_to_cancelled_when_no_reject_option(self, tmp_path):
        """When no reject option was advertised (kiro-cli), reject_tool falls
        back to the cancelled outcome — kiro handles it as a clean rejection."""
        from kiro_crew.acp.types import OUTCOME_CANCELLED

        client = AcpClient(work_dir=tmp_path)
        client._permission_options[61] = {"once": "allow_once", "always": "allow_always"}
        client._send_response = AsyncMock()
        await client.reject_tool(61)
        client._send_response.assert_awaited_once_with(
            61,
            {"outcome": {"outcome": OUTCOME_CANCELLED}},
        )
        assert 61 not in client._permission_options

    @pytest.mark.asyncio
    async def test_falls_back_to_cancelled_when_nothing_recorded(self, tmp_path):
        """No recorded options at all → cancelled fallback (safe for kiro)."""
        from kiro_crew.acp.types import OUTCOME_CANCELLED

        client = AcpClient(work_dir=tmp_path)
        client._send_response = AsyncMock()
        await client.reject_tool(62)
        client._send_response.assert_awaited_once_with(
            62,
            {"outcome": {"outcome": OUTCOME_CANCELLED}},
        )


class TestHandlePermission:
    """Tests for _handle_permission (auto-approve)."""

    @pytest.mark.asyncio
    async def test_auto_approves(self, tmp_path):
        client = AcpClient(work_dir=tmp_path)
        from kiro_crew.acp.types import JsonRpcMessage

        msg = JsonRpcMessage(
            id=55,
            method="session/requestPermission",
            params={"toolCall": {"title": "bash"}, "options": []},
        )
        client.approve_tool = AsyncMock()

        await client._handle_permission(msg)
        client.approve_tool.assert_awaited_once_with(55)


class TestReadNewToolResultsSync:
    """Tests for _read_new_tool_results_sync."""

    @pytest.fixture(autouse=True)
    # nosemgrep: kirocrew.test-autouse-shared-monkeypatch -- no test in this module calls monkeypatch.undo()
    def _isolate_home(self, tmp_path, monkeypatch):
        """Redirect Path.home to tmp_path so JSONL writes are isolated."""
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    @pytest.mark.parametrize(
        "session_id",
        [
            pytest.param(None, id="no_session_returns_empty"),
            pytest.param("nonexistent-session-xyz", id="missing_file_returns_empty"),
        ],
    )
    def test_nothing_to_read_returns_empty(self, session_id):
        client = AcpClient()
        client._session_id = session_id
        assert client._read_new_tool_results_sync() == []

    def test_reads_tool_results(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess.jsonl"

        entry = {
            "kind": "ToolResults",
            "data": {
                "content": [
                    {
                        "kind": "toolResult",
                        "data": {
                            "toolUseId": "tu-1",
                            "content": [{"kind": "text", "data": "output here"}],
                        },
                    }
                ]
            },
        }
        jsonl_path.write_text(json.dumps(entry) + "\n")

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-1"
            assert "output here" in results[0].tool_output
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_reads_json_kind_with_stdout(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-2"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-2.jsonl"

        entry = {
            "kind": "ToolResults",
            "data": {
                "content": [
                    {
                        "kind": "toolResult",
                        "data": {
                            "toolUseId": "tu-2",
                            "content": [{"kind": "json", "data": {"stdout": "hello world"}}],
                        },
                    }
                ]
            },
        }
        jsonl_path.write_text(json.dumps(entry) + "\n")

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert "hello world" in results[0].tool_output
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_skips_non_tool_results(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-3"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-3.jsonl"

        lines = [
            json.dumps({"kind": "Message", "data": {"text": "hi"}}) + "\n",
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "tu-3",
                                    "content": [{"kind": "text", "data": "result"}],
                                },
                            }
                        ]
                    },
                }
            )
            + "\n",
        ]
        jsonl_path.write_text("".join(lines))

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-3"
        finally:
            jsonl_path.unlink(missing_ok=True)

    def test_partial_line_not_consumed(self, tmp_path):
        client = AcpClient()
        client._session_id = "test-sess-4"
        client._jsonl_pos = 0

        session_dir = Path.home() / ".kiro" / "sessions" / "cli"
        session_dir.mkdir(parents=True, exist_ok=True)
        jsonl_path = session_dir / "test-sess-4.jsonl"

        # Write a complete line + partial line (no trailing newline)
        complete = (
            json.dumps(
                {
                    "kind": "ToolResults",
                    "data": {
                        "content": [
                            {
                                "kind": "toolResult",
                                "data": {
                                    "toolUseId": "tu-ok",
                                    "content": [{"kind": "text", "data": "done"}],
                                },
                            }
                        ]
                    },
                }
            )
            + "\n"
        )
        partial = '{"kind": "ToolResults", "data": {"content": [{"kind": "toolRes'
        jsonl_path.write_text(complete + partial)

        try:
            results = client._read_new_tool_results_sync()
            assert len(results) == 1
            assert results[0].tool_call_id == "tu-ok"
        finally:
            jsonl_path.unlink(missing_ok=True)
