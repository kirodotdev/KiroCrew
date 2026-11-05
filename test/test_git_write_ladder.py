"""Each dashboard grant puts a git history write to the card under ``hooks.confirm_git_writes``.

Session trust, YOLO, the hook layer's auto-approve, a session trusted pattern and a
scoped worker grant each approved a ``git commit`` without a card before; now each
one asks, while a git read, the per-session allow and the setting turned off keep
the old outcome, and a hook deny still wins. Imports nothing this change adds, so
reverting the change leaves these tests collectable and failing.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import MagicMock

import pytest
from dashboard_approval_helpers import (
    _answer_approval,
    _complete_event,
    _context_builder,
    _drain,
    _make_slot,
    _make_state,
    _patch_stats,
    _set_stream,
)

from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.state import _ChatSlot, parse_cls_meta
from kiro_crew.hooks import ToolHookResult
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent


def _shell_event(command: str, request_id: str = "req-1") -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=command,
        tool_kind="execute",
        request_id=request_id,
        is_shell=True,
        tool_input=json.dumps({"command": command}),
    )


def _permission_rows(slot: _ChatSlot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") == "permission"]


async def _run(state, slot, client, command: str, *, answer: str | None = None):
    _set_stream(client, [_shell_event(command), _complete_event()])
    answerer = (
        asyncio.get_running_loop().create_task(_answer_approval(slot, "req-1", answer))
        if answer
        else None
    )
    try:
        with _patch_stats():
            await _run_chat(state, slot, "hello")
    finally:
        if answerer is not None:
            await _drain(answerer)


# ── The dashboard ladder ──


class TestGitWritesAskUnderEveryGrant:
    @pytest.mark.asyncio
    async def test_session_trust_asks_before_a_commit(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        await _run(state, slot, client, "git commit -m x", answer="approved")
        rows = _permission_rows(slot)
        assert len(rows) == 1
        assert parse_cls_meta(rows[0]["cls"]).get("git_write") == "1"
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_yolo_asks_before_a_push(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.enable_yolo()
        slot = _make_slot()
        await _run(state, slot, client, "git push origin topic", answer="approved")
        assert len(_permission_rows(slot)) == 1

    @pytest.mark.asyncio
    async def test_a_hook_auto_approve_asks_before_a_commit(self, tmp_path):
        cb = _context_builder(ToolHookResult(action="auto_approve"))
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        await _run(state, slot, client, "git commit -m x", answer="approved")
        assert len(_permission_rows(slot)) == 1

    @pytest.mark.asyncio
    async def test_a_session_trusted_pattern_asks_before_a_commit(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        slot._trusted_patterns = {"*"}
        await _run(state, slot, client, "git commit -m x", answer="approved")
        assert len(_permission_rows(slot)) == 1

    @pytest.mark.asyncio
    async def test_a_scoped_worker_grant_asks_before_a_commit(self, tmp_path, monkeypatch):
        from kiro_crew.dashboard import chat_runner

        override = MagicMock()
        override.is_scope_active.return_value = True
        monkeypatch.setattr(chat_runner, "safety_override", lambda: override)
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        slot._trust_scope = "app:worker:1"
        await _run(state, slot, client, "git commit -m x", answer="approved")
        assert len(_permission_rows(slot)) == 1

    @pytest.mark.asyncio
    async def test_a_rejected_card_does_not_run_the_commit(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        await _run(state, slot, client, "git commit -m x", answer="rejected")
        client.approve_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_commit_hidden_behind_a_description_title_still_asks(self, tmp_path):
        # The title is display text; the command the ladder reads is the one
        # the gate judged (``event.shell_command``).
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        event = _shell_event("git commit -am wip")
        event.title = "Check the working tree"
        _set_stream(client, [event, _complete_event()])
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        assert len(_permission_rows(slot)) == 1


class TestWhatStillRunsWithoutACard:
    @pytest.mark.asyncio
    async def test_trust_still_approves_a_git_read(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        await _run(state, slot, client, "git status")
        assert _permission_rows(slot) == []
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_per_session_allow_lets_trust_approve_a_commit(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        slot._git_writes_allowed = True
        await _run(state, slot, client, "git commit -m x")
        assert _permission_rows(slot) == []
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_allow_alone_does_not_approve_a_commit(self, tmp_path):
        # The allow lifts the confirm; it is not itself a grant.
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        slot._git_writes_allowed = True
        await _run(state, slot, client, "git commit -m x", answer="approved")
        rows = _permission_rows(slot)
        assert len(rows) == 1
        assert "git_write" not in parse_cls_meta(rows[0]["cls"])

    @pytest.mark.asyncio
    async def test_the_setting_off_restores_the_plain_ladder(self, tmp_path):
        cb = _context_builder()
        cb.hooks.confirm_git_writes = False
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot(trust=True)
        await _run(state, slot, client, "git commit -m x")
        assert _permission_rows(slot) == []
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_hook_deny_still_wins(self, tmp_path):
        cb = _context_builder(ToolHookResult.deny("blocked by policy"))
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot(trust=True)
        await _run(state, slot, client, "git commit -m x")
        assert _permission_rows(slot) == []
        client.approve_tool.assert_not_called()


class TestTheCardOffersOnlyTheSessionAllow:
    @pytest.mark.asyncio
    async def test_command_scoped_tiers_are_withheld_on_a_git_write_card(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        await _run(state, slot, client, "git commit -m x", answer="approved")
        meta = parse_cls_meta(_permission_rows(slot)[0]["cls"])
        assert meta["git_write"] == "1"
        assert "trust_command_grantable" not in meta
        assert "trust_base_grantable" not in meta


class TestAskingForTheAllowAlsoAsks:
    """Opening a session with ``allow_git_writes`` is itself confirmed under trust."""

    @staticmethod
    def _create_event(args: dict) -> LLMEvent:
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running: @kirocrew-dashboard/session_create",
            tool_kind="other",
            request_id="req-1",
            is_shell=False,
            tool_input=json.dumps(args),
            tool_name="session_create",
            mcp_server_name="kirocrew-dashboard",
        )

    @pytest.mark.asyncio
    async def test_trust_asks_before_opening_a_session_with_the_allow(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(
            client,
            [self._create_event({"title": "w", "allow_git_writes": True}), _complete_event()],
        )
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        rows = _permission_rows(slot)
        assert len(rows) == 1
        assert parse_cls_meta(rows[0]["cls"]).get("git_write") == "request"

    @pytest.mark.asyncio
    async def test_trust_still_approves_an_ordinary_session_create(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(client, [self._create_event({"title": "w"}), _complete_event()])
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        assert _permission_rows(slot) == []
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_request_card_does_not_offer_the_allow_itself(self, tmp_path):
        # Approving one child's allow must not grant the asking session: the
        # card is marked "request", which the slot-approve route refuses.
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(
            client,
            [self._create_event({"title": "w", "allow_git_writes": True}), _complete_event()],
        )
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        assert parse_cls_meta(_permission_rows(slot)[0]["cls"])["git_write"] == "request"
        assert "trust_grantable" not in parse_cls_meta(_permission_rows(slot)[0]["cls"])


class TestCallsWhoseArgumentsCannotBeRead:
    @pytest.mark.asyncio
    async def test_a_low_fidelity_child_with_a_verified_identity_asks(self, tmp_path):
        # The identity-keyed hook grant survives the low-fidelity downgrade, but
        # the arguments never arrived, so nothing shows the call is not a git
        # history write.
        cb = _context_builder(ToolHookResult(action="auto_approve", identity_grant=True))
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot(trust=True)
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running: @kirocrew-core/shell",
            tool_kind="other",
            request_id="req-1",
            is_shell=False,
            tool_name="shell",
            mcp_server_name="kirocrew-core",
            sub_session_id="child-1",
            mcp_identity_trusted=True,
        )
        assert event.child_low_fidelity and event.child_unconditional_grant_eligible
        _set_stream(client, [event, _complete_event()])
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        assert len(_permission_rows(slot)) == 1

    @pytest.mark.asyncio
    async def test_a_shell_call_without_a_readable_command_asks_with_no_gate(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=None)
        slot = _make_slot(trust=True)
        event = _shell_event("")
        event.title = "Running a shell command"
        event.tool_input = ""
        _set_stream(client, [event, _complete_event()])
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        assert len(_permission_rows(slot)) == 1


class TestFileToolsInsideGitDir:
    @staticmethod
    def _write_event(path: str) -> LLMEvent:
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Writing a file",
            tool_kind="edit",
            request_id="req-1",
            is_shell=False,
            tool_input=json.dumps({"path": path, "content": "x"}),
            tool_name="fs_write",
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "repo/.git/config",
            "/home/u/.gitconfig",
            "/home/u/.config/git/config",
            "/srv/repo.git/hooks/post-checkout",
            "C:\\Users\\u\\.gitconfig",
            "C:\\Users\\u\\.GITCONFIG",
            "C:\\repo\\.GIT\\config",
            "C:\\Users\\u\\.config\\GIT\\CONFIG",
            "/home/u/.gitconfig.local",
            ".git/hooks/x",
            "~/.gitconfig",
            "~/.GitConfig",
            "repo/.Git/Config",
            "repo/src/../.git/config",
            "../../.git/hooks/pre-commit",
        ],
    )
    async def test_trust_asks_before_a_file_write_inside_git(self, tmp_path, path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(client, [self._write_event(path), _complete_event()])
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        assert len(_permission_rows(slot)) == 1

    @pytest.mark.asyncio
    async def test_trust_still_approves_an_ordinary_file_write(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(client, [self._write_event("repo/.github/ci.yml"), _complete_event()])
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        assert _permission_rows(slot) == []

    @staticmethod
    def _event(tool_name: str, tool_kind: str, params: dict) -> LLMEvent:
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="A tool call",
            tool_kind=tool_kind,
            request_id="req-1",
            is_shell=False,
            tool_input=json.dumps(params),
            tool_name=tool_name,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool_name, tool_kind, params",
        [
            ("web_fetch", "fetch", {"url": "https://x/r.git"}),
            (
                "fs_write",
                "edit",
                {"path": "notes.md", "content": "see .git/config and ~/.gitconfig"},
            ),
            ("fs_write", "edit", {"path": ".dockerignore", "content": ".git\nnode_modules\n"}),
            ("memory_store", "other", {"text": "clone repo.git into .git/"}),
            ("str_replace", "edit", {"path": "a.md", "old_str": ".git", "new_str": ".gitconfig"}),
        ],
    )
    async def test_trust_approves_a_call_whose_only_git_text_is_not_a_write_path(
        self, tmp_path, tool_name, tool_kind, params
    ):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(client, [self._event(tool_name, tool_kind, params), _complete_event()])
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        assert _permission_rows(slot) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "params",
        [
            {"target": "repo/.git/config", "body": "x"},
            {"patchText": "*** Begin Patch\n*** Add File: .git/hooks/x\n+y\n*** End Patch"},
            {"path": "a.txt", "destination": ".git/config"},
            {"operations": [{"path": "a.txt", "destination": ".git/config"}]},
            {"options": {"save_as": "~/.gitconfig"}},
            {"paths": ["a.txt", ".git/hooks/pre-commit"]},
            {"path": "a.txt", "to": "/r/.git/config"},
            {"file_path": "/tmp/x", "copy_to": "~/.gitconfig"},
            {"path": "/tmp/a", "ops": [{"dest": "/r/.git/config"}]},
            {"url": "x", "save_to": "/r/.git/hooks/post-checkout"},
            {"path": "script.py", "code": "open('.git/config','w')"},
        ],
    )
    @pytest.mark.parametrize("tool_kind", ["edit", "move", "other", "read", ""])
    async def test_trust_asks_when_any_non_content_value_names_git(
        self, tmp_path, params, tool_kind
    ):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(client, [self._event("some_writer", tool_kind, params), _complete_event()])
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        assert len(_permission_rows(slot)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "raw_params, tool_input",
        [
            ({}, json.dumps({"path": ".git/hooks/pre-commit", "content": "x"})),
            ("guest", json.dumps({"path": ".git/hooks/pre-commit", "content": "x"})),
            ({"path": "a.txt"}, json.dumps(".git/config")),
            ({"path": "a.txt"}, json.dumps([".git/config"])),
            ({"uri": "file:///repo/.git/config"}, ""),
            ({"url": "FILE:///repo/.git/hooks/x"}, ""),
            ({"content": {"path": ".git/config"}}, ""),
            ({"patchText": " *** Add File: .git/hooks/x\n+y"}, ""),
            ({"patchText": "+y\r*** Add File: .git/hooks/x"}, ""),
        ],
    )
    async def test_trust_asks_when_either_argument_form_names_git(
        self, tmp_path, raw_params, tool_input
    ):
        from kiro_crew.platform.guest_paths import GuestPathParams

        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        event = self._event("some_writer", "edit", {})
        event.raw_tool_params = (
            GuestPathParams(None, root="/w") if raw_params == "guest" else raw_params
        )
        event.tool_input = tool_input
        _set_stream(client, [event, _complete_event()])
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        assert len(_permission_rows(slot)) == 1

    @pytest.mark.asyncio
    async def test_trust_approves_an_https_url_ending_in_dot_git(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        event = self._event("web_fetch", "fetch", {"uri": "https://x/repo.git"})
        event.raw_tool_params = {"uri": "https://x/repo.git"}
        _set_stream(client, [event, _complete_event()])
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        assert _permission_rows(slot) == []

    @pytest.mark.asyncio
    async def test_trust_asks_for_a_read_kind_whose_diff_block_names_git(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        event = self._event("reader", "read", {"note": "x"})
        event.diff_path = "/tmp/repo/.git/config"
        _set_stream(client, [event, _complete_event()])
        answerer = asyncio.get_running_loop().create_task(
            _answer_approval(slot, "req-1", "approved")
        )
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
        finally:
            await _drain(answerer)
        assert len(_permission_rows(slot)) == 1


def _path_event(params, tool_input=None):
    from types import SimpleNamespace

    return SimpleNamespace(
        raw_tool_params=params,
        tool_input=json.dumps(params) if tool_input is None else tool_input,
        diff_path="",
    )


@pytest.mark.parametrize(
    "brk", ["\r", "\x0c", "\x0b", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029"]
)
def test_a_patch_header_holding_another_line_break_asks(brk):
    from kiro_crew.dashboard.chat_turn.tool_approval import _names_git_path_target

    patch = (
        "*** Begin Patch\n*** Add File: " + brk + " /repo/.git/hooks/pre-commit\n"
        "+#!/bin/sh\n*** End Patch"
    )
    assert _names_git_path_target(_path_event({"patchText": patch}))


@pytest.mark.parametrize("depth", [100, 10_000])
def test_deeply_nested_arguments_ask_without_crashing(depth):
    from kiro_crew.dashboard.chat_turn.tool_approval import _names_git_path_target

    raw = "[" * depth + '"a.txt"' + "]" * depth
    assert _names_git_path_target(_path_event({}, raw))
    nested: object = "a.txt"
    for _ in range(depth):
        nested = [nested]
    assert _names_git_path_target(_path_event({"x": nested}, ""))


@pytest.mark.parametrize(
    "params",
    [
        {"patchText": "*** Begin Patch\n*** Add File: /repo/src/a.py\n+x = 1\n*** End Patch\n"},
        {
            "patchText": "*** Begin Patch\r\n*** Update File: /repo/a.md\r\n@@\r\n-a\r\n+see .git\r\n"
            "*** End Patch\r\n"
        },
        {"url": "https://x/repo.git"},
        {"path": "/repo/notes.md", "content": "the .git/config file and ~/.gitconfig"},
    ],
)
def test_ordinary_patches_urls_and_content_do_not_ask(params):
    from kiro_crew.dashboard.chat_turn.tool_approval import _names_git_path_target

    assert not _names_git_path_target(_path_event(params))


@pytest.mark.parametrize(
    "event_args",
    [
        (
            {
                "patchText": "*** Begin Patch\n*** Add File: /repo/sub/.git\ufeff/hooks/x\n+y\n*** End Patch"
            },
            None,
        ),
        (
            {
                "patchText": "*** Begin Patch\n*** Add File: /home/u/.config/git/config\ufeff\n+y\n*** End Patch"
            },
            None,
        ),
        ({"path": "/repo/sub/.git\ufeff"}, None),
        ({"nested": [{"target": "/repo/a\ufeff/b"}]}, None),
        ({"path\ufeff": "/repo/a.txt"}, None),
        ({}, '{"path": "/repo/.git\\ufeff/config"'),
    ],
)
def test_a_path_holding_a_byte_order_mark_asks(event_args):
    from kiro_crew.dashboard.chat_turn.tool_approval import _names_git_path_target

    params, raw = event_args
    assert _names_git_path_target(_path_event(params, raw))


def test_a_diff_path_holding_a_byte_order_mark_asks():
    from kiro_crew.dashboard.chat_turn.tool_approval import _names_git_path_target

    event = _path_event({"path": "/repo/a.txt"})
    event.diff_path = "/repo/.git\ufeff/config"
    assert _names_git_path_target(event)


@pytest.mark.parametrize(
    "params",
    [
        {"path": "/repo/a.txt", "content": "\ufeffhello"},
        {"patchText": "*** Begin Patch\n*** Add File: /repo/a.txt\n+\ufeffhello\n*** End Patch\n"},
    ],
)
def test_file_content_holding_a_byte_order_mark_does_not_ask(params):
    from kiro_crew.dashboard.chat_turn.tool_approval import _names_git_path_target

    assert not _names_git_path_target(_path_event(params))


def test_deeply_nested_arguments_naming_the_allow_ask_without_crashing():
    from types import SimpleNamespace

    from kiro_crew.dashboard.chat_turn.tool_approval import _requests_git_writes_grant

    raw = "[" * 10_000 + '"allow_git_writes"' + "]" * 10_000
    event = SimpleNamespace(raw_tool_params=None, tool_input=raw)
    assert _requests_git_writes_grant(event)
