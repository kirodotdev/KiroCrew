"""How a tool permission request is routed: normal, trust and yolo modes, and hook verdicts.

Normal mode prompts on the dashboard and trust and yolo auto-approve; a host-gate deny
rejects without a prompt or an approval future, and a scripted PreToolUse hook still judges
an auto-approved call. These cases also pin trust and yolo reaching the session's approval
policy, ``resolve_approval`` falling through to slot-level futures, and the tool-call id
redacted before the event loop uses it. The state, stream and answerer helpers come from
``dashboard_approval_helpers.py``.
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dashboard_approval_helpers import (
    _answer_approval,
    _complete_event,
    _context_builder,
    _drain,
    _make_hook_store,
    _make_slot,
    _make_state,
    _patch_stats,
    _permission_event,
    _set_stream,
)

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.state import _ChatSlot, parse_cls_meta, row_mid
from kiro_crew.hooks import (
    HOOK_EVENT_PRE_TOOL_USE,
    TOOL_AUTO_APPROVE,
    ScriptHookResult,
    ToolHookResult,
)
from kiro_crew.name_grant import SHADOWED, Refusal
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, LLMEvent
from kiro_crew.testing.wait import await_until, finished


def _tool_messages(slot: _ChatSlot) -> list[dict]:
    return [m for m in slot.messages if m.get("role") in ("tool", "permission")]


# ── Tests ──


class TestApprovalModes:
    """Verify that normal/trust/yolo modes route permission requests correctly."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("outcome", ["approved", "rejected_once"])
    async def test_old_completion_preserves_replacement_request(self, tmp_path, outcome):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])
        replacement = asyncio.get_running_loop().create_future()
        observed = {}

        def replace():
            old_row = next(m for m in slot.messages if m.get("role") == "permission")
            assert slot.approval_instance("req-1") == row_mid(old_row)
            row = slot.append("permission", "Replacement", json.dumps({"request_id": "req-1"}))
            slot.register_approval("req-1", replacement, row)
            observed["row"] = row
            state.broadcast_ws.reset_mock()

        answer = asyncio.create_task(
            _answer_approval(slot, "req-1", outcome, timeout=5, before_answer=replace)
        )
        try:
            with _patch_stats():
                await finished(_run_chat(state, slot, "hello"), "the chat turn completing")
            await await_until(answer, what="the approval answer landing")
            assert slot._approval_futures["req-1"] is replacement
            assert slot.approval_instance("req-1") == row_mid(observed["row"])
            assert "resolved" not in parse_cls_meta(observed["row"]["cls"])
            assert not replacement.done()
            assert not any(
                call.args[0] == "approval_resolved" for call in state.broadcast_ws.call_args_list
            )
        finally:
            await _drain(answer)
            replacement.cancel()
            slot.unregister_approval("req-1", replacement)

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "purpose", ["Inspect the isolated workspace", "", "Inspect token ghp_" + "a" * 36]
    )
    async def test_pending_permission_persists_its_own_redacted_purpose(self, tmp_path, purpose):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        slot.messages.append(
            {
                "role": "tool",
                "content": "Previous tool",
                "cls": json.dumps({"tool_purpose": "Unrelated purpose"}),
            }
        )
        event = _permission_event()
        event.tool_purpose = purpose
        _set_stream(client, [event, _complete_event()])
        observed = {}

        async def _inspect_and_approve():
            await _answer_approval(
                slot,
                "req-1",
                "approved",
                before_answer=lambda: observed.update(slot.to_dict()["pending_approval_info"]),
            )

        approver = asyncio.create_task(_inspect_and_approve())
        try:
            with _patch_stats():
                await _run_chat(state, slot, "hello")
            # flake-ok: pre-existing await of the answerer task -- its own wait is deadline-bounded -- moved verbatim; tracked for a de-flake pass
            await approver
        finally:
            await _drain(approver)
        permission = next(m for m in slot.messages if m.get("role") == "permission")
        metadata = parse_cls_meta(permission["cls"])
        assert observed["request_id"] == "req-1"
        if purpose:
            expected = chat_runner._redact_tool_field(purpose, limit=chat_runner._MAX_TOOL_PURPOSE)
            assert metadata["tool_purpose"] == expected
            assert observed["tool_purpose"] == expected
            if "ghp_" in purpose:
                assert "ghp_" not in expected
        else:
            assert not metadata.get("tool_purpose")
            assert not observed.get("tool_purpose")
        assert observed.get("tool_purpose") != "Unrelated purpose"

    @pytest.mark.asyncio
    async def test_normal_mode_prompts_interactively(self, tmp_path):
        """Normal mode (no trust, no yolo) must send a permission message."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        # Answer via the shared helper, which waits for the future to EXIST
        # rather than sleeping a fixed interval and hoping the chat registered
        # it by then, and keep a handle so the answerer is cancelled instead of
        # being GC'd mid-flight during a later test.
        async def _auto_approve() -> None:
            await _answer_approval(slot, "req-1", "approved")

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
        cb = _context_builder(ToolHookResult.deny("Blocked by security policy: git push"))
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        msgs = _tool_messages(slot)
        assert any("security policy: git push" in m.get("content", "").lower() for m in msgs), [
            m.get("content") for m in msgs
        ]

    @pytest.mark.asyncio
    async def test_hook_deny_broadcasts_activity_event(self, tmp_path):
        """A host-gate deny must broadcast a visible activity_event (mirroring
        the auto-approve branch) so the block is not silent."""
        cb = _context_builder(ToolHookResult.deny("Blocked by security policy: git push"))
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        perm_activity = [
            c.args
            for c in state.broadcast_ws.call_args_list
            if c.args
            and c.args[0] == "activity_event"
            and isinstance(c.args[1], dict)
            and c.args[1].get("kind") == "permission"
        ]
        assert perm_activity, state.broadcast_ws.call_args_list
        # The broadcast text should mention the block.
        assert any("block" in a[1].get("text", "").lower() for a in perm_activity), perm_activity

    @pytest.mark.asyncio
    async def test_hook_deny_never_registers_approval_future(self, tmp_path):
        """Non-bypass guard for the Tool-Approval Layer (Req 6.1-6.3).

        The single-authority boundary rests on the runner's BRANCH ORDER: a
        TOOL_DENY hits ``reject_tool`` + SEL audit and returns BEFORE the
        interactive path that stores ``slot._approval_futures[request_id]`` and
        renders an approvable card. If that order regressed so a denied call
        reached the approval-future registration, the frontend could surface —
        and a human/batch could resume — a call the gate denied.

        Checking ``request_id not in slot._approval_futures`` AFTER the turn is
        not enough: ``_on_permission_prompt``'s ``finally`` (chat_turn/permission_prompt.py) pops
        the future it registered, so the interactive path also leaves it
        absent — the residue is identical for allow and deny. So this test
        instead OBSERVES THE REGISTRATION ITSELF by recording every key ever
        assigned to ``_approval_futures`` during the turn. On a deny that key
        must never appear; if the branch order regressed to register it, the
        recorded-keys assertion fails fast and by name (not via a 120s parked-
        future timeout).
        """
        request_id = "req-deny-guard"
        cb = _context_builder(ToolHookResult.deny("blocked by policy"))
        state, client = _make_state(tmp_path, context_builder=cb)
        slot = _make_slot()

        # Record every request id the runner ever REGISTERS as an approval
        # future — observed at assignment time, so a deny that (wrongly) reached
        # the interactive registration is caught even though the end-of-turn
        # finally would later pop it.
        registered_ids: list[str] = []

        class _RecordingFutures(dict):  # type: ignore[type-arg]
            def __setitem__(self, key, value):  # type: ignore[no-untyped-def]
                registered_ids.append(key)
                super().__setitem__(key, value)

        slot._approval_futures = _RecordingFutures()  # type: ignore[assignment]

        deny_event = LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title="fs_write",
            tool_kind="edit",
            request_id=request_id,
        )
        _set_stream(client, [deny_event, _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        # The bypass this guards: a denied call must NEVER be registered as a
        # pending approval. If the runner registered a future for it, an
        # operator (or a batch resume) could execute what the gate denied.
        assert request_id not in registered_ids, (
            "TOOL_DENY registered an approval future — a denied call became "
            "approvable, defeating the single-authority boundary (Req 6.1). "
            f"registered ids: {registered_ids!r}"
        )
        # And the deny took the terminal reject path, not the interactive one.
        client.reject_tool.assert_called_once()
        client.approve_tool.assert_not_called()

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
        assert any("hook blocked" in m.get("content", "").lower() for m in msgs), msgs

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "label,result_kwargs",
        [
            # asyncio.TimeoutError branch of run_script_hook: no exit_code is set,
            # so the dataclass default (-1) stands and `error` carries the reason.
            (
                "timeout",
                {"exit_code": -1, "error": "Timed out after 3s", "stderr": "", "stdout": ""},
            ),
            # Generic exception branch: same shape, exception text on `error`.
            (
                "crash",
                {"exit_code": -1, "error": "boom", "stderr": "", "stdout": ""},
            ),
            # /bin/sh could not exec the hook command at all.
            (
                "missing binary",
                {"exit_code": 127, "error": "", "stderr": "", "stdout": ""},
            ),
        ],
    )
    async def test_auto_approve_blocked_when_pretooluse_hook_delivers_no_verdict(
        self, tmp_path, label, result_kwargs
    ):
        """A PreToolUse hook that cannot render a verdict must fail CLOSED.

        Timeout, crash, and missing-binary all leave an exit code that is neither
        0 nor 2. Treating those as "no opinion" means slowing, breaking, or
        deleting a deny hook silently disables the policy it enforces, so the
        auto-approve path must reject the tool instead of proceeding.
        """
        cb = _context_builder(ToolHookResult.auto_approve())
        hook_store = _make_hook_store()
        hook_store.fire = AsyncMock(
            # A real ScriptHookResult (not a bare MagicMock) pins the test to
            # the production dataclass: a renamed field fails loudly here as a
            # TypeError instead of silently configuring a stale attribute.
            return_value=[
                ScriptHookResult(
                    hook_id="h-policy-gate",
                    hook_name="policy-gate",
                    event=HOOK_EVENT_PRE_TOOL_USE,
                    **result_kwargs,
                )
            ]
        )
        state, client = _make_state(tmp_path, context_builder=cb, hook_store=hook_store)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        client.reject_tool.assert_called_once()
        client.approve_tool.assert_not_called()
        msgs = _tool_messages(slot)
        assert any("hook blocked" in m.get("content", "").lower() for m in msgs), (label, msgs)

    @pytest.mark.asyncio
    async def test_auto_approve_allowed_when_pretooluse_hook_exits_zero(self, tmp_path):
        """Exit 0 is a delivered "allow" verdict and must still approve.

        Pins the fail-closed branch to non-0/2 exits only, so a healthy hook that
        approves silently is not caught by it.
        """
        cb = _context_builder(ToolHookResult.auto_approve())
        hook_store = _make_hook_store()
        hook_store.fire = AsyncMock(
            return_value=[
                # Real dataclass instance (see the no-verdict test above for why).
                ScriptHookResult(
                    hook_id="h-policy-gate",
                    hook_name="policy-gate",
                    event=HOOK_EVENT_PRE_TOOL_USE,
                    exit_code=0,
                    stdout="",
                    stderr="",
                    error="",
                )
            ]
        )
        state, client = _make_state(tmp_path, context_builder=cb, hook_store=hook_store)
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        client.approve_tool.assert_called_once_with("req-1")
        client.reject_tool.assert_not_called()
        assert not any("hook blocked" in m.get("content", "").lower() for m in _tool_messages(slot))

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
        client.reject_tool.assert_called_once_with("req-1")
        assert [m["content"] for m in _tool_messages(slot)] == ["🚫 fs_write (hook error)"]

    @pytest.mark.asyncio
    @pytest.mark.xdist_group(name="serial")
    async def test_interactive_reject(self, tmp_path):
        """User rejecting interactively must call reject_tool."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        async def _auto_reject() -> None:
            await _answer_approval(slot, "req-1", "rejected")

        rejecter = asyncio.get_event_loop().create_task(_auto_reject())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(rejecter)

        client.reject_tool.assert_called_once_with("req-1")
        client.approve_tool.assert_not_called()
        msgs = _tool_messages(slot)
        assert [json.loads(m["cls"])["resolved"] for m in msgs if m["role"] == "permission"] == [
            "rejected"
        ]
        assert "🚫 fs_write (rejected)" in [m["content"] for m in msgs]

    @pytest.mark.asyncio
    async def test_interactive_approve_with_empty_hooks(self, tmp_path):
        """After interactive approve, empty hook results must NOT reject."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        _set_stream(client, [_permission_event(), _complete_event()])

        async def _auto_approve() -> None:
            await _answer_approval(slot, "req-1", "approved")

        approver = asyncio.get_event_loop().create_task(_auto_approve())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(approver)

        msgs = _tool_messages(slot)
        assert not any(
            "no hooks" in m.get("content", "") for m in msgs
        ), f"Empty hook results should not reject: {msgs}"
        client.approve_tool.assert_called_once()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "mode, expect_note", [("normal", True), ("trust", False), ("yolo", False)]
    )
    async def test_shadow_note_shown_only_when_a_card_asks(self, tmp_path, mode, expect_note):
        """A refused hook grant's "Approve this command explicitly" note needs a card.

        Under session trust or YOLO the unconditional rung approves with no card,
        so the note would ask for an approval nobody is asked for. Normal mode
        reaches the card and still shows it, ahead of the card.
        """
        cb = _context_builder(ToolHookResult(action=TOOL_AUTO_APPROVE))
        state, client = _make_state(tmp_path, context_builder=cb)
        if mode == "yolo":
            state.enable_yolo()
        slot = _make_slot(trust=(mode == "trust"))
        _set_stream(client, [_permission_event(), _complete_event()])

        # Only normal mode parks on an approval future; an answerer started for
        # trust/YOLO would wait on a future that never registers.
        approver = None
        if mode == "normal":

            async def _approve() -> None:
                await _answer_approval(slot, "req-1", "approved")

            approver = asyncio.get_running_loop().create_task(_approve())

        shadow = Refusal(code=SHADOWED, detail="python3 resolves to /shims/python3")
        refusal_check = AsyncMock(return_value=shadow)
        try:
            with (
                _patch_stats(),
                patch.object(chat_runner, "_name_grant_refusal_for", refusal_check),
            ):
                await _run_chat(state, slot, "hello")
        finally:
            if approver is not None:
                await _drain(approver)

        # The hook rung really declined; otherwise a missing note proves nothing.
        refusal_check.assert_awaited_once()
        shown = [
            m.get("role")
            for m in slot.messages
            if m.get("role") == "permission" or "Auto-approve not applied" in m.get("content", "")
        ]
        assert shown == (["system", "permission"] if expect_note else []), slot.messages
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
            "approval_resolved",
            # ``slot`` keys the frame for the slot-scoped WS gate — it must name
            # the slot that actually owned the resolved future.
            {"id": "req-42", "approved": True, "slot": "chat-1-test"},
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
        # A decided rejection broadcasts bare; only an expired wait names its
        # decision in the frame.
        state.broadcast_ws.assert_called_with(
            "approval_resolved",
            {"id": "req-43", "approved": False, "slot": "chat-1-test"},
        )

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
                "slot": slot.key,
                "tool": evt.title,
                "kind": evt.tool_kind,
                "auto": True,
                "tool_call_id": "tcid-clean",
                "purpose": "test purpose",
                "input_preview": "",
            },
        )
