"""Each dashboard grant puts a git history write to the card under ``hooks.confirm_git_writes``.

Session trust, YOLO, the hook layer's auto-approve, a session trusted pattern and a
scoped worker grant each approved a ``git commit`` without a card before; now each
one asks, every time, while a git read and the setting turned off keep the old
outcome, and a hook deny still wins. The card offers no standing grant. Imports nothing this change adds, so
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


def _notice_rows(slot: _ChatSlot) -> list[str]:
    return [m.get("content", "") for m in slot.messages if m.get("role") == "system"]


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
        assert parse_cls_meta(rows[0]["cls"]).get("git_confirm") == "1"
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


class TestNativeCrewAutoApprove:
    @staticmethod
    def _native_crew_on(monkeypatch):
        from kiro_crew.agent_sdk.backends import ACP_BACKEND_KIRO
        from kiro_crew.agent_sdk.capabilities import capabilities_for
        from kiro_crew.dashboard.chat_turn import tool_approval

        # The decider reads the predicate from its own module globals. The rung
        # runs only on a backend that tags its children's requests (kiro-cli).
        monkeypatch.setitem(
            tool_approval._decide_permission_request.__globals__,
            "_native_crew_should_auto_approve",
            lambda *_a, **_k: True,
        )
        monkeypatch.setitem(
            tool_approval._decide_permission_request.__globals__,
            "capabilities_of",
            lambda _client: capabilities_for(ACP_BACKEND_KIRO),
        )

    @staticmethod
    def _child_shell_event(command: str) -> LLMEvent:
        # The rung takes only a child's own call, as a kiro-cli frame marks it.
        event = _shell_event(command)
        event.sub_session_id = "child-1"
        event.child_calls_marked = True
        event.raw_tool_params = {"command": command}
        event.raw_params_trusted = True
        event.shell_classified = True
        return event

    async def _run_child(self, state, slot, client, command: str, *, answer: str | None = None):
        _set_stream(client, [self._child_shell_event(command), _complete_event()])
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

    @pytest.mark.asyncio
    async def test_a_python_heredoc_with_no_git_is_still_auto_approved(self, tmp_path, monkeypatch):
        self._native_crew_on(monkeypatch)
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        await self._run_child(
            state,
            slot,
            client,
            "python3 - <<'EOF'\nimport json\nprint(json.dumps({'a': 1}))\nEOF",
        )
        assert _permission_rows(slot) == []
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_commit_still_asks(self, tmp_path, monkeypatch):
        self._native_crew_on(monkeypatch)
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        await self._run_child(state, slot, client, "git commit -m x", answer="approved")
        assert len(_permission_rows(slot)) == 1


class TestTheCardOffersNoStandingGrant:
    """A confirm card is answered once: Allow once or Reject, nothing that lasts."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", ["git commit -m x", "echo $HOME > notes.txt", 'echo "$X"'])
    async def test_a_confirm_card_offers_no_tier_and_carries_the_confirm_mark(
        self, tmp_path, command
    ):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        await _run(state, slot, client, command, answer="approved")
        rows = _permission_rows(slot)
        assert len(rows) == 1
        meta = parse_cls_meta(rows[0]["cls"])
        assert meta.get("git_confirm") == "1"
        assert "git_write" not in meta
        assert "trust_grantable" not in meta
        assert "trust_command_grantable" not in meta
        assert "trust_base_grantable" not in meta
        assert not meta.get("is_read_only")
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_session_create_is_an_ordinary_call(self, tmp_path):
        # Opening a session grants nothing about git, so trust approves it.
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        args = {"title": "w"}
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running: @kirocrew-dashboard/session_create",
            tool_kind="other",
            request_id="req-1",
            is_shell=False,
            tool_input=json.dumps(args),
            raw_tool_params=args,
            tool_name="session_create",
            mcp_server_name="kirocrew-dashboard",
        )
        _set_stream(client, [event, _complete_event()])
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        assert _permission_rows(slot) == []
        client.approve_tool.assert_called_once()


class TestCallsWhoseArgumentsCannotBeRead:
    @pytest.mark.asyncio
    async def test_a_low_fidelity_child_with_a_verified_identity_keeps_its_grant(self, tmp_path):
        # A remote MCP tool's frame streams no arguments, but its server/tool
        # identity is verified. With its arguments present the call would never
        # ask (an MCP call is not read as a shell command), so missing arguments
        # do not make it ask either: the identity-keyed hook grant approves it.
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
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        assert _permission_rows(slot) == []
        assert _notice_rows(slot) == []
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_low_fidelity_child_with_an_unverified_identity_still_asks(self, tmp_path):
        # The same frame without a verified identity: nothing shows what the
        # call is, so it is put to the person even under trust.
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
            mcp_identity_trusted=False,
        )
        assert event.child_low_fidelity and not event.child_mcp_identity_trusted
        _set_stream(client, [event, _complete_event()])
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
        assert parse_cls_meta(rows[0]["cls"]).get("git_confirm") == "1"
        # The card says the arguments could not be checked and names the tool;
        # nothing showed it is a git command, so it does not say so.
        notices = _notice_rows(slot)
        assert len(notices) == 1
        assert "@kirocrew-core/shell" in notices[0]
        assert notices[0].startswith("The arguments of ")
        assert "could not be checked" in notices[0]
        assert "git" not in notices[0]

    @pytest.mark.asyncio
    async def test_a_low_fidelity_shell_child_still_asks(self, tmp_path):
        # A child shell call whose command never arrived asks, whatever identity
        # flag it carries: a shell call is never identity-trusted.
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running a shell command",
            tool_kind="execute",
            request_id="req-1",
            is_shell=True,
            sub_session_id="child-1",
            mcp_identity_trusted=True,
        )
        assert event.child_low_fidelity and not event.child_mcp_identity_trusted
        _set_stream(client, [event, _complete_event()])
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
        assert parse_cls_meta(rows[0]["cls"]).get("git_confirm") == "1"

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
        notices = _notice_rows(slot)
        assert len(notices) == 1
        assert "Running a shell command" in notices[0]
        assert notices[0].startswith("The arguments of ")
        assert "could not be checked" in notices[0]
        assert "git" not in notices[0]

    @pytest.mark.asyncio
    async def test_a_remote_mcp_child_call_with_a_verified_identity_is_approved_by_trust(
        self, tmp_path
    ):
        # A backend subagent calls a remote HTTP MCP tool; its frame carries no
        # arguments but a verified identity. Session trust approves it with no
        # card, as it does when the arguments are present.
        cb = _context_builder(ToolHookResult(action="auto_approve", identity_grant=True))
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot(trust=True)
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="Running: @acme-tools/deploy",
            tool_kind="other",
            request_id="req-1",
            is_shell=False,
            tool_name="deploy",
            mcp_server_name="acme-tools",
            sub_session_id="child-1",
            mcp_identity_trusted=True,
        )
        assert event.child_low_fidelity and event.child_mcp_identity_trusted
        _set_stream(client, [event, _complete_event()])
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        assert _permission_rows(slot) == []
        client.approve_tool.assert_called_once()


class TestAShellToolStreamedUnderAnotherKind:
    """A harness may stream its shell tool under a kind other than ``execute``
    (the DeepSeek harness sends ``bash`` as ``other``), so ``is_shell`` stays
    False. The tool's own ``command`` argument is still the command it runs."""

    @staticmethod
    def _bash_as_other(command: str) -> LLMEvent:
        params = {"command": command, "description": "run it"}
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="bash",
            tool_kind="other",
            request_id="req-1",
            is_shell=False,
            raw_tool_params=params,
            raw_params_trusted=True,
            tool_input=json.dumps(params),
        )

    @pytest.mark.asyncio
    async def test_session_trust_asks_before_a_commit(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(
            client, [self._bash_as_other("git commit -am x && git push"), _complete_event()]
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
        assert parse_cls_meta(rows[0]["cls"]).get("git_confirm") == "1"
        notices = _notice_rows(slot)
        assert len(notices) == 1
        assert "git command is not a known read-only one" in notices[0]

    @pytest.mark.asyncio
    async def test_yolo_asks_before_a_push_named_by_cmd(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.enable_yolo()
        slot = _make_slot()
        params = {"cmd": "git push origin topic"}
        event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="bash",
            tool_kind="other",
            request_id="req-1",
            is_shell=False,
            raw_tool_params=params,
            raw_params_trusted=True,
            tool_input=json.dumps(params),
        )
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
    async def test_session_trust_still_approves_a_git_read(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(client, [self._bash_as_other("git status"), _complete_event()])
        with _patch_stats():
            await _run_chat(state, slot, "hello")
        assert _permission_rows(slot) == []
        client.approve_tool.assert_called_once()


class TestATimedOutConfirmCard:
    @pytest.mark.asyncio
    async def test_it_is_not_told_to_turn_yolo_on(self, tmp_path):
        # YOLO never answers a git-confirm card, so the decline note after the
        # approval window must not advise turning it on.
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.approval_timeout_for = MagicMock(return_value=0.05)
        slot = _make_slot()
        assert not state.is_yolo_active()
        await _run(state, slot, client, "git commit -m x")
        rows = _permission_rows(slot)
        assert len(rows) == 1
        assert parse_cls_meta(rows[0]["cls"]).get("git_confirm") == "1"
        client.approve_tool.assert_not_called()
        [card] = [
            m["content"]
            for m in slot.messages
            if m.get("role") == "error" and "nothing came back within" in m["content"]
        ]
        assert "turn on YOLO mode" not in card
        assert card.endswith(
            "approve the prompt when it appears. YOLO mode does not cover this call."
        )
