"""The interactive read-only tier's proof for kiro-cli's built-in file reader.

kiro-cli's ``session/request_permission`` for its ``read`` built-in carries no
``kind``: only the tool_call frame before it says ``read``. The gate therefore sees
the host-stamped tool name, the trusted-identity flag, the arguments and a
``Reading …`` title, which the title fallback does not take. These tests drive the
gate with exactly that shape. A read whose every path is absolute and holds no
sensitive location is approved after every deny tier; anything else keeps the
prompt it had.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kiro_crew import hooks as hooks_mod
from kiro_crew.acp._dispatch import build_permission_event, parse_session_update
from kiro_crew.acp.types import JsonRpcMessage
from kiro_crew.hooks import (
    TOOL_ALLOW,
    TOOL_AUTO_APPROVE,
    TOOL_DENY,
    HookManager,
    HooksConfig,
    ToolCall,
)

_KIRO_READ_FIXTURE = (
    Path(__file__).parent / "fixtures" / "acp_frames" / "kiro" / "side-read-only-live.jsonl"
)


def _line_read(path: str) -> dict:
    """kiro-cli's ``read`` arguments for one Line read of *path*."""
    return {
        "__tool_use_purpose": "Read the note.",
        "operations": [{"mode": "Line", "path": path, "start_line": 1, "end_line": 3}],
    }


def _judge(
    tool: str,
    raw: dict,
    *,
    trusted: bool = True,
    kind: str = "",
    title: str = "Reading note.txt:1-3",
    **extra,
):
    """One kiro-cli permission request for built-in *tool*, as the gate receives it."""
    return HookManager(extra.pop("config", None) or HooksConfig()).on_tool_call(
        title,
        tool_kind=kind,
        raw_params=raw,
        mcp_tool_name=tool,
        mcp_identity_trusted=trusted,
        **extra,
    )


@pytest.fixture
def note(tmp_path):
    """An absolute file outside the session's folder, in a folder holding nothing sensitive."""
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    path = folder / "note.txt"
    path.write_text("line one\nline two\nline three\n", encoding="utf-8")
    return str(path)


class TestKiroCliReadsAreApproved:
    def test_a_reads_chat_approves_the_kindless_read_the_reporter_saw(self, note):
        call = ToolCall(
            title="Reading note.txt:1-3",
            raw_params=_line_read(note),
            mcp_tool="read",
            identity_trusted=True,
        )
        result = HookManager().judge(call, trust_reads=True, classifier_only=False)
        assert (result.action, result.read_only) == (TOOL_AUTO_APPROVE, True)

    def test_a_normal_chat_approves_it_as_it_approves_a_read_kind(self, note):
        result = _judge("read", _line_read(note))
        assert (result.action, result.read_only) == (TOOL_AUTO_APPROVE, True)

    def test_the_spec_spelling_is_the_same_reader(self, note):
        assert _judge("fs_read", _line_read(note)).action == TOOL_AUTO_APPROVE

    def test_the_kind_the_tool_call_frame_stated_is_accepted(self, note):
        assert _judge("read", _line_read(note), kind="read").action == TOOL_AUTO_APPROVE

    def test_a_directory_listing_of_a_plain_folder_is_approved(self, note):
        raw = {"operations": [{"mode": "Directory", "path": os.path.dirname(note), "depth": 1}]}
        assert _judge("read", raw, title="Reading elsewhere").action == TOOL_AUTO_APPROVE

    def test_a_batch_of_plain_reads_is_approved(self, note):
        raw = {
            "operations": [
                {"mode": "Line", "path": note},
                {"mode": "Directory", "path": os.path.dirname(note)},
            ]
        }
        assert _judge("read", raw).action == TOOL_AUTO_APPROVE


class TestEverythingElseKeepsItsPrompt:
    def test_an_untrusted_identity_proves_nothing(self, note):
        assert _judge("read", _line_read(note), trusted=False).action == TOOL_ALLOW

    def test_a_server_tool_named_like_the_builtin_proves_nothing(self, note):
        result = _judge("read", _line_read(note), mcp_server_name="files")
        assert result.action == TOOL_ALLOW

    @pytest.mark.parametrize("tool", ["write", "fs_write", "web_fetch", "web_search", "code"])
    def test_another_builtin_gains_no_proof(self, tool, note):
        raw = {**_line_read(note), "url": "https://example.com", "query": "x"}
        assert _judge(tool, raw).action == TOOL_ALLOW

    @pytest.mark.parametrize("tool", ["shell", "write", "web_fetch", "web_search", "grep", ""])
    def test_the_proof_names_only_the_file_reader(self, tool, note):
        assert not hooks_mod._is_host_file_read_builtin_call(
            tool, "", _line_read(note), mcp_identity_trusted=True
        )

    @pytest.mark.parametrize("kind", ["edit", "execute", "delete", "other", "move", "search"])
    def test_a_kind_naming_anything_else_refuses_the_proof(self, kind, note):
        assert _judge("read", _line_read(note), kind=kind).action == TOOL_ALLOW

    @pytest.mark.parametrize("path", ["note.txt", "./note.txt", "../note.txt", "$HOME/note.txt"])
    def test_a_path_the_gateway_would_resolve_differently_asks(self, path):
        assert _judge("read", _line_read(path)).action == TOOL_ALLOW

    def test_a_read_naming_no_path_asks(self):
        assert _judge("read", {"operations": []}).action == TOOL_ALLOW

    def test_an_image_read_whose_targets_are_not_scanned_asks(self, note):
        image = {"mode": "Image", "image_paths": [note]}
        assert _judge("read", {"operations": [image]}).action == TOOL_ALLOW
        mixed = {"operations": [{"mode": "Line", "path": note}, image]}
        assert _judge("read", mixed).action == TOOL_ALLOW

    def test_a_listing_of_a_folder_that_holds_a_credential_store_asks(self):
        for path in ("~", os.path.expanduser("~"), "/"):
            raw = {"operations": [{"mode": "Directory", "path": path, "depth": 3}]}
            assert _judge("read", raw, title="Reading directory").action == TOOL_ALLOW

    def test_a_read_whose_containment_check_stalls_asks(self, note, monkeypatch):
        monkeypatch.setattr(hooks_mod.security, "path_contains_sensitive", lambda _p: True)
        assert _judge("read", _line_read(note)).action == TOOL_ALLOW

    @pytest.mark.parametrize("pattern", ["read", "fs_read"])
    def test_an_operator_confirm_pattern_still_asks(self, pattern, note):
        config = HooksConfig(confirm_tools=[pattern])
        assert _judge("read", _line_read(note), config=config).action == TOOL_ALLOW

    def test_the_sensitive_path_keystone_still_denies_first(self):
        result = _judge("read", _line_read("~/.ssh/id_rsa"), title="Reading id_rsa:1-3")
        assert result.action == TOOL_DENY


class TestKiroCliFrames:
    """kiro-cli's own frames, through the ACP parse, to the gate's verdict."""

    @staticmethod
    def _tool_call_frame() -> dict:
        for line in _KIRO_READ_FIXTURE.read_text(encoding="utf-8").splitlines():
            update = (json.loads(line).get("params") or {}).get("update") or {}
            if (
                update.get("sessionUpdate") == "tool_call"
                and (update.get("_meta") or {}).get("kiro", {}).get("toolName") == "read"
            ):
                return update
        raise AssertionError("the kiro fixture holds no built-in read")

    def _verdict(self, path: str):
        update = self._tool_call_frame()
        raw = update["rawInput"]
        raw["operations"][0]["path"] = path
        caches: dict = {
            "tool_input_cache": {},
            "shell_cache": {},
            "raw_params_cache": {},
            "mcp_server_name_cache": {},
            "tool_name_cache": {},
            "tool_kind_cache": {},
        }
        parse_session_update(update, **caches)
        # kiro-cli's request names the call, its title and its input: no kind.
        request = JsonRpcMessage(
            id=11,
            method="session/request_permission",
            params={
                "sessionId": "kiro-side-session-1",
                "toolCall": {
                    "toolCallId": update["toolCallId"],
                    "title": update["title"],
                    "rawInput": raw,
                },
                "options": [
                    {"optionId": "allow_once", "name": "Yes", "kind": "allow_once"},
                    {"optionId": "reject_once", "name": "No", "kind": "reject_once"},
                ],
            },
        )
        event, _ = build_permission_event(request, **caches)
        assert event is not None
        assert (event.tool_kind, event.tool_name, event.mcp_server_name) == ("", "read", "")
        assert event.mcp_identity_trusted is True
        return HookManager().judge(ToolCall.from_event(event), trust_reads=True)

    def test_an_absolute_read_runs_without_a_prompt(self, note):
        result = self._verdict(note)
        assert (result.action, result.read_only) == (TOOL_AUTO_APPROVE, True)

    def test_the_captured_relative_read_keeps_its_prompt(self):
        assert self._verdict("note.txt").action == TOOL_ALLOW
