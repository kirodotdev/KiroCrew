"""Tests for dashboard tool approval flow — normal/trust/yolo modes."""

from __future__ import annotations

import ast
import asyncio
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.handlers.sessions import api_approval_resolve
from kiro_crew.dashboard.state import (
    REFUSAL_RECOVERY_PREFIX,
    DashboardState,
    _ChatSlot,
    build_refusal_recovery_prompt,
    parse_cls_meta,
)
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHookResult, ToolHookResult
from kiro_crew.providers.base import (
    EVENT_COMPLETE,
    EVENT_PERMISSION_REQUEST,
    EVENT_TEXT_CHUNK,
    EVENT_THINKING_CHUNK,
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
            # Real dataclass instance so a renamed production field fails
            # loudly as a TypeError instead of silently configuring a stale
            # attribute on a bare MagicMock.
            ScriptHookResult(
                hook_id="h-" + hook_name,
                hook_name=hook_name,
                event=HOOK_EVENT_PRE_TOOL_USE,
                exit_code=2,
                stderr=reason,
                stdout="",
            )
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
            await _answer_approval(slot, "req-1", "approved")

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


# How long an answerer waits for the turn to register its approval future. The
# product parks on that future for the MINIMUM of `approval_timeout_for()` and
# `tool_approval_timeout_secs()` — 600s by default — which is LONGER than CI's
# `--timeout=180`, so a poke that misses does not fail the test: pytest-timeout
# hard-kills the xdist worker and the run reports `worker 'gwN' crashed`,
# naming an arbitrary in-flight test and discarding the real cause. Waiting for
# the future to EXIST instead of sleeping a fixed interval removes that race;
# the deadline exists only so a future that never appears surfaces as the named
# assertion below rather than as a killed worker.
_ANSWER_WAIT_SECS = 30.0
_ANSWER_POLL_SECS = 0.01


async def _answer_approval(
    owner: object, request_id: str, outcome: str, *, timeout: float = _ANSWER_WAIT_SECS
) -> None:
    """Resolve *owner*'s approval future for *request_id* once the turn registers it.

    Stands in for a user clicking Approve/Reject. The future is created inside
    ``_run_chat`` when the permission event is processed, so an answerer that
    pokes at a fixed offset races the stream: on a loaded runner the event can
    arrive after the poke, leaving the turn parked on an approval nobody will
    ever answer.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        fut = owner._approval_futures.get(request_id)  # type: ignore[attr-defined]
        if fut is not None:
            if not fut.done():
                fut.set_result(outcome)
            return
        assert loop.time() < deadline, (
            f"approval future {request_id!r} was never registered within "
            f"{timeout:.0f}s — the turn never reached its permission event, so "
            f"there was nothing to answer"
        )
        await asyncio.sleep(_ANSWER_POLL_SECS)


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
    async def test_hook_deny_never_registers_approval_future(self, tmp_path):
        """Non-bypass guard for the Tool-Approval Layer (Req 6.1-6.3).

        The single-authority boundary rests on the runner's BRANCH ORDER: a
        TOOL_DENY hits ``reject_tool`` + SEL audit and returns BEFORE the
        interactive path that stores ``slot._approval_futures[request_id]`` and
        renders an approvable card. If that order regressed so a denied call
        reached the approval-future registration, the frontend could surface —
        and a human/batch could resume — a call the gate denied.

        Checking ``request_id not in slot._approval_futures`` AFTER the turn is
        not enough: the runner's ``finally`` (``chat_runner.py`` ~7396) pops
        every future at end-of-turn, so the interactive path also leaves it
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
        assert any(
            "hook blocked" in m.get("content", "").lower() for m in msgs
        ), msgs

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
        assert any(
            "hook blocked" in m.get("content", "").lower() for m in msgs
        ), (label, msgs)

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

        client.approve_tool.assert_called_once()
        client.reject_tool.assert_not_called()

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

        async def _auto_reject() -> None:
            await _answer_approval(slot, "req-1", "rejected")

        rejecter = asyncio.get_event_loop().create_task(_auto_reject())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(rejecter)

        client.reject_tool.assert_called_once()

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

        async def _reject_first() -> None:
            await _answer_approval(slot, "req-1", "rejected")

        rejecter = asyncio.get_event_loop().create_task(_reject_first())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(rejecter)

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


class TestDenialCascadeLifetime:
    """The batch-rejection suppression must not outlive the group it belongs to.

    Issue #7681: ``_batch_rejected`` was only cleared in the turn runner's
    ``finally``, so it lived for the whole TURN. A user who denied one call had
    every later call in that turn auto-denied without ever being shown a card —
    breaking deny → discuss → agent revises → agent retries, and reporting the
    phantom denial to the model as "User denied tool execution".
    """

    @pytest.mark.parametrize("resume_kind", [EVENT_TEXT_CHUNK, EVENT_THINKING_CHUNK])
    @pytest.mark.asyncio
    async def test_later_sequential_call_is_evaluated_independently(
        self, tmp_path, resume_kind
    ):
        """Call N+1 gets its own prompt once the model resumed after N was denied."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        denied = _permission_event(title="tool_a")
        denied.request_id = "req-1"
        denied.tool_call_id = "tc-1"
        # Model-authored output between the two calls: the denied group is over
        # and req-2 is a NEW decision, not a remaining member of that group.
        resumed = LLMEvent(kind=resume_kind, text="Understood - trying a different edit.")
        revised = _permission_event(title="tool_b")
        revised.request_id = "req-2"
        revised.tool_call_id = "tc-2"
        _set_stream(client, [denied, resumed, revised, _complete_event()])

        async def _answer_both() -> None:
            await _answer_approval(slot, "req-1", "rejected")
            await _answer_approval(slot, "req-2", "approved")

        answerer = asyncio.get_event_loop().create_task(_answer_both())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(answerer)

        # The denial itself still denies.
        client.reject_tool.assert_any_call("req-1")
        # The revised call was PROMPTED and could be approved — never swallowed.
        client.approve_tool.assert_any_call("req-2")
        assert ("req-2",) not in [c.args for c in client.reject_tool.call_args_list]

    @pytest.mark.asyncio
    async def test_same_group_still_cascades_after_the_fix(self, tmp_path):
        """No model output between the calls → the remaining member stays denied.

        Pins the blast-radius reduction as a LIFETIME change only: suppression
        of the group the user actually refused is preserved.
        """
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        async def _reject_first() -> None:
            await _answer_approval(slot, "req-1", "rejected")

        rejecter = asyncio.get_event_loop().create_task(_reject_first())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(rejecter)

        client.reject_tool.assert_any_call("req-1")
        client.reject_tool.assert_any_call("req-2")
        client.approve_tool.assert_not_called()


class TestDenyOnce:
    """Verify 'rejected_once' denies a single tool without cascading."""

    @pytest.mark.asyncio
    async def test_deny_once_rejects_tool_but_does_not_cascade(self, tmp_path):
        """reject_once rejects the first tool but lets the second get its own prompt."""
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt1 = _permission_event(title="tool_a")
        evt1.request_id = "req-1"
        evt1.tool_call_id = "tc-1"
        evt2 = _permission_event(title="tool_b")
        evt2.request_id = "req-2"
        evt2.tool_call_id = "tc-2"
        _set_stream(client, [evt1, evt2, _complete_event()])

        async def _answer_both() -> None:
            await _answer_approval(slot, "req-1", "rejected_once")
            await _answer_approval(slot, "req-2", "approved")

        answerer = asyncio.get_event_loop().create_task(_answer_both())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
        await _drain(answerer)

        # First tool rejected, second approved
        client.reject_tool.assert_any_call("req-1")
        client.approve_tool.assert_any_call("req-2")
        # Batch rejection flag must NOT be set
        assert slot._batch_rejected is False

    @pytest.mark.asyncio
    async def test_decision_override_reaches_slot_future(self, tmp_path):
        """``rejected_once=True`` is what the slot future receives, not "rejected"."""
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-once"] = fut

        assert state.resolve_approval("req-once", False, rejected_once=True) is True
        assert fut.result() == "rejected_once"

    @pytest.mark.asyncio
    async def test_both_audit_event_types_agree_on_the_denial(self, tmp_path):
        """The `approval_decision` event records the token too, not just `rejected`.

        `log_tool_invocation` already distinguishes the two denials on the tool
        event. If this second event type flattened both to "rejected", the audit
        trail would distinguish them in one place and not the other, which
        distinguishes nothing.
        """
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        slot._approval_futures["req-audit"] = fut

        with patch("kiro_crew.dashboard.state.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            state.resolve_approval("req-audit", False, rejected_once=True)
            outcomes = [
                c.kwargs.get("outcome")
                for c in mock_sel.return_value.log_tool_invocation.call_args_list
                if c.kwargs.get("tool_name") == "approval_decision"
            ]
        assert outcomes == ["rejected_once"]

    @pytest.mark.asyncio
    async def test_state_level_override_drop_is_logged_not_silent(self, tmp_path):
        """A state-level future cannot carry the token, so the drop must be audible.

        State futures take priority by design (id-collision safety), and they
        hold a bool — so an override addressed at one is discarded. Harmless
        today (a background approval has no batch to cascade to), but silence is
        what makes the next decision token repeat the discovery.
        """
        state, _ = _make_state(tmp_path)
        slot = _make_slot()
        state._slots[slot.key] = slot

        loop = asyncio.get_running_loop()
        state_fut: asyncio.Future[bool] = loop.create_future()
        state._approval_futures["req-bg"] = state_fut

        with patch.object(state, "_log") as log:
            assert (
                state.resolve_approval("req-bg", False, rejected_once=True) is True
            )
        assert state_fut.result() is False
        assert log.warning.called
        assert "rejected_once" in repr(log.warning.call_args)

    @pytest.mark.asyncio
    async def test_api_reject_once_maps_to_decision_override(self):
        """The HTTP seam: ``reject_once`` is a valid action and sets the flag.

        Pinned at the handler, not in redux: a frontend-only test passes even when
        the action never reaches ``resolve_approval``, leaving Deny once wired to
        the plain batch reject in production.
        """
        state = MagicMock()
        state.resolve_approval.return_value = True

        async def _call(action: str):
            request = MagicMock()
            request.app = {"state": state}
            request.match_info = {"id": "req-9", "action": action}
            return await api_approval_resolve(request)

        assert (await _call("reject_once")).status == 200
        assert state.resolve_approval.call_args.args == ("req-9", False)
        assert state.resolve_approval.call_args.kwargs["rejected_once"] is True

        # Plain reject must keep the cascading behavior (no override).
        assert (await _call("reject")).status == 200
        assert state.resolve_approval.call_args.kwargs["rejected_once"] is False

        # Unknown actions are still refused.
        assert (await _call("reject_twice")).status == 400

    @pytest.mark.asyncio
    async def test_deny_once_is_distinguishable_in_the_audit_log(self, tmp_path):
        """SEL records ``rejected_once``.

        A hard-coded ``"rejected"`` would make a single-tool denial and a
        whole-batch denial identical in the audit trail, which is the one place
        the distinction has to survive.
        """
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        evt = _permission_event(title="tool_a")
        evt.request_id = "req-1"
        _set_stream(client, [evt, _complete_event()])

        answerer = asyncio.get_event_loop().create_task(
            _answer_approval(slot, "req-1", "rejected_once")
        )
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            with _patch_stats():
                await _run_chat(state, slot, "hello")
            await _drain(answerer)
            outcomes = [
                c.kwargs.get("outcome")
                for c in mock_sel.return_value.log_tool_invocation.call_args_list
                if c.kwargs.get("tool_name") == "tool_a"
            ]
        assert outcomes == ["rejected_once"]


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
    async def test_deny_after_an_answer_injects_awareness_not_a_redo(self, tmp_path):
        """A denied call followed by a real answer must not ask for a resume.

        The regression this pins: the turn answered the user's question despite
        the block, then the continuation told it to "continue the task where you
        left off" — so it answered the same question again, at full turn cost,
        once per blocked call.
        """
        cb = _context_builder(
            ToolHookResult.deny("Blocked by security policy: git push")
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
                # Blocked, then the model answers anyway from what it already had.
                return _async_iter(
                    [
                        _permission_event(),
                        LLMEvent(kind=EVENT_TEXT_CHUNK, text="Here is the answer."),
                        _complete_event(),
                    ]
                )
            return _async_iter([_complete_event()])

        client.stream = MagicMock(side_effect=_stream)

        with _patch_stats():
            await _run_chat(state, slot, "hello")
            if slot.task:
                await slot.task

        recovery = [
            m["content"]
            for m in slot.messages
            if m.get("role") == "inject"
            and m.get("content", "").startswith(REFUSAL_RECOVERY_PREFIX)
        ]
        assert recovery, "the block reason must still reach the model"
        body = recovery[-1]
        # Awareness is kept …
        assert "security policy: git push" in body.lower()
        assert "NOT a user action" in body
        # … the redo instruction is not.
        assert "continue the task where you left off" not in body
        assert "Do NOT repeat" in body

    @pytest.mark.asyncio
    async def test_answer_before_the_block_also_gets_awareness_not_a_redo(self, tmp_path):
        """The answer-THEN-block ordering must reach the same awareness body.

        The sibling above covers block-then-answer, where the answer is still in
        ``assistant_text`` at end of turn. Here the model answers FIRST and then
        calls the blocked tool, and the tool boundary flushes that answer out of
        ``assistant_text`` — so the end-of-turn buffer is empty even though the
        user has already read the answer on screen. Keying the body on that buffer
        alone sent the resume instruction for this ordering, which is the same
        duplicate answer at full turn cost (GPT round on #8275).
        """
        cb = _context_builder(
            ToolHookResult.deny("Blocked by security policy: git push")
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
                # The answer lands first; the blocked call ends the turn after it.
                return _async_iter(
                    [
                        LLMEvent(kind=EVENT_TEXT_CHUNK, text="Here is the answer."),
                        _permission_event(),
                        _complete_event(),
                    ]
                )
            return _async_iter([_complete_event()])

        client.stream = MagicMock(side_effect=_stream)

        with _patch_stats():
            await _run_chat(state, slot, "hello")
            if slot.task:
                await slot.task

        recovery = [
            m["content"]
            for m in slot.messages
            if m.get("role") == "inject"
            and m.get("content", "").startswith(REFUSAL_RECOVERY_PREFIX)
        ]
        assert recovery, "the block reason must still reach the model"
        body = recovery[-1]
        assert "security policy: git push" in body.lower()
        assert "NOT a user action" in body
        assert "continue the task where you left off" not in body
        assert "Do NOT repeat" in body

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

    def test_answered_turn_gets_awareness_body_not_a_continuation(self):
        """A turn that already answered must not be told to resume the task.

        Same reasons, same remediation — but "continue the task where you left
        off" is what made the model re-answer a question the user had already
        read, once per blocked call.
        """
        out = build_refusal_recovery_prompt(
            [("bash", "command accesses sensitive credential path")], answered=True
        )
        # The reason still reaches the model: on a backend without mid-turn steer
        # this turn is the only channel for it.
        assert "bash" in out
        assert "sensitive credential path" in out
        assert "NOT a user action" in out
        # …but the premise flips from "ended the turn early" to awareness-only.
        assert "ended the turn early" not in out
        assert "this note is for awareness" in out
        assert "continue the task where you left off" not in out
        assert "Do NOT repeat" in out

    def test_unanswered_turn_keeps_the_continuation_body(self):
        out = build_refusal_recovery_prompt([("bash", "reason")], answered=False)
        assert "ended the turn early" in out
        assert "continue the task where you left off" in out
        assert "this note is for awareness" not in out

    def test_answered_keeps_per_class_remediation(self):
        # The awareness variant drops the resume instruction, not the guidance:
        # "how to do this properly" is the awareness the user is owed.
        reason = "Blocked by security policy: cat ~/.aws/credentials"
        assert build_refusal_recovery_prompt(
            [("Running: cat creds", reason)], answered=True
        ).count("How to do this properly:") == build_refusal_recovery_prompt(
            [("Running: cat creds", reason)]
        ).count("How to do this properly:")


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

        state.sessions.reset.assert_any_await("dashboard:chat-1-test", skip_if_busy=True)
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

        state.sessions.reset.assert_any_await("dashboard:chat-1-test", skip_if_busy=True)
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
        async def _auto_reject() -> None:
            await _answer_approval(slot, "req-1", "rejected")

        rejecter = asyncio.get_running_loop().create_task(_auto_reject())

        with _patch_stats():
            await _run_chat(state, slot, "hello")
            if slot.task:
                await slot.task
        await _drain(rejecter)

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


# ── Deny-row title redaction (all permission paths) ──


def _raising_hook_store(message: str) -> MagicMock:
    """Hook store whose PreToolUse fire raises, driving the hook-error deny.

    Only PreToolUse raises: ``_fire`` re-raises hook errors for that event
    alone, and the other hook events fired during a turn must stay healthy so
    the turn actually reaches the permission path under test.
    """
    hs = _make_hook_store()

    async def _fire(event: str, *args, **kwargs):  # type: ignore[no-untyped-def]
        if event == HOOK_EVENT_PRE_TOOL_USE:
            raise RuntimeError(message)
        return []

    hs.fire = AsyncMock(side_effect=_fire)
    return hs


async def _drive_deny_turn(
    state,
    client,
    slot,
    *,
    title: str,
    tool_input: str = "",
    approve_prompt: bool = False,
) -> None:
    """Run one turn whose only tool call lands on the deny path under test.

    Only the first stream yields the permission request so any recovery
    continuation completes instead of denying again. ``approve_prompt``
    answers the interactive permission future, which is the only way to reach
    the validation/hook code that runs after the user approves.
    """
    client.context_usage_pct = MagicMock(return_value=0.0)
    client._client = client
    client.last_prompt_stats = None
    calls = {"n": 0}

    def _stream(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return _async_iter(
                [
                    LLMEvent(
                        kind=EVENT_PERMISSION_REQUEST,
                        title=title,
                        tool_kind="edit",
                        request_id="req-1",
                        tool_input=tool_input,
                    ),
                    _complete_event(),
                ]
            )
        return _async_iter([_complete_event()])

    client.stream = MagicMock(side_effect=_stream)

    approver = None
    if approve_prompt:

        async def _answer() -> None:
            await _answer_approval(slot, "req-1", "approved")

        approver = asyncio.get_event_loop().create_task(_answer())

    with _patch_stats():
        await _run_chat(state, slot, "hello")
        if slot.task:
            await slot.task

    if approver is not None:
        await _drain(approver)


def _assert_deny_surfaces_redacted(slot, audit, secret: str, *, row_suffix: str) -> None:
    """The secret must reach no transcript row and no audit ``tool_name``."""
    rows = [m.get("content", "") for m in slot.messages]
    assert not any(secret in row for row in rows), rows
    assert any("[REDACTED: credential]" in row and row_suffix in row for row in rows), rows
    for call in audit.log_tool_invocation.call_args_list:
        assert secret not in call.kwargs.get("tool_name", ""), call.kwargs


class TestDenyRowTitleRedaction:
    """Every deny surface must publish the model-authored title redacted.

    ``event.title`` prefers the model's own ``description`` field
    (``_select_tool_title``), so a credential the model plants there must
    never reach a transcript row (broadcast to the dashboard AND persisted to
    the ConversationLog) or a SEL audit ``tool_name``. Each permission path
    denies through its own control flow, so each gets a behavioral test; the
    structural test keeps a future path from inlining a raw interpolation.

    Two triggers per path where the path has both: an invalid tool name
    (length cap — the title is over ``MAX_TOOL_NAME_LEN`` with the credential
    embedded) and a hook-fire error (title passes validation, PreToolUse
    raises).
    """

    # Assembled at runtime, never as one literal: the redactor only fires on
    # credential-SHAPED input, and a real key shape in the source trips the
    # source-text scanners (`scripts/scrub-lint.sh`, Semgrep).
    _SECRET = "AKIA" + "1234567890ABCDEF"

    def _invalid_title(self) -> str:
        # Over MAX_TOOL_NAME_LEN with is_shell False, so _validate_tool_name
        # raises the length error while the credential sits in the title.
        return f"Deploy with {self._SECRET} " + "x" * 300

    def _valid_title(self) -> str:
        return f"Deploy with {self._SECRET} now"

    def _assert_audit_redacted(self, audit, outcome: str) -> None:
        recs = [
            call.kwargs
            for call in audit.log_tool_invocation.call_args_list
            if call.kwargs.get("outcome") == outcome
        ]
        assert recs, f"expected a {outcome} audit record"
        assert all("[REDACTED: credential]" in c.get("tool_name", "") for c in recs), recs

    @pytest.mark.asyncio
    async def test_auto_approve_invalid_name_redacts(self, tmp_path):
        state, client = _make_state(
            tmp_path, context_builder=_context_builder(ToolHookResult.auto_approve())
        )
        slot = _make_slot()
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(state, client, slot, title=self._invalid_title())
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(invalid:")
        self._assert_audit_redacted(audit, "denied")

    @pytest.mark.asyncio
    async def test_auto_approve_hook_error_redacts(self, tmp_path):
        state, client = _make_state(
            tmp_path,
            context_builder=_context_builder(ToolHookResult.auto_approve()),
            hook_store=_raising_hook_store("hook exploded"),
        )
        slot = _make_slot()
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(state, client, slot, title=self._valid_title())
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(hook error)")
        self._assert_audit_redacted(audit, "hook_error")

    @pytest.mark.asyncio
    async def test_gated_path_invalid_name_redacts(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(state, client, slot, title=self._invalid_title())
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(invalid:")
        self._assert_audit_redacted(audit, "denied")

    @pytest.mark.asyncio
    async def test_gated_path_hook_error_redacts(self, tmp_path):
        state, client = _make_state(
            tmp_path,
            context_builder=_context_builder(),
            hook_store=_raising_hook_store("hook exploded"),
        )
        slot = _make_slot()
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(state, client, slot, title=self._valid_title())
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(hook error)")
        self._assert_audit_redacted(audit, "hook_error")

    @pytest.mark.asyncio
    async def test_trust_reads_invalid_name_redacts(self, tmp_path):
        """Trust-reads denies on a redacted row with an audited SEL record."""
        state, client = _make_state(tmp_path)
        slot = _make_slot()
        slot._trust_reads = True
        # The name-grant check is stubbed to "no refusal" so this test keeps
        # measuring what its name claims -- that the trust-reads DENY row and its
        # SEL record are redacted -- rather than the host's PATH semantics. It
        # reaches the tier with `ls`, which resolves to a trusted system program on
        # POSIX but does not exist on Windows, where the check therefore declines
        # the tier outright, the request falls through to the interactive card, and
        # the `trust_reads` deny asserted below never happens. Patching the ONE
        # off-loop entry point every tier goes through is the same seam
        # `test_chat_runner_coverage.py` uses; the check itself is covered directly
        # in `test/test_name_grant.py`.
        _no_refusal = patch.object(
            chat_runner, "_name_grant_refusal_off_loop", new=AsyncMock(return_value=None)
        )
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel, _no_refusal:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(
                state,
                client,
                slot,
                title=self._invalid_title(),
                tool_input='{"command": "ls"}',
            )
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(invalid:")
        self._assert_audit_redacted(audit, "denied")
        denied = [
            call.kwargs
            for call in audit.log_tool_invocation.call_args_list
            if call.kwargs.get("outcome") == "denied"
        ]
        assert any(
            (c.get("metadata") or {}).get("reason") == "trust_reads" for c in denied
        ), denied

    @pytest.mark.asyncio
    async def test_trust_mode_invalid_name_redacts(self, tmp_path):
        state, client = _make_state(tmp_path)
        slot = _make_slot(trust=True)
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(state, client, slot, title=self._invalid_title())
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(invalid:")
        self._assert_audit_redacted(audit, "denied")

    @pytest.mark.asyncio
    async def test_trust_mode_hook_error_redacts(self, tmp_path):
        state, client = _make_state(tmp_path, hook_store=_raising_hook_store("hook exploded"))
        slot = _make_slot(trust=True)
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(state, client, slot, title=self._valid_title())
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(hook error)")
        self._assert_audit_redacted(audit, "hook_error")

    @pytest.mark.asyncio
    async def test_interactive_approved_invalid_name_redacts(self, tmp_path):
        state, client = _make_state(tmp_path)
        slot = _make_slot()
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(
                state, client, slot, title=self._invalid_title(), approve_prompt=True
            )
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(invalid:")
        self._assert_audit_redacted(audit, "denied")

    @pytest.mark.asyncio
    async def test_interactive_approved_hook_error_redacts(self, tmp_path):
        state, client = _make_state(tmp_path, hook_store=_raising_hook_store("hook exploded"))
        slot = _make_slot()
        with patch("kiro_crew.dashboard.chat_runner.sel") as mock_sel:
            audit = MagicMock()
            mock_sel.return_value = audit
            await _drive_deny_turn(
                state, client, slot, title=self._valid_title(), approve_prompt=True
            )
        _assert_deny_surfaces_redacted(slot, audit, self._SECRET, row_suffix="(hook error)")
        self._assert_audit_redacted(audit, "hook_error")

    def test_no_deny_surface_interpolates_the_raw_title(self) -> None:
        """No module code may interpolate the raw title into a row or audit.

        The behavioral cases above each cover one existing path. This one is
        structural, and it is why the shared helpers exist: a permission path
        added later that inlines ``reject`` + row + audit would publish the
        raw model-authored title again while every behavioral assertion still
        passes. Rendering each deny shape in exactly one place (its helper)
        makes that omission unrepresentable.
        """
        source = Path(chat_runner.__file__).read_text(encoding="utf-8")
        assert source.count("🚫 {event.title}") == 0, (
            "a transcript row interpolates the raw model-authored title -- "
            "route the deny through _reject_invalid_tool/_reject_hook_error"
        )
        assert source.count("tool_name=event.title") == 0, (
            "a SEL audit passes the raw model-authored title -- redact it "
            "(reuse an in-scope redacted variable or _redact_display_text)"
        )
        # Each deny shape is rendered in exactly one place — its helper.
        assert source.count('f"🚫 {title} (invalid: {error})"') == 1
        assert source.count('f"🚫 {title} (hook error)"') == 1


class TestApprovalAnswerersDoNotRaceTheStream:
    """No answerer may wait on the clock instead of on the approval future.

    An answerer that sleeps a fixed interval and then reads
    ``_approval_futures`` once is racing the provider stream: when the
    permission event arrives after the poke, the future is registered with
    nobody left to answer it, and the turn parks on
    ``asyncio.wait_for(fut, timeout=...)`` for the whole approval window.

    That window (600s by default) outlives CI's ``--timeout=180``, so the test
    does not fail — pytest-timeout hard-kills the xdist worker and the run
    reports ``worker 'gwN' crashed while running <whatever was in flight>``,
    naming an arbitrary test and discarding the real cause.

    These are source ratchets, not behavioral tests, because the racy shape
    passes every assertion on an idle machine — only load reveals it, and only
    as somebody else's crashed worker.
    """

    @staticmethod
    def _functions():
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                yield node

    @staticmethod
    def _calls(fn, *, attr, on=None):
        """Calls of ``<...>.attr(...)`` inside *fn*, optionally on ``<...>.on``."""
        out = []
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if not isinstance(f, ast.Attribute) or f.attr != attr:
                continue
            if on is not None:
                base = f.value
                if not (isinstance(base, ast.Attribute) and base.attr == on):
                    continue
            out.append(node)
        return out

    def test_only_the_shared_helper_waits_for_an_approval_future(self):
        """A function touching _approval_futures must not also sleep."""
        offenders = []
        for fn in self._functions():
            if fn.name == "_answer_approval":
                continue  # the one place allowed to wait, and it polls
            touches = any(
                isinstance(n, ast.Attribute) and n.attr == "_approval_futures"
                for n in ast.walk(fn)
            )
            if not touches:
                continue
            if self._calls(fn, attr="sleep"):
                offenders.append(f"{fn.name} (line {fn.lineno})")
        assert not offenders, (
            "these functions wait on the clock before touching an approval "
            "future, which races the stream and surfaces as a crashed xdist "
            "worker -- await _answer_approval() instead: " + ", ".join(offenders)
        )

    def test_answerers_do_not_hand_roll_the_future_lookup(self):
        """Only _answer_approval may .get() an approval future to answer it."""
        offenders = [
            f"{fn.name} (line {fn.lineno})"
            for fn in self._functions()
            if fn.name != "_answer_approval"
            and self._calls(fn, attr="get", on="_approval_futures")
        ]
        assert not offenders, (
            "these functions look up an approval future themselves instead of "
            "reusing _answer_approval, so the wait discipline has to be "
            "re-derived at each site: " + ", ".join(offenders)
        )
