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
