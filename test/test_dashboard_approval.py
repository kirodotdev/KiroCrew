"""Tests for dashboard tool approval flow — normal/trust/yolo modes."""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.state import (
    REFUSAL_RECOVERY_PREFIX,
    DashboardState,
    _ChatSlot,
    build_refusal_recovery_prompt,
    parse_cls_meta,
)
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import ToolHookResult
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    LLMEvent,
)

# ── Helpers ──


async def _async_iter(items: list):  # type: ignore[type-arg]
    for item in items:
        yield item


@contextmanager
def _patch_stats():
    with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
        mock_sel.return_value = MagicMock()
        yield


def _permission_event(
    title: str = "fs_write",
    tool_kind: str = "edit",
) -> LLMEvent:
    return LLMEvent(
        kind=EVENT_PERMISSION_REQUEST,
        title=title,
        tool_kind=tool_kind,
        request_id="req-1",
    )


def _complete_event() -> LLMEvent:
    return LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")


def _make_hook_store() -> MagicMock:
    hs = MagicMock()
    hs.fire = AsyncMock(return_value=[])
    return hs


def _blocking_hook_store(reason: str, hook_name: str = "policy-hook") -> MagicMock:
    """Hook store whose PreToolUse fire blocks (exit 2) with *reason* on stderr."""
    hs = _make_hook_store()
    hs.fire = AsyncMock(
        return_value=[
            MagicMock(exit_code=2, stderr=reason, stdout="", hook_name=hook_name)
        ]
    )
    return hs


async def _drive_hook_blocked_turn(
    state, client, slot, *, approve_prompt: bool = False, title: str = "fs_write"
) -> None:
    """Run one turn whose only tool call is blocked by a PreToolUse script hook.

    Only the first stream yields a permission request, so the automatic recovery
    continuation completes instead of blocking again. ``approve_prompt`` answers
    the interactive permission future, which is the only way to reach the hook
    fire that happens after the user approves.
    """
    client.context_usage_pct = MagicMock(return_value=0.0)
    client._client = client
    client.last_prompt_stats = None
    calls = {"n": 0}

    def _stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _async_iter([_permission_event(title=title), _complete_event()])
        return _async_iter([_complete_event()])

    client.stream = MagicMock(side_effect=_stream)

    approver = None
    if approve_prompt:

        async def _answer() -> None:
            for _ in range(600):
                fut = slot._approval_futures.get("req-1")
                if fut is not None:
                    if not fut.done():
                        fut.set_result("approved")
                    return
                await asyncio.sleep(0.01)

        approver = asyncio.get_event_loop().create_task(_answer())

    with _patch_stats():
        await _run_chat(state, slot, "hello")
        if slot.task:
            await slot.task

    if approver is not None:
        await _drain(approver)


def _assert_block_reason_recovered(slot, client, reason: str) -> None:
    """Assert the call was rejected and *reason* reached a recovery continuation.

    Selected by content, not position: a turn can also enqueue the
    empty-response nudge, so the last inject is not reliably the recovery one.
    """
    client.reject_tool.assert_called_once()
    recoveries = [
        message.get("content", "")
        for message in slot.messages
        if message.get("role") == "inject"
        and message.get("content", "").startswith(REFUSAL_RECOVERY_PREFIX)
    ]
    assert recoveries, (
        "Script-hook block must trigger refusal-recovery; injects were "
        f"{[m.get('content', '')[:40] for m in slot.messages if m.get('role') == 'inject']}"
    )
    assert any(reason.lower() in recovery.lower() for recovery in recoveries)


def _make_state(
    tmp_path,
    context_builder=None,
    hook_store=None,
) -> tuple[DashboardState, AsyncMock]:
    """Return (state, client) with all async methods properly mocked."""
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    client = AsyncMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(
            list_jobs=MagicMock(return_value=[]),
            status=MagicMock(return_value={}),
        ),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    state.context_builder = context_builder
    state._hook_store = hook_store or _make_hook_store()
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    return state, client


def _make_slot(key: str = "chat-1-test", trust: bool = False) -> _ChatSlot:
    slot = _ChatSlot(key)
    slot._trust = trust
    return slot


def _set_stream(client: AsyncMock, events: list[LLMEvent]) -> None:
    """Make client.stream() return an async iterable of events."""
    client.stream = MagicMock(side_effect=lambda *a, **kw: _async_iter(events))


def _tool_messages(slot: _ChatSlot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") in ("tool", "permission")]


def _context_builder(hook_result: ToolHookResult = ToolHookResult.allow()) -> MagicMock:
    cb = MagicMock()
    cb.hooks.on_tool_call.return_value = hook_result
    cb.build_message.return_value = ("hello", None)
    return cb


async def _drain(task: asyncio.Task) -> None:
    """Cancel *task* and await it, so it cannot outlive the test.

    A helper task left running is garbage-collected on a later test's loop, which
    reports "coroutine ignored GeneratorExit" against an innocent test.
    """
    if not task.done():
        task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── Tests ──


class TestApprovalModes:
    """Verify that normal/trust/yolo modes route permission requests correctly."""

    @pytest.mark.asyncio
    async def test_normal_mode_prompts_interactively(self, tmp_path):
        """Normal mode (no trust, no yolo) must send a permission message."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        # Poll for the future rather than sleeping a fixed 0.05s and hoping the chat
        # has registered it by then, and keep a handle so the helper is cancelled
        # instead of being GC'd mid-flight during a later test.
        async def _auto_approve():
            for _ in range(600):
                fut = slot._approval_futures.get("req-1")
                if fut is not None:
                    if not fut.done():
                        fut.set_result("approved")
                    return
                await asyncio.sleep(0.01)

        approver = asyncio.get_event_loop().create_task(_auto_approve())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(approver)

        msgs = _tool_messages(slot)
        assert any(
            m["role"] == "permission" for m in msgs
        ), f"Expected interactive prompt, got: {msgs}"
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_trust_mode_auto_approves(self, tmp_path):
        """Trust mode must auto-approve without interactive prompt."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        msgs = _tool_messages(slot)
        assert not any(m["role"] == "permission" for m in msgs), "Trust mode should not prompt"
        # Auto-approved tools are broadcast via WS, not appended to slot
        state.broadcast_ws.assert_any_call(
            "tool_call",
            {
                "slot": slot.key,
                "tool": _permission_event().title,
                "kind": _permission_event().tool_kind,
                "auto": True,
                "tool_call_id": "",
                "purpose": "",
                "input_preview": "",
            },
        )
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_yolo_mode_auto_approves(self, tmp_path):
        """YOLO mode must auto-approve without interactive prompt."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.enable_yolo()
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        msgs = _tool_messages(slot)
        assert not any(m["role"] == "permission" for m in msgs), "YOLO mode should not prompt"
        state.broadcast_ws.assert_any_call(
            "tool_call",
            {
                "slot": slot.key,
                "tool": _permission_event().title,
                "kind": _permission_event().tool_kind,
                "auto": True,
                "tool_call_id": "",
                "purpose": "",
                "input_preview": "",
            },
        )
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_hook_deny_rejects(self, tmp_path):
        """Hook deny must reject the tool without prompting."""
        cb = _context_builder(ToolHookResult.deny("blocked by policy"))
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        msgs = _tool_messages(slot)
        assert any("blocked" in m.get("content", "").lower() for m in msgs)
        client.reject_tool.assert_called_once()
        client.approve_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_hook_deny_pill_includes_reason(self, tmp_path):
        """The blocked pill must carry the deny reason, not just '(blocked)',
        so the user learns WHY (e.g. 'Blocked by security policy: git push')
        instead of seeing a silent/cryptic stop."""
        cb = _context_builder(
            ToolHookResult.deny("Blocked by security policy: git push")
        )
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        msgs = _tool_messages(slot)
        assert any(
            "security policy: git push" in m.get("content", "").lower()
            for m in msgs
        ), [m.get("content") for m in msgs]

    @pytest.mark.asyncio
    async def test_hook_deny_broadcasts_activity_event(self, tmp_path):
        """A host-gate deny must broadcast a visible activity_event (mirroring
        the auto-approve branch) so the block is not silent."""
        cb = _context_builder(
            ToolHookResult.deny("Blocked by security policy: git push")
        )
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        perm_activity = [
            c.args
            for c in state.broadcast_ws.call_args_list
            if c.args and c.args[0] == "activity_event"
            and isinstance(c.args[1], dict)
            and c.args[1].get("kind") == "permission"
        ]
        assert perm_activity, state.broadcast_ws.call_args_list
        # The broadcast text should mention the block.
        assert any(
            "block" in a[1].get("text", "").lower() for a in perm_activity
        ), perm_activity

    @pytest.mark.asyncio
    async def test_hook_auto_approve_skips_prompt(self, tmp_path):
        """Hook auto-approve must approve without interactive prompt."""
        cb = _context_builder(ToolHookResult.auto_approve())
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        assert not any(m["role"] == "permission" for m in _tool_messages(slot))
        client.approve_tool.assert_called_once()
        client.reject_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_approve_still_fires_pretooluse_script_hook(self, tmp_path):
        """Auto-approve must NOT bypass scripted PreToolUse hooks (audit gate)."""
        from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE

        cb = _context_builder(ToolHookResult.auto_approve())
        hook_store = _make_hook_store()
        state, client = _make_state(tmp_path, context_builder=cb, hook_store=hook_store)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        # Hook must have fired with PreToolUse before approval.
        events_fired = [c.args[0] for c in hook_store.fire.call_args_list]
        assert HOOK_EVENT_PRE_TOOL_USE in events_fired, events_fired
        # Tool must still be approved (empty hook results = pass-through).
        client.approve_tool.assert_called_once()
        client.reject_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_auto_approve_blocked_by_pretooluse_script_hook(self, tmp_path):
        """Exit-2 PreToolUse hook must override auto-approve and reject the tool.

        chat_runner's inner _fire() helper translates a ScriptHookResult
        with exit_code=2 into a 'BLOCKED:<name>:<stderr>' marker string
        before the auto-approve branch checks startswith('BLOCKED:'). The
        mock returns ScriptHookResult-shaped objects so the full
        translation path runs.
        """
        cb = _context_builder(ToolHookResult.auto_approve())
        hook_store = _make_hook_store()
        # ScriptHookResult-shaped mock: _fire() reads .exit_code/.stderr/
        # .hook_name and converts exit-2 into the BLOCKED: string the
        # auto-approve branch checks.
        blocked_result = MagicMock(
            exit_code=2,
            stderr="policy denial",
            stdout="",
            hook_name="test-blocker",
        )
        hook_store.fire = AsyncMock(return_value=[blocked_result])
        state, client = _make_state(tmp_path, context_builder=cb, hook_store=hook_store)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        # Tool must be rejected because the script hook blocked it.
        client.reject_tool.assert_called_once()
        client.approve_tool.assert_not_called()
        # User-facing pill must reflect the block (NOT a hook_error).
        msgs = _tool_messages(slot)
        assert any(
            "hook blocked" in m.get("content", "").lower() for m in msgs
        ), msgs

    @pytest.mark.asyncio
    async def test_auto_approve_deny_by_default_on_unexpected_hook_output(self, tmp_path):
        """Non-list/None hook return must reject the tool (deny-by-default)."""
        cb = _context_builder(ToolHookResult.auto_approve())
        hook_store = _make_hook_store()
        # Simulate a misbehaving fire() returning None (e.g. store
        # misconfiguration, race). Iterating None would raise TypeError;
        # the inner guard must reject explicitly rather than fall through.
        hook_store.fire = AsyncMock(return_value=None)
        state, client = _make_state(tmp_path, context_builder=cb, hook_store=hook_store)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        # Deny-by-default: auto-approve must NOT silently approve on bad hook output.
        client.approve_tool.assert_not_called()
        client.reject_tool.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.xdist_group(name="serial")
    async def test_interactive_reject(self, tmp_path):
        """User rejecting interactively must call reject_tool."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        async def _auto_reject():
            await asyncio.sleep(0.05)
            fut = slot._approval_futures.get("req-1")
            if fut and not fut.done():
                fut.set_result("rejected")

        asyncio.get_event_loop().create_task(_auto_reject())

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        client.reject_tool.assert_called_once()

    @pytest.mark.asyncio
    async def test_interactive_approve_with_empty_hooks(self, tmp_path):
        """After interactive approve, empty hook results must NOT reject."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        async def _auto_approve():
            for _ in range(600):
                fut = slot._approval_futures.get("req-1")
                if fut is not None:
                    if not fut.done():
                        fut.set_result("approved")
                    return
                await asyncio.sleep(0.01)

        approver = asyncio.get_event_loop().create_task(_auto_approve())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(approver)

        msgs = _tool_messages(slot)
        assert not any(
            "no hooks" in m.get("content", "") for m in msgs
        ), f"Empty hook results should not reject: {msgs}"
        client.approve_tool.assert_called_once()


class TestTrustYoloPropagation:
    """Trust/YOLO mode propagates approval policy to session manager."""

    @pytest.mark.asyncio
    async def test_run_chat_propagates_trust_to_session(self, tmp_path):
        """When slot has _trust=True, _run_chat sets session approval policy to auto."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        _set_stream(client, [_complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        state.sessions.set_approval_policy.assert_called_with(f"dashboard:{slot.key}", "auto")

    @pytest.mark.asyncio
    async def test_run_chat_propagates_yolo_to_session(self, tmp_path):
        """When state._yolo=True, _run_chat sets session approval policy to auto."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.enable_yolo()
        slot = _make_slot()
        _set_stream(client, [_complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        state.sessions.set_approval_policy.assert_called_with(f"dashboard:{slot.key}", "auto")

    @pytest.mark.asyncio
    @pytest.mark.xdist_group(name="serial")
    async def test_run_chat_no_propagation_without_trust_or_yolo(self, tmp_path):
        """Without trust or YOLO, set_approval_policy clears to default."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        _set_stream(client, [_complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        state.sessions.set_approval_policy.assert_called_once_with(f"dashboard:{slot.key}", "")


class TestResolveApprovalSlotFallback:
    """resolve_approval falls through to slot-level futures for chat tool approvals."""

    @pytest.mark.asyncio
    async def test_resolves_slot_future_when_state_has_none(self, tmp_path):
        """resolve_approval finds futures in slot._approval_futures."""
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-42"] = fut

        result = state.resolve_approval("req-42", True)

        assert result is True
        assert fut.done()
        assert fut.result() == "approved"
        state.broadcast_ws.assert_called_with(
            "approval_resolved", {"id": "req-42", "approved": True}
        )
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_slot_reject(self, tmp_path):
        """resolve_approval rejects slot futures correctly."""
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-43"] = fut

        result = state.resolve_approval("req-43", False)

        assert result is True
        assert fut.result() == "rejected"

    @pytest.mark.asyncio
    async def test_state_futures_checked_first(self, tmp_path):
        """State-level futures take priority over slot-level."""
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        state_fut: asyncio.Future[bool] = loop.create_future()
        slot_fut: asyncio.Future[str] = loop.create_future()
        state._approval_futures["req-44"] = state_fut
        slot._approval_futures["req-44"] = slot_fut

        state.resolve_approval("req-44", True)

        assert state_fut.done()
        assert not slot_fut.done(), "Slot future should not be touched when state future exists"

    @pytest.mark.asyncio
    async def test_returns_false_when_not_found(self, tmp_path):
        """resolve_approval returns False when ID not in state or any slot."""
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        assert state.resolve_approval("nonexistent", True) is False


class TestToolCallIdRedaction:
    """Verify tool_call_id is redacted before use in event loop."""

    @pytest.mark.asyncio
    async def test_tool_call_id_redacted_in_trust_mode(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        evt = _permission_event()
        evt.tool_call_id = "tcid-clean"
        evt.tool_purpose = "test purpose"
        _set_stream(client, [evt, _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        # Trust mode broadcasts tool_call via WS with tool_call_id
        state.broadcast_ws.assert_any_call(
            "tool_call",
            {
                "slot": slot.key, "tool": evt.title, "kind": evt.tool_kind,
                "auto": True, "tool_call_id": "tcid-clean",
                "purpose": "test purpose", "input_preview": "",
            },
        )


class TestBatchRejection:
    """Verify batch rejection auto-rejects remaining tools."""

    @pytest.mark.asyncio
    async def test_batch_rejection_auto_rejects_remaining(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        async def _reject_first():
            await asyncio.sleep(0.05)
            fut = slot._approval_futures.get("req-1")
            if fut and not fut.done():
                fut.set_result("rejected")

        asyncio.get_event_loop().create_task(_reject_first())

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        # First tool rejected interactively, second auto-rejected
        client.reject_tool.assert_any_call("req-1")
        client.reject_tool.assert_any_call("req-2")
        assert slot._batch_rejected is False  # reset in finally

    @pytest.mark.asyncio
    async def test_batch_rejected_reset_on_exception(self, tmp_path):
        """_batch_rejected is reset even if event loop raises."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        slot._batch_rejected = True

        async def _exploding_stream():
            yield _permission_event()
            raise RuntimeError("boom")

        client.stream = MagicMock(side_effect=lambda *a, **kw: _exploding_stream())

        with _patch_stats():
            try:
                await _run_chat(state, slot, "hello")
            except RuntimeError:
                pass

        assert slot._batch_rejected is False


class TestToolCompletionTracking:
    """Verify tool completion state tracking."""

    @pytest.mark.asyncio
    async def test_trust_mode_with_tool_call_id(self, tmp_path):
        """Trust mode auto-approve broadcasts tool_call_id in WS."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot(trust=True)
        evt = _permission_event()
        evt.tool_call_id = "tc-42"
        _set_stream(client, [evt, _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        # Verify tool_call broadcast includes tool_call_id
        calls = [c for c in state.broadcast_ws.call_args_list if c[0][0] == "tool_call"]
        assert len(calls) > 0
        assert calls[0][0][1]["tool_call_id"] == "tc-42"


class TestBackgroundApprovalDenyFast:
    """Background sources (cron/heartbeat/taskrunner) deny-fast on a short window
    instead of burning the full 2h human window (F4).

    No human is present for unattended turns, so request_approval must wait only
    _BACKGROUND_APPROVAL_TIMEOUT_SECS and then deny. Interactive sources keep the
    long _APPROVAL_TIMEOUT window.
    """

    @pytest.mark.asyncio
    async def test_background_uses_short_window_and_denies(self, tmp_path, monkeypatch):
        state, _ = _make_state(tmp_path)
        captured = {}

        async def _fake_wait_for(fut, timeout):
            captured["timeout"] = timeout
            fut.cancel()  # don't leave a dangling future
            raise asyncio.TimeoutError

        monkeypatch.setattr("kiro_crew.dashboard.state.asyncio.wait_for", _fake_wait_for)

        result = await state.request_approval(
            "req-bg", "heartbeat", "fs_write", is_background=True
        )

        assert result is False  # deny-fast on expiry
        assert captured["timeout"] == DashboardState._BACKGROUND_APPROVAL_TIMEOUT_SECS
        # The short window is far below the 2h human window.
        assert captured["timeout"] < DashboardState._APPROVAL_TIMEOUT
        # Pending state cleaned up.
        assert "req-bg" not in state._pending_approvals
        assert "req-bg" not in state._approval_futures

    @pytest.mark.asyncio
    async def test_interactive_uses_long_window(self, tmp_path, monkeypatch):
        state, _ = _make_state(tmp_path)
        captured = {}

        async def _fake_wait_for(fut, timeout):
            captured["timeout"] = timeout
            fut.cancel()
            raise asyncio.TimeoutError

        monkeypatch.setattr("kiro_crew.dashboard.state.asyncio.wait_for", _fake_wait_for)

        # Default is_background=False — interactive dashboard/slack path.
        result = await state.request_approval("req-ui", "dashboard", "fs_write")

        assert result is False  # timeout still denies (pauses) for interactive
        assert captured["timeout"] == DashboardState._APPROVAL_TIMEOUT

    @pytest.mark.asyncio
    async def test_background_approve_before_timeout_returns_true(self, tmp_path):
        """A background approval that IS answered in time still approves."""
        state, _ = _make_state(tmp_path)

        async def _approve_soon():
            await asyncio.sleep(0.01)
            state.resolve_approval("req-bg2", True)

        asyncio.get_event_loop().create_task(_approve_soon())
        result = await state.request_approval(
            "req-bg2", "cron", "fs_write", is_background=True
        )
        assert result is True


class TestStateMetaAndPermissions:
    """Cover state.py meta handling and permission resolution."""

    def test_append_with_meta(self):
        slot = _make_slot()
        slot.append("tool", "test", meta={"tool_call_id": "tc-1", "purpose": "testing"}, broadcast=False)
        assert slot.messages[-1]["meta"]["tool_call_id"] == "tc-1"

    def test_mark_permission_resolved(self):
        import json
        slot = _make_slot()
        cls_data = json.dumps({"request_id": "req-42"})
        slot.append("permission", "tool_x", cls_data, broadcast=False)
        slot.mark_permission_resolved("req-42", "rejected")
        updated = json.loads(slot.messages[-1]["cls"])
        assert updated["resolved"] == "rejected"

    def test_mark_permission_resolved_not_found(self):
        slot = _make_slot()
        # Should not raise
        slot.mark_permission_resolved("nonexistent", "approved")

    def test_parse_cls_meta_normalizes_request_id(self):
        meta = parse_cls_meta('{"request_id": "req-1", "tool_input": "x"}')
        assert "approval_id" in meta
        assert "request_id" not in meta

    def test_meta_stored_on_message(self):
        slot = _make_slot()
        slot.append("tool", "test", meta={"tool_call_id": "tc-1"}, broadcast=False)
        assert slot.messages[-1].get("meta", {}).get("tool_call_id") == "tc-1"


class TestRefusalRecovery:
    """A recoverable refusal (host-gate policy deny / read-only bash gate) ends
    the turn via kiro-cli's tool-interrupted marker. KiroCrew should hand the
    reason back to the model as an auto-continuation so the agent can adapt
    instead of stalling — without the user having to poke it."""

    @pytest.mark.asyncio
    async def test_host_gate_deny_enqueues_recovery_continuation(self, tmp_path):
        """A host-gate deny records the reason and the finally-block dequeue
        re-dispatches it as an 'inject' continuation carrying that reason."""
        cb = _context_builder(
            ToolHookResult.deny("Blocked by security policy: git push")
        )
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()

        # The AsyncMock client returns coroutines for sync getters; give the
        # success-tail context-usage readout real values so it doesn't raise
        # before the recovery step (production always has real numbers here).
        client.context_usage_pct = MagicMock(return_value=0.0)
        client._client = client
        client.last_prompt_stats = None

        # First turn denies; the recovery continuation turn streams clean so the
        # loop terminates (no artificial cap — the model would simply stop here).
        calls = {"n": 0}

        def _stream(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _async_iter([_permission_event(), _complete_event()])
            return _async_iter([_complete_event()])

        client.stream = MagicMock(side_effect=_stream)

        with _patch_stats():
            await _run_chat(state, slot, "hello")
            # Drain the auto-dispatched recovery turn so no task is left pending.
            if slot.task:
                await slot.task

        injects = [m for m in slot.messages if m.get("role") == "inject"]
        assert injects, "expected an injected recovery continuation"
        recovery = injects[-1]["content"]
        assert recovery.startswith(REFUSAL_RECOVERY_PREFIX)
        assert "security policy: git push" in recovery.lower()
        assert "NOT a user action" in recovery
        # The synthetic prompt is delivered to the model (a 2nd stream call).
        assert calls["n"] >= 2
        client.reject_tool.assert_called()

    @pytest.mark.asyncio
    async def test_clean_turn_does_not_enqueue_recovery(self, tmp_path):
        """An auto-approved tool with no refusal must not trigger recovery."""
        cb = _context_builder(ToolHookResult.auto_approve())
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")
            if slot.task:
                await slot.task

        assert not any(
            REFUSAL_RECOVERY_PREFIX in m.get("content", "") for m in slot.messages
        )
        assert not slot._queue


class TestBuildRefusalRecoveryPrompt:
    """build_refusal_recovery_prompt hands a tool-refusal reason back to the
    model so it can adapt, instead of the turn stalling silently."""

    def test_empty_returns_empty(self):
        assert build_refusal_recovery_prompt([]) == ""

    def test_single_refusal_includes_title_and_reason(self):
        out = build_refusal_recovery_prompt(
            [("bash", "command 'python' is not on the read-only allowlist")]
        )
        assert "bash" in out
        assert "not on the read-only allowlist" in out
        # Frames the block as a system decision, NOT a user cancellation.
        assert "NOT a user action" in out
        assert "not treat it as a cancellation" in out
        # Tells the model it may adapt or stop on its own.
        assert "alternative" in out.lower()

    def test_multiple_refusals_all_listed(self):
        out = build_refusal_recovery_prompt(
            [("bash", "unsafe shell pattern"), ("fs_write", "blocked by policy")]
        )
        assert "bash" in out and "unsafe shell pattern" in out
        assert "fs_write" in out and "blocked by policy" in out
        assert out.count("- ") >= 2

    def test_missing_reason_still_lists_title(self):
        out = build_refusal_recovery_prompt([("some_tool", "")])
        assert "some_tool" in out

    def test_body_excludes_prefix(self):
        # The caller prepends REFUSAL_RECOVERY_PREFIX; the body must not.
        out = build_refusal_recovery_prompt([("bash", "reason")])
        assert REFUSAL_RECOVERY_PREFIX not in out


class TestPendingProjectReset:
    """Locks in the start-of-turn / end-of-turn dual-consume contract for
    `slot._pending_reset_history_key`. See chat_runner._run_chat for context."""

    @pytest.mark.asyncio
    async def test_start_of_turn_resets_before_get_or_create(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        slot._pending_reset_history_key = "dashboard:chat-1-test"
        state.sessions.reset = AsyncMock()
        _set_stream(client, [_complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        state.sessions.reset.assert_any_await("dashboard:chat-1-test")
        # reset() must appear before get_or_create() on the parent sessions mock.
        sess_calls = state.sessions.mock_calls
        reset_pos = next(i for i, c in enumerate(sess_calls) if c[0] == "reset")
        goc_pos = next(i for i, c in enumerate(sess_calls) if c[0] == "get_or_create")
        assert reset_pos < goc_pos
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_no_pending_flag_does_not_reset(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        state.sessions.reset = AsyncMock()
        _set_stream(client, [_complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        state.sessions.reset.assert_not_awaited()
        assert slot._pending_reset_history_key is None

    @pytest.mark.asyncio
    async def test_reset_failure_retains_flag_for_retry(self, tmp_path):
        """If reset() raises, the flag stays set so the next turn can retry."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        slot._pending_reset_history_key = "dashboard:chat-1-test"
        state.sessions.reset = AsyncMock(side_effect=RuntimeError("reset failed"))
        _set_stream(client, [_complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        assert slot._pending_reset_history_key == "dashboard:chat-1-test"

    @pytest.mark.asyncio
    async def test_end_of_turn_consumes_flag_set_mid_turn(self, tmp_path):
        """Flag set mid-turn (by set_project MCP tool) is consumed in finally."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        state.sessions.reset = AsyncMock()

        def set_flag_mid_stream(*args, **kwargs):
            slot._pending_reset_history_key = "dashboard:chat-1-test"
            return _async_iter([_complete_event()])

        client.stream = MagicMock(side_effect=set_flag_mid_stream)

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        state.sessions.reset.assert_any_await("dashboard:chat-1-test")
        assert slot._pending_reset_history_key is None


class TestInteractiveDenyDoesNotTriggerRecovery:
    """an interactive user denial (clicking Reject in the dashboard)
    must NOT populate _refusal_reasons or trigger refusal-recovery. Only
    system-side blocks (hook deny at ~L1974) should trigger recovery."""

    @pytest.mark.asyncio
    async def test_interactive_reject_does_not_enqueue_recovery(self, tmp_path):
        """User clicks Reject on a bash command that would fail the safety gate.
        The turn should end cleanly with NO recovery continuation injected."""
        # Use allow() so we reach the interactive approval path (not auto-deny).
        cb = _context_builder(ToolHookResult.allow())
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()

        # Provide context-usage mock so post-stream bookkeeping doesn't raise.
        client.context_usage_pct = MagicMock(return_value=0.0)
        client._client = client
        client.last_prompt_stats = None

        # A bash command NOT on the read-only allowlist — triggers unsafe_bash_reason.
        bash_event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="execute_bash: python3 -c 'print(1)'",
            tool_kind="bash",
            request_id="req-1",
            tool_input='{"command": "python3 -c \'print(1)\'"}',
        )

        # Multi-call stream: first call yields the permission event (triggers
        # interactive approval flow + rejection). The empty-response retry
        # re-queues and calls stream again; return a clean completion so the
        # turn terminates without requesting another tool approval.
        calls = {"n": 0}

        def _stream(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _async_iter([bash_event, _complete_event()])
            return _async_iter([_complete_event()])

        client.stream = MagicMock(side_effect=_stream)

        # Simulate the user clicking Reject after a short delay.
        async def _auto_reject():
            await asyncio.sleep(0.05)
            fut = slot._approval_futures.get("req-1")
            if fut and not fut.done():
                fut.set_result("rejected")

        asyncio.get_running_loop().create_task(_auto_reject())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
            if slot.task:
                await slot.task

        # The key assertion: no recovery continuation was injected.
        assert not any(
            REFUSAL_RECOVERY_PREFIX in m.get("content", "") for m in slot.messages
        ), "Interactive user deny must NOT trigger refusal-recovery"
        # The tool was rejected (not approved).
        client.reject_tool.assert_called()
        client.approve_tool.assert_not_called()

    @pytest.mark.asyncio
    async def test_hook_deny_still_populates_refusal_recovery(self, tmp_path):
        """Complementary check: a system-side hook deny DOES trigger recovery,
        confirming the hook-deny path (L1974) is unaffected by the fix."""
        cb = _context_builder(
            ToolHookResult.deny("Blocked by security policy: rm -rf /")
        )
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()

        client.context_usage_pct = MagicMock(return_value=0.0)
        client._client = client
        client.last_prompt_stats = None

        calls = {"n": 0}

        def _stream(*a, **kw):
            calls["n"] += 1
            if calls["n"] == 1:
                return _async_iter([_permission_event(), _complete_event()])
            return _async_iter([_complete_event()])

        client.stream = MagicMock(side_effect=_stream)

        with _patch_stats():
            await _run_chat(state, slot, "hello")
            if slot.task:
                await slot.task

        # Recovery continuation IS injected for system-side deny.
        injects = [m for m in slot.messages if m.get("role") == "inject"]
        assert injects, "Hook deny must trigger refusal-recovery"
        recovery = injects[-1]["content"]
        assert recovery.startswith(REFUSAL_RECOVERY_PREFIX)
        assert "security policy" in recovery.lower()


class TestPreToolUseHookBlockRecovery:
    """A PreToolUse script-hook block feeds its reason into refusal recovery.

    Four permission paths can fire PreToolUse hooks and they are not
    interchangeable: the gating path latches ``_pre_tool_hooks_fired``, so the
    trust and interactive paths only fire hooks themselves when no context
    builder ran first. Each path therefore needs its own fixture.
    """

    @pytest.mark.asyncio
    async def test_declarative_auto_approve_block_enqueues_recovery(self, tmp_path):
        """A declarative auto-approve verdict still routes a block to recovery."""
        reason = "Read the whole SKILL.md - this was a truncated slice"
        state, client = _make_state(
            tmp_path,
            context_builder=_context_builder(ToolHookResult.auto_approve()),
            hook_store=_blocking_hook_store(reason, hook_name="skill-truncation"),
        )
        slot = _make_slot()

        await _drive_hook_blocked_turn(state, client, slot)

        _assert_block_reason_recovered(slot, client, reason)

    @pytest.mark.asyncio
    async def test_gated_path_block_enqueues_recovery(self, tmp_path):
        """A block raised while gating a normal tool call routes to recovery."""
        reason = "Gating hook refused this command"
        state, client = _make_state(
            tmp_path,
            context_builder=_context_builder(),
            hook_store=_blocking_hook_store(reason),
        )
        slot = _make_slot()

        await _drive_hook_blocked_turn(state, client, slot)

        _assert_block_reason_recovered(slot, client, reason)

    @pytest.mark.asyncio
    async def test_trusted_path_block_enqueues_recovery(self, tmp_path):
        """Trust mode fires the hook itself and must route its block to recovery."""
        reason = "Trusted calls still respect policy"
        state, client = _make_state(tmp_path, hook_store=_blocking_hook_store(reason))
        slot = _make_slot(trust=True)

        await _drive_hook_blocked_turn(state, client, slot)

        _assert_block_reason_recovered(slot, client, reason)

    @pytest.mark.asyncio
    async def test_interactive_approved_block_enqueues_recovery(self, tmp_path):
        """A block landing after the user approves must route to recovery."""
        reason = "Approved by the user but refused by policy"
        state, client = _make_state(tmp_path, hook_store=_blocking_hook_store(reason))
        slot = _make_slot()

        await _drive_hook_blocked_turn(state, client, slot, approve_prompt=True)

        _assert_block_reason_recovered(slot, client, reason)

    def test_every_hook_deny_path_routes_through_the_shared_helper(self) -> None:
        """A fifth permission path must not be able to deny without recording the reason.

        The four cases above each cover one existing path behaviourally. This one is
        structural, and it is why the helper exists: rejecting, showing the blocked row
        and auditing WITHOUT appending the hook's reason is precisely the defect this
        change fixes -- the model stalls with no idea what it did wrong, while every
        other assertion still passes. One helper makes that omission unrepresentable.
        """
        source = Path(chat_runner.__file__).read_text(encoding="utf-8")
        deny_branches = source.count("if _pre_tool_hooks_should_block(pre_hook_results):")
        helper_calls = source.count("await _reject_hook_blocked(")

        assert deny_branches >= 4, "expected at least the four known PreToolUse deny paths"
        assert helper_calls == deny_branches, (
            f"{deny_branches} hook-deny branch(es) but {helper_calls} helper call(s) -- "
            "a deny path that inlines reject/row/audit can silently drop the reason"
        )
        # The audit lives in the helper and nowhere else, so an inlined deny path
        # cannot reappear without this failing.
        assert source.count('outcome="hook_blocked"') == 1, (
            "hook_blocked is audited in more than one place -- a deny path was inlined "
            "again instead of routed through the helper"
        )

    @pytest.mark.asyncio
    async def test_blocked_row_and_audit_redact_the_model_authored_title(
        self, tmp_path
    ) -> None:
        """A credential the model put in the tool title must not reach either surface.

        ``event.title`` prefers the model's own ``description`` field
        (``_select_tool_title``), so it is LLM-controlled display text. The sibling
        reject path redacts it before both the transcript row and the audit
        (``_safe_reject_title``); this path published it verbatim, and the row is both
        broadcast to the dashboard and persisted to the ConversationLog.
        """
        # Assembled at runtime, never written as one literal: the redactor only
        # fires on credential-SHAPED input (a plain sentinel passes through
        # untouched, so this test would prove nothing), but a real key shape
        # sitting in the source trips the source-text scanners --
        # `scripts/scrub-lint.sh` and Semgrep's
        # `detected-aws-access-key-id-value`. Splitting satisfies both, and
        # matches the existing sentinels in code_review_sage's tests.
        secret = "AKIA" + "1234567890ABCDEF"
        state, client = _make_state(
            tmp_path,
            context_builder=_context_builder(),
            hook_store=_blocking_hook_store("Gating hook refused this command"),
        )
        slot = _make_slot()

        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_hook_blocked_turn(
                state, client, slot, title=f"Deploy with {secret} now"
            )

        rows = [m.get("content", "") for m in slot.messages]
        assert not any(secret in row for row in rows), rows
        assert any("[REDACTED: credential]" in row and "hook blocked" in row for row in rows), rows

        blocked = [
            call.kwargs
            for call in audit.log_tool_invocation.call_args_list
            if call.kwargs.get("outcome") == "hook_blocked"
        ]
        assert blocked, "expected a hook_blocked audit record"
        assert all(secret not in c.get("tool_name", "") for c in blocked), blocked
