"""Tests for dashboard tool approval flow — normal/trust/yolo modes.

Mode routing lives in ``test_dashboard_approval_modes.py``, denials and expiry in
``test_dashboard_approval_denials.py``, shared helpers in ``dashboard_approval_helpers.py``.
The answerer ratchet here reads all of those modules.
"""

from __future__ import annotations

import ast
import asyncio
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from dashboard_approval_helpers import (
    _answer_approval,
    _async_iter,
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
from named_cases_helpers import named_cases

from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.state import (
    REFUSAL_RECOVERY_PREFIX,
    DashboardState,
    _ChatSlot,
    build_refusal_recovery_prompt,
    row_mid,
)
from kiro_crew.dashboard.turn_dispatch import format_approval_no_budget_card
from kiro_crew.hooks import HOOK_EVENT_PRE_TOOL_USE, ScriptHookResult, ToolHookResult
from kiro_crew.providers.base import EVENT_PERMISSION_REQUEST, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.validation import MAX_TOOL_NAME_LEN

#: The modules whose functions ``TestApprovalAnswerersDoNotRaceTheStream`` reads: this one,
#: the ``_modes`` and ``_denials`` parts, and the helpers module that defines
#: ``_answer_approval``.
_ANSWERER_MODULES = (
    "test_dashboard_approval.py",
    "test_dashboard_approval_modes.py",
    "test_dashboard_approval_denials.py",
    "dashboard_approval_helpers.py",
)


def _answerer_family_tree() -> ast.Module:
    """One module holding every answerer module's statements, so one walk reads them all."""
    return ast.Module(
        body=[
            stmt
            for name in _ANSWERER_MODULES
            for stmt in ast.parse(Path(__file__).with_name(name).read_text(encoding="utf-8")).body
        ],
        type_ignores=[],
    )


def test_leaf_test_scope_derives_this_file_as_a_corpus_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The answerer ratchet reads the parts as data, so a test-only diff to one must run it.

    ``scripts/leaf_test_scope.py`` runs every corpus gate on such a diff, and this module is
    one because it scans directories it reaches through ``__file__``.
    """
    root = Path(__file__).resolve().parent.parent
    monkeypatch.syspath_prepend(str(root / "scripts"))
    leaf_test_scope = importlib.import_module("leaf_test_scope")
    this_file = Path(__file__).resolve().relative_to(root).as_posix()
    assert this_file in leaf_test_scope.corpus_gates(root)


# ── Helpers ──


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


class TestRefusalRecovery:
    """A recoverable refusal (host-gate policy deny / read-only bash gate) ends
    the turn via kiro-cli's tool-interrupted marker. KiroCrew should hand the
    reason back to the model as an auto-continuation so the agent can adapt
    instead of stalling — without the user having to poke it."""

    @pytest.mark.asyncio
    async def test_host_gate_deny_enqueues_recovery_continuation(self, tmp_path):
        """A host-gate deny records the reason and the finally-block dequeue
        re-dispatches it as an 'inject' continuation carrying that reason."""
        cb = _context_builder(ToolHookResult.deny("Blocked by security policy: git push"))
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
        cb = _context_builder(ToolHookResult.deny("Blocked by security policy: git push"))
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

        The tool boundary flushes the answer out of ``assistant_text``, so the end-of-turn
        buffer is empty although the user has read it. Keying on that buffer means a redo.
        """
        cb = _context_builder(ToolHookResult.deny("Blocked by security policy: git push"))
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

        assert not any(REFUSAL_RECOVERY_PREFIX in m.get("content", "") for m in slot.messages)
        assert not slot._queue


class TestBuildRefusalRecoveryPrompt:
    """build_refusal_recovery_prompt hands a tool-refusal reason back to the
    model so it can adapt, instead of the turn stalling silently."""

    @pytest.mark.parametrize(
        (),
        [
            *named_cases("test_empty_returns_empty"),
            *named_cases("test_empty_refusals_still_yield_nothing"),
        ],
    )
    def test_no_refusals_yield_an_empty_prompt(self):
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

    def test_answered_finished_task_leads_with_the_outcome_not_the_block(self):
        """The continuation's reply becomes the user's last message, so a finished
        task opens with what was done; a reply that only notes the block buries the
        summary the user needs."""
        out = build_refusal_recovery_prompt([("bash", "reason")], answered=True)
        finished = out[out.index("Only if the task IS finished") :]
        assert "reply with one short line noting the block" not in out
        assert finished.index("outcome of the work") < finished.index("noting the block")

    def test_unanswered_turn_keeps_the_continuation_body(self):
        out = build_refusal_recovery_prompt([("bash", "reason")], answered=False)
        assert "ended the turn early" in out
        assert "continue the task where you left off" in out
        assert "this note is for awareness" not in out

    def test_backend_abort_overrules_the_harness_interrupted_message(self):
        """codex ends a denied turn as cancelled and then tells the model, twice,
        that the USER interrupted ('aborted by user' on the tool result, a
        <turn_aborted> note). The generic 'not a user action' line loses to two
        harness-authored messages saying the opposite unless the body names them.
        """
        out = build_refusal_recovery_prompt([("bash", "reason")], turn_aborted=True)
        assert "aborted by user" in out
        assert "interrupted the previous turn on purpose" in out
        assert "not an interruption by the user" in out
        assert "disregard those messages" in out
        # The reason and the remediation instruction are unchanged by the flag.
        assert "bash" in out and "reason" in out
        assert "continue the task where you left off" in out

    def test_abort_clause_is_absent_when_the_turn_ran_on(self):
        out = build_refusal_recovery_prompt([("bash", "reason")])
        assert "aborted by user" not in out
        assert "disregard those messages" not in out

    def test_abort_clause_does_not_inflate_the_blocked_count(self):
        # RecoveryCard counts bullet-shaped lines as blocked calls; the clause is
        # prose and must not read as one more blocked tool.
        plain = build_refusal_recovery_prompt([("bash", "reason")])
        aborted = build_refusal_recovery_prompt([("bash", "reason")], turn_aborted=True)

        def bullets(body: str) -> int:
            return sum(1 for line in body.splitlines() if line.lstrip().startswith("- "))

        assert bullets(aborted) == bullets(plain) == 1

    def test_abort_clause_composes_with_the_awareness_body(self):
        out = build_refusal_recovery_prompt([("bash", "reason")], answered=True, turn_aborted=True)
        assert "this note is for awareness" in out
        assert "disregard those messages" in out

    def test_answered_keeps_per_class_remediation(self):
        # The awareness variant drops the resume instruction, not the guidance:
        # "how to do this properly" is the awareness the user is owed.
        reason = "Blocked by security policy: cat ~/.aws/credentials"
        assert build_refusal_recovery_prompt([("Running: cat creds", reason)], answered=True).count(
            "How to do this properly:"
        ) == build_refusal_recovery_prompt([("Running: cat creds", reason)]).count(
            "How to do this properly:"
        )


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
        cb = _context_builder(ToolHookResult.deny("Blocked by security policy: rm -rf /"))
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

        Structural: denying without the hook's reason leaves the model stalled while every
        behavioural assertion passes, so all paths must use the one helper.
        """
        source = "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [
                *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
                Path(chat_runner.__file__),
            ]
            if turn_file.name != "__init__.py"
        )
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
    async def test_blocked_row_and_audit_redact_the_model_authored_title(self, tmp_path) -> None:
        """A credential the model put in the tool title must not reach either surface.

        ``event.title`` is LLM-controlled (``_select_tool_title``) and must be redacted
        (``_safe_reject_title``) before the broadcast, persisted row and the audit.
        """
        # Assembled at runtime, never written as one literal: the redactor only
        # fires on credential-SHAPED input (a plain sentinel passes through
        # untouched, so this test would prove nothing), but a real key shape
        # sitting in the source trips the source-text scanners --
        # internal-content-scan and Semgrep's
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
            await _drive_hook_blocked_turn(state, client, slot, title=f"Deploy with {secret} now")

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


class TestPreToolUseFiresOncePerCall:
    """A call the gate passed to the approval card had its PreToolUse fire there."""

    @pytest.mark.asyncio
    async def test_a_gated_call_approved_on_the_card_fires_once(self, tmp_path) -> None:
        hook_store = _make_hook_store()
        state, client = _make_state(
            tmp_path, context_builder=_context_builder(), hook_store=hook_store
        )
        slot = _make_slot()

        await _drive_hook_blocked_turn(state, client, slot, approve_prompt=True)

        pre_fires = [
            call
            for call in hook_store.fire.await_args_list
            if call.args[0] == HOOK_EVENT_PRE_TOOL_USE
        ]
        assert len(pre_fires) == 1, pre_fires
        client.approve_tool.assert_called_once()
        client.reject_tool.assert_not_called()


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

    ``event.title`` prefers the model's ``description`` (``_select_tool_title``), so a
    planted credential must reach neither a transcript row nor a SEL ``tool_name``.
    Triggers: a title over ``MAX_TOOL_NAME_LEN``, and a PreToolUse hook that raises.
    """

    # Assembled at runtime, never as one literal: the redactor only fires on
    # credential-SHAPED input, and a real key shape in the source trips the
    # source-text scanners (internal-content-scan, Semgrep).
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
        # Stub the name-grant check: `ls` does not exist on Windows, where the check would
        # decline the tier and the `trust_reads` deny below would never happen. The check
        # itself is covered in `test/test_name_grant.py`.
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
        assert any((c.get("metadata") or {}).get("reason") == "trust_reads" for c in denied), denied

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

        Structural: a new path that inlines ``reject`` + row + audit would publish the raw
        title while every behavioral case passes, so each deny shape renders in one helper.
        """
        source = "\n".join(
            turn_file.read_text(encoding="utf-8")
            for turn_file in [
                *sorted(Path(chat_runner.__file__).with_name("chat_turn").glob("*.py")),
                Path(chat_runner.__file__),
            ]
            if turn_file.name != "__init__.py"
        )
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


# ── Long read titles carrying a canonical identity ──


class TestLongTitleWithCanonicalIdentity:
    """A ``read`` whose title embeds long image paths must not be refused as a bad name.

    The identity travels as ``AcpEvent.tool_name`` (``_meta.kiro.toolName``). The cap in
    ``_validate_tool_name`` applies only when the title is the ONLY identity.
    """

    _TITLE = "View image " + " ".join(
        f"/mnt/Sign in with Apple - screenshot {i:02d} of the consent sheet.png" for i in range(6)
    )

    def _read_event(self, *, tool_name: str) -> LLMEvent:
        assert len(self._TITLE) > MAX_TOOL_NAME_LEN
        return LLMEvent(
            kind=EVENT_PERMISSION_REQUEST,
            title=self._TITLE,
            tool_kind="read",
            request_id="req-1",
            tool_input="",
            tool_name=tool_name,
        )

    @pytest.mark.asyncio
    async def test_auto_approve_proceeds_with_canonical_name(self, tmp_path):
        state, client = _make_state(
            tmp_path, context_builder=_context_builder(ToolHookResult.auto_approve())
        )
        slot = _make_slot()
        _set_stream(client, [self._read_event(tool_name="fs_read"), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        client.approve_tool.assert_called_once_with("req-1")
        client.reject_tool.assert_not_called()
        rows = [m.get("content", "") for m in slot.messages]
        assert not any("(invalid:" in row for row in rows), rows
        # The scripted PreToolUse gate still ran, keyed on the sanitised title.
        pre = [
            c
            for c in state._hook_store.fire.call_args_list
            if c.args and c.args[0] == HOOK_EVENT_PRE_TOOL_USE
        ]
        assert pre, state._hook_store.fire.call_args_list
        assert pre[0].kwargs["tool_name"] == self._TITLE

    @pytest.mark.asyncio
    async def test_auto_approve_without_identity_keeps_the_loud_refusal(self, tmp_path):
        """A backend publishing no ``_meta`` identity is refused exactly as before."""
        state, client = _make_state(
            tmp_path, context_builder=_context_builder(ToolHookResult.auto_approve())
        )
        slot = _make_slot()
        _set_stream(client, [self._read_event(tool_name=""), _complete_event()])

        with _patch_stats():
            await _run_chat(state, slot, "hello")

        client.approve_tool.assert_not_called()
        rows = [m.get("content", "") for m in slot.messages]
        assert any("(invalid:" in row and "exceeds max length" in row for row in rows), rows


class TestHostPreDeclinedRowIsBornResolved:
    """What the permission row's one frame carries when the host declines it.

    The chime (``shouldChimeOnPermissionRow``) is gated on the frame's ``resolved``, and a
    later ``approval_resolved`` cannot un-play it. So a no-budget decline is written into
    the row before the append. A failed Slack post is known only after the row is out, so
    its chime is the accepted residual.

    The tests read the frame at EMISSION: the later mark mutates the same dict.
    """

    @staticmethod
    def _record(slot: _ChatSlot, state: DashboardState) -> list[tuple]:
        """Timeline of broadcast rows (role, cls at emission), slots pushes
        (whether an approval future was still pending when the push ran) and
        ``broadcast_ws`` frames (kind, payload)."""
        timeline: list[tuple] = []
        slot._on_message = lambda _key, msg: timeline.append(
            ("row", msg["role"], msg.get("cls", ""))
        )
        state.push_slots_update.side_effect = lambda: timeline.append(
            ("push", any(not f.done() for f in slot._approval_futures.values()))
        )
        state.broadcast_ws.side_effect = lambda kind, payload=None, *_a, **_k: timeline.append(
            ("ws", kind, payload)
        )
        return timeline

    @staticmethod
    def _permission_frames(timeline: list[tuple]) -> list[dict]:
        return [json.loads(t[2]) for t in timeline if t[:2] == ("row", "permission")]

    @pytest.mark.asyncio
    async def test_no_budget_row_is_emitted_resolved_and_the_board_unblocks(self, tmp_path):
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        slot = _make_slot()
        timeline = self._record(slot, state)
        _set_stream(client, [_permission_event(), _complete_event()])

        # No turn budget left is the no-budget branch (the per-slot bound is a
        # positive constant, so only the remaining-budget bound can zero the
        # window): the host declines without waiting, so the row's one
        # delivery is for a prompt already decided.
        with (
            _patch_stats(),
            patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        ):
            await _run_chat(state, slot, "hello")

        (frame,) = self._permission_frames(timeline)
        assert frame["request_id"] == "req-1"
        assert frame["resolved"] == "rejected", (
            "the no-budget prompt's frame left the gateway pending: the client "
            "chimes for a prompt the host declined in the same tick"
        )
        client.reject_tool.assert_awaited_once_with("req-1")
        # The row needed no in-place mark, but the future it registered is
        # gone only in the ``finally`` -- the Board must still learn
        # pending_approval=false before the turn moves on to the denied row.
        row_at = next(i for i, t in enumerate(timeline) if t[:2] == ("row", "permission"))
        denied_at = next(
            i for i, t in enumerate(timeline) if i > row_at and t[:2] == ("row", "tool")
        )
        assert ("push", False) in timeline[row_at:denied_at], (
            "no slots push cleared pending_approval between the born-resolved "
            "row and the denied tool row -- the Board keeps the session Blocked"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize("failure", ["delivery-returns-none", "delivery-raises"])
    async def test_slack_linked_row_is_published_before_the_post_and_retired_when_it_fails(
        self, tmp_path, failure
    ):
        from kiro_crew.dashboard.chat_handlers import _get_pattern_from_pending

        state, client = _make_state(tmp_path, context_builder=_context_builder())
        # A linked slot whose thread the prompt cannot reach. The user-echo
        # mirror resolves its own link through the sessions registry; an empty
        # link keeps that leg out of the way of the approval mirror under test.
        state.slack_client = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))
        slot = _make_slot()
        slot._slack_linked = True
        slot._slack_channel = "C1"
        slot._slack_thread_ts = "1700000000.000100"
        timeline = self._record(slot, state)
        _set_stream(client, [_permission_event(), _complete_event()])

        # A SLOW post double. What the dashboard has while the post is still in
        # flight decides the user's experience on every linked prompt: the row's
        # frame and the Board's pending state must already be out (a post that
        # takes its whole timeout to fail must not hide the card that long), and
        # the Slack card's durable-Trust proof must be readable off that same
        # row (``_linked_trust_grantable``).
        seen_at_post: dict[str, object] = {}
        passed: dict[str, asyncio.Future] = {}

        async def _mirror(_slack, _chan, _ts, request_id, *_a, **_k):
            await asyncio.sleep(0.01)
            seen_at_post["proof"] = _get_pattern_from_pending(
                slot, str(request_id), "trust_grantable"
            )
            seen_at_post["frames"] = len(self._permission_frames(timeline))
            seen_at_post["pending_pushed"] = ("push", True) in timeline
            # The card a click settles is this slot's own request, not any one
            # holding the same id.
            seen_at_post["slot_key"] = _k["slot_key"]
            passed["future"] = _k["future"]
            if failure == "delivery-raises":
                raise RuntimeError("slack down")
            return None

        mirror = AsyncMock(side_effect=_mirror)
        with _patch_stats(), patch.object(chat_runner, "post_linked_approval", mirror):
            await _run_chat(state, slot, "hello")

        mirror.assert_awaited_once()
        assert seen_at_post == {
            "proof": "1",
            "frames": 1,
            "pending_pushed": True,
            "slot_key": slot.key,
        }, (
            "while the Slack post was in flight the dashboard must already hold the "
            "row (one frame out), the Board its pending state, and the mirror its "
            "Trust proof and the slot that asked"
        )
        # The future handed to the mirror is the one the turn parked on: the
        # failed delivery declined it.
        assert passed["future"].result() == "rejected"
        (frame,) = self._permission_frames(timeline)
        assert frame["request_id"] == "req-1"
        # The accepted residual: the frame went out before the delivery outcome
        # existed, so it is pending (and the arrival chime stands). What the
        # failure owes is the retirement, and it owes it at once.
        assert "resolved" not in frame
        retire_at = next(
            i
            for i, t in enumerate(timeline)
            if t[:2] == ("ws", "approval_resolved") and t[2].get("id") == "req-1"
        )
        (permission_row,) = [m for m in slot.messages if m.get("role") == "permission"]
        assert timeline[retire_at][2] == {
            "id": "req-1",
            "approved": False,
            "slot": slot.key,
            # The runner's own request, named by the row it wrote.
            "origin": "native",
            "mid": row_mid(permission_row),
        }
        denied_at = next(i for i, t in enumerate(timeline) if t[:2] == ("row", "tool"))
        assert retire_at < denied_at, (
            "the failed post did not retire the card before the turn moved on to "
            "the denied tool row"
        )
        (stored,) = [m for m in slot.messages if m.get("role") == "permission"]
        assert json.loads(stored["cls"])["resolved"] == "rejected"
        client.reject_tool.assert_awaited_once_with("req-1")
        if failure == "delivery-returns-none":
            # Nobody had answered, so this decline is the runner's own and the
            # user is told so.
            assert any(
                m.get("role") == "assistant" and "auto-declined" in str(m.get("content"))
                for m in slot.messages
            ), "the undelivered prompt was declined without telling the user"
        # Nothing was posted, so nothing is cleaned up on the Slack side.
        state.slack_client.delete_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_answer_given_during_the_slack_post_survives_a_window_that_ran_out(
        self, tmp_path
    ):
        """A decision already made must not be overruled by the no-budget decline.

        The user can answer during the Slack post, and the window is computed after it.
        The wait consumes an arrived answer before consulting the window.
        """
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.slack_client = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))
        slot = _make_slot()
        slot._slack_linked = True
        slot._slack_channel = "C1"
        slot._slack_thread_ts = "1700000000.000100"
        timeline = self._record(slot, state)
        _set_stream(client, [_permission_event(), _complete_event()])

        posted = {"done": False}

        async def _slow_post_answered_meanwhile(_slack, _chan, _ts, request_id, *_a, **_k):
            # The dashboard answers while the post is in flight.
            await _answer_approval(slot, str(request_id), "approved")
            posted["done"] = True
            return "1700000000.000200"

        def _budget() -> float:
            # Budget at the append (the row goes out pending); none left once
            # the post has returned and the wait computes its window.
            return 0.0 if posted["done"] else 70.0

        with (
            _patch_stats(),
            patch.object(chat_runner, "post_linked_approval", _slow_post_answered_meanwhile),
            patch.object(chat_runner, "tool_approval_timeout_secs", side_effect=_budget),
        ):
            await _run_chat(state, slot, "hello")

        (frame,) = self._permission_frames(timeline)
        assert "resolved" not in frame, "the row had budget when it went out"
        client.approve_tool.assert_awaited_once_with("req-1")
        client.reject_tool.assert_not_awaited()
        no_budget_card = format_approval_no_budget_card()
        assert not any(
            m.get("role") == "error" and m.get("content") == no_budget_card for m in slot.messages
        ), "the no-budget card was rendered for a prompt the user had already approved"
        retire = [t for t in timeline if t[:2] == ("ws", "approval_resolved")]
        (row,) = [m for m in slot.messages if m.get("role") == "permission"]
        # The runner's own request, named by the row it wrote.
        resolved = {"id": "req-1", "approved": True, "slot": slot.key, "origin": "native"}
        assert retire == [("ws", "approval_resolved", {**resolved, "mid": row_mid(row)})]
        (stored,) = [m for m in slot.messages if m.get("role") == "permission"]
        assert json.loads(stored["cls"])["resolved"] == "approved"
        # The posted Slack card is cleaned up now that the decision is in.
        state.slack_client.delete_message.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_delivery_failure_after_an_answer_keeps_the_answer_and_says_nothing(
        self, tmp_path
    ):
        """A prompt answered during the post is not reported as auto-declined.

        Both the auto-reject and the auto-declined notice sit under ``if not fut.done()``,
        so an answer given during a failing post stands.
        """
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.slack_client = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))
        slot = _make_slot()
        slot._slack_linked = True
        slot._slack_channel = "C1"
        slot._slack_thread_ts = "1700000000.000100"
        timeline = self._record(slot, state)
        _set_stream(client, [_permission_event(), _complete_event()])

        async def _answered_then_undeliverable(_slack, _chan, _ts, request_id, *_a, **_k):
            # The dashboard answers while the post is in flight; the post then
            # reports that nothing reached the thread.
            await _answer_approval(slot, str(request_id), "approved")
            return None

        with (
            _patch_stats(),
            patch.object(chat_runner, "post_linked_approval", _answered_then_undeliverable),
        ):
            await _run_chat(state, slot, "hello")

        notices = [
            m
            for m in slot.messages
            if m.get("role") == "assistant" and "auto-declined" in str(m.get("content"))
        ]
        assert notices == [], (
            "the delivery-failure arm told the user their prompt was auto-declined "
            "after the dashboard had already approved it"
        )
        client.approve_tool.assert_awaited_once_with("req-1")
        client.reject_tool.assert_not_awaited()
        retire = [t for t in timeline if t[:2] == ("ws", "approval_resolved")]
        (row,) = [m for m in slot.messages if m.get("role") == "permission"]
        # The runner's own request, named by the row it wrote.
        resolved = {"id": "req-1", "approved": True, "slot": slot.key, "origin": "native"}
        assert retire == [("ws", "approval_resolved", {**resolved, "mid": row_mid(row)})]
        (stored,) = [m for m in slot.messages if m.get("role") == "permission"]
        assert json.loads(stored["cls"])["resolved"] == "approved"
        # Nothing was posted, so nothing is cleaned up on the Slack side.
        state.slack_client.delete_message.assert_not_awaited()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("bound", [True, False], ids=["code-channel", "linked-thread"])
    async def test_slack_approval_goes_top_level_in_a_bound_code_channel(self, tmp_path, bound):
        """A code channel's own session asks where it replies: the channel itself.

        Its slot's ``_slack_thread_ts`` is the channel's anchor (kickoff) message,
        so threading the card under it hides the prompt in a thread nobody reads.
        The mirror's own rule (``code_channel_post_thread``) decides; an ordinary
        linked thread still gets the card in its thread.
        """
        from types import SimpleNamespace

        from kiro_crew.slack.code_channels import binding as cc_binding

        anchor = "1700000000.000100"
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.slack_client = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))
        slot = _make_slot()
        slot._slack_linked = True
        slot._slack_channel = "C0CODE"
        slot._slack_thread_ts = anchor
        _set_stream(client, [_permission_event(), _complete_event()])

        async def _post_blocks(_chan, _blocks, _text, _thread_ts=None):
            await _answer_approval(slot, "req-1", "approved")
            return "1700000000.000999"

        state.slack_client.post_blocks = AsyncMock(side_effect=_post_blocks)

        def _active(channel_id, thread_ts):
            if bound and (channel_id, thread_ts) == ("C0CODE", anchor):
                return SimpleNamespace(channel_id=channel_id)
            return None

        with _patch_stats(), patch.object(cc_binding, "active_binding", _active):
            await _run_chat(state, slot, "hello")

        (call,) = [
            c
            for c in state.slack_client.post_blocks.await_args_list
            if c.args[2] == "Manual approval required"
        ]
        assert call.args[0] == "C0CODE"
        assert call.args[3] == (None if bound else anchor)
        client.approve_tool.assert_awaited_once_with("req-1")

    @pytest.mark.asyncio
    async def test_slack_linked_no_budget_row_is_not_mirrored_and_its_future_is_settled(
        self, tmp_path
    ):
        """A pre-declined row settles its future without a cancellable await.

        The Slack post sits before the ``try``/``finally`` that pops the future, and a
        ``CancelledError`` there would leave it unresolved (session stuck Blocked, Continue
        409). So a pre-declined row is not mirrored to Slack at all.
        """
        state, client = _make_state(tmp_path, context_builder=_context_builder())
        state.slack_client = AsyncMock()
        state.sessions.get_slack_link = MagicMock(return_value=("", ""))
        slot = _make_slot()
        slot._slack_linked = True
        slot._slack_channel = "C1"
        slot._slack_thread_ts = "1700000000.000100"
        timeline = self._record(slot, state)
        _set_stream(client, [_permission_event(), _complete_event()])

        # The turn ceiling landing inside the post. The runner's outer handler
        # absorbs the cancellation, so whatever the approval block left behind
        # is what the slot keeps.
        async def _ceiling_cancels_the_post(*_a, **_k):
            raise asyncio.CancelledError()

        mirror = AsyncMock(side_effect=_ceiling_cancels_the_post)
        with (
            _patch_stats(),
            patch.object(chat_runner, "post_linked_approval", mirror),
            patch.object(chat_runner, "tool_approval_timeout_secs", return_value=0.0),
        ):
            await _run_chat(state, slot, "hello")

        assert slot._approval_futures == {}, (
            "the pre-declined prompt's future is still registered: the Board keeps "
            "the session Blocked and Continue answers 409 until something else "
            "rejects it"
        )
        mirror.assert_not_awaited()
        (frame,) = self._permission_frames(timeline)
        assert frame["resolved"] == "rejected"
        client.reject_tool.assert_awaited_once_with("req-1")
        no_budget_card = format_approval_no_budget_card()
        assert any(
            m.get("role") == "error" and m.get("content") == no_budget_card for m in slot.messages
        ), "the no-budget decline was not recorded for the pre-declined prompt"
        # The slots push that clears pending_approval lands before the denied
        # tool row, exactly as for an unlinked slot; nothing was posted, so
        # there is nothing to clean up on the Slack side.
        row_at = next(i for i, t in enumerate(timeline) if t[:2] == ("row", "permission"))
        denied_at = next(
            i for i, t in enumerate(timeline) if i > row_at and t[:2] == ("row", "tool")
        )
        assert ("push", False) in timeline[row_at:denied_at]
        state.slack_client.delete_message.assert_not_awaited()


class TestApprovalAnswerersDoNotRaceTheStream:
    """No answerer may wait on the clock instead of on the approval future.

    Sleeping then reading ``_approval_futures`` once races the stream; a late event parks
    the turn for the 600s window, past CI's ``--timeout=180``, and crashes some xdist worker.
    Source ratchets, because the race passes on an idle machine.
    """

    @staticmethod
    def _functions():
        tree = _answerer_family_tree()
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
                isinstance(n, ast.Attribute) and n.attr == "_approval_futures" for n in ast.walk(fn)
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
            if fn.name != "_answer_approval" and self._calls(fn, attr="get", on="_approval_futures")
        ]
        assert not offenders, (
            "these functions look up an approval future themselves instead of "
            "reusing _answer_approval, so the wait discipline has to be "
            "re-derived at each site: " + ", ".join(offenders)
        )
