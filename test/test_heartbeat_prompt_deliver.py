"""Tests for heartbeat prompt:dashboard:<slot> deliver mode."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest


@pytest.fixture()
def dashboard_state():
    """Minimal DashboardState mock with slot support."""
    state = MagicMock()
    state._background_tasks = set()
    slot = MagicMock()
    slot.running = False
    slot.key = "chat-1"
    slot._queue = []
    slot.task = None
    # Default: prompt runs immediately (slot idle). Individual tests can
    # override to False to simulate the busy/queued path.
    slot.enqueue_or_run_prompt = MagicMock(return_value=True)
    state.get_slot.return_value = slot
    state.resolve_slot.return_value = slot
    state.push_slots_update = MagicMock()
    state.notify = MagicMock()
    return state, slot


@pytest.fixture()
def orchestrator(dashboard_state):
    """Minimal GatewayOrchestrator with dashboard_state wired up.

    Note: bypasses __init__ via __new__. If GatewayOrchestrator.__init__ adds
    new required attributes accessed by _deliver_result, update this fixture.
    """
    from kiro_crew.slack.gateway import GatewayOrchestrator

    orch = GatewayOrchestrator.__new__(GatewayOrchestrator)
    orch.dashboard_state = dashboard_state[0]
    orch.slack = None
    orch._owner_id = None
    return orch


class TestPromptDashboardDeliver:
    """prompt:dashboard:<slot> sends result as user prompt, triggering agent turn."""

    @pytest.mark.asyncio()
    async def test_prompt_triggers_enqueue_or_run(self, orchestrator, dashboard_state):
        from kiro_crew.dashboard.chat import _run_chat

        state, slot = dashboard_state
        await orchestrator._deliver_result(
            "💓 Heartbeat", "CR check", "Fix these comments", "prompt:dashboard:chat-1"
        )
        slot.enqueue_or_run_prompt.assert_called_once()
        call_args = slot.enqueue_or_run_prompt.call_args
        prompt = call_args.args[0]
        assert "Fix these comments" in prompt
        # run_chat coro and state are passed through in the documented positional order
        assert call_args.args[1] is _run_chat
        assert call_args.args[2] is state

    @pytest.mark.asyncio()
    async def test_prompt_warns_on_missing_slot(self, orchestrator, dashboard_state, caplog, monkeypatch):
        state, _ = dashboard_state
        state.resolve_slot.return_value = None
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        await orchestrator._deliver_result(
            "💓 Heartbeat", "CR check", "Fix these", "prompt:dashboard:chat-99"
        )
        assert any("not found" in r.message for r in caplog.records)
        # notify must NOT fire when slot is missing (avoids dead-link notifications)
        state.notify.assert_not_called()
        # SEL audit event fires on not_found so operators can detect misconfig/probing
        sel_mock.log_api_access.assert_called_once()
        kwargs = sel_mock.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "not_found"
        assert kwargs["operation"] == "heartbeat_prompt_deliver"
        assert "chat-99" in kwargs["resources"]

    @pytest.mark.asyncio()
    async def test_prompt_sends_notification_once(self, orchestrator, dashboard_state):
        state, slot = dashboard_state
        await orchestrator._deliver_result(
            "💓 Heartbeat", "CR check", "Fix these", "prompt:dashboard:chat-1"
        )
        state.notify.assert_called_once()
        call_args = state.notify.call_args
        assert call_args[0][0] == "heartbeat"
        assert call_args[1]["meta"]["slot"] == "chat-1"

    @pytest.mark.asyncio()
    async def test_prompt_skips_notify_and_push_when_queued(
        self, orchestrator, dashboard_state
    ):
        """Queued prompts (slot busy) must NOT fire notify() or push_slots_update() —
        the prompt has no visible effect until it's dequeued, so notifying now would
        send the user to an empty slot.
        """
        state, slot = dashboard_state
        slot.enqueue_or_run_prompt.return_value = False  # simulate queued path
        await orchestrator._deliver_result(
            "💓 Heartbeat", "CR check", "Fix these", "prompt:dashboard:chat-1"
        )
        slot.enqueue_or_run_prompt.assert_called_once()
        state.notify.assert_not_called()
        state.push_slots_update.assert_not_called()

    @pytest.mark.asyncio()
    async def test_prompt_fires_notify_and_push_when_run(
        self, orchestrator, dashboard_state
    ):
        """Counterpart to queued case: when slot is idle, both notify and
        push_slots_update must fire so the UI reflects the new agent turn.
        """
        state, slot = dashboard_state
        slot.enqueue_or_run_prompt.return_value = True  # default, but explicit
        await orchestrator._deliver_result(
            "💓 Heartbeat", "CR check", "Fix these", "prompt:dashboard:chat-1"
        )
        state.notify.assert_called_once()
        state.push_slots_update.assert_called_once()

    @pytest.mark.asyncio()
    async def test_prompt_noop_when_no_dashboard_state(self, orchestrator, caplog):
        import logging

        caplog.set_level(logging.DEBUG, logger="kiro_crew.slack.gateway")
        orchestrator.dashboard_state = None
        await orchestrator._deliver_result(
            "💓 Heartbeat", "CR check", "Fix these", "prompt:dashboard:chat-1"
        )
        # Assert the observability log fires so a silent regression removing
        # the logger.debug would fail the test (not just the no-crash case).
        assert any(
            "no dashboard_state" in r.message for r in caplog.records
        )

    @pytest.mark.asyncio()
    async def test_prompt_empty_slot_name_ignored(self, orchestrator, dashboard_state, caplog):
        import logging

        # Scope to the gateway logger explicitly — xdist workers may not
        # propagate DEBUG from the root logger reliably.
        caplog.set_level(logging.DEBUG, logger="kiro_crew.slack.gateway")
        state, _ = dashboard_state
        await orchestrator._deliver_result(
            "💓 Heartbeat", "CR check", "Fix these", "prompt:dashboard:"
        )
        state.get_slot.assert_not_called()
        state.resolve_slot.assert_not_called()
        state.notify.assert_not_called()
        assert any("missing slot name" in r.message for r in caplog.records)

    @pytest.mark.asyncio()
    async def test_prompt_truncates_oversized_ascii(self, orchestrator, dashboard_state, caplog):
        from kiro_crew.dashboard.handlers import MAX_PROMPT_BYTES

        state, slot = dashboard_state
        oversize = "x" * (MAX_PROMPT_BYTES + 1000)
        await orchestrator._deliver_result(
            "💓 Heartbeat", "CR check", oversize, "prompt:dashboard:chat-1"
        )
        delivered = slot.enqueue_or_run_prompt.call_args.args[0]
        assert len(delivered.encode("utf-8")) <= MAX_PROMPT_BYTES
        assert any("truncated" in r.message for r in caplog.records)

    @pytest.mark.asyncio()
    async def test_prompt_truncates_utf8_boundary_safely(self, orchestrator, dashboard_state):
        """Truncation at a multi-byte UTF-8 boundary must produce valid UTF-8 within the limit.

        Constructs content that splits a 3-byte char at the truncation point,
        verifying errors='ignore' drops the partial bytes without expanding size.
        """
        from kiro_crew.dashboard.handlers import MAX_PROMPT_BYTES

        state, slot = dashboard_state
        title = "💓 Heartbeat"
        summary = "CR check"
        prefix_bytes = len(f"{title}\n\n".encode("utf-8"))
        ascii_pad_len = MAX_PROMPT_BYTES - prefix_bytes - 1
        content = "x" * ascii_pad_len + "你" + "y" * 100
        await orchestrator._deliver_result(
            title, summary, content, "prompt:dashboard:chat-1"
        )
        delivered = slot.enqueue_or_run_prompt.call_args.args[0]
        delivered.encode("utf-8").decode("utf-8")  # raises if invalid
        assert len(delivered.encode("utf-8")) <= MAX_PROMPT_BYTES
        assert "\ufffd" not in delivered

    @pytest.mark.asyncio()
    async def test_prompt_passes_through_at_size_boundary(self, orchestrator, dashboard_state):
        """Content sized exactly at the limit (inclusive of prefix) must not be truncated."""
        from kiro_crew.dashboard.handlers import MAX_PROMPT_BYTES

        state, slot = dashboard_state
        title = "💓 Heartbeat"
        summary = "CR check"
        prefix_bytes = len(f"{title}\n\n".encode("utf-8"))
        content = "x" * (MAX_PROMPT_BYTES - prefix_bytes)
        await orchestrator._deliver_result(
            title, summary, content, "prompt:dashboard:chat-1"
        )
        delivered = slot.enqueue_or_run_prompt.call_args.args[0]
        assert content in delivered
        assert len(delivered.encode("utf-8")) == MAX_PROMPT_BYTES

    @pytest.mark.asyncio()
    async def test_enqueue_registers_exception_logger_internally(self, caplog):
        """enqueue_or_run_prompt always registers _log_task_exception — verify via failing task."""
        import logging
        from unittest.mock import MagicMock

        from kiro_crew.dashboard.state import _ChatSlot

        caplog.set_level(logging.ERROR)
        slot = _ChatSlot(key="chat-1")
        state = MagicMock()
        state._background_tasks = set()

        async def failing_coro(*args):
            raise RuntimeError("test error")

        slot.enqueue_or_run_prompt("hello", failing_coro, state)
        try:
            await slot.task
        except RuntimeError:
            pass

        assert any("Background task failed" in r.message for r in caplog.records)


class TestDashboardSlotInjectDeliver:
    """dashboard:<slot> inject path (non-prompt)."""

    @pytest.mark.asyncio()
    async def test_inject_resolves_via_prefix(self, orchestrator, dashboard_state):
        state, slot = dashboard_state
        slot.key = "chat-1-1776476208"
        state._slots = {"chat-1-1776476208": slot}
        from kiro_crew.dashboard.state import DashboardState

        state.resolve_slot = lambda name: DashboardState.resolve_slot(state, name)

        await orchestrator._deliver_result(
            "💓 Heartbeat", "test", "result", "dashboard:chat-1"
        )
        slot.append.assert_called_once()

    @pytest.mark.asyncio()
    async def test_inject_no_notify_when_slot_missing(self, orchestrator, dashboard_state, monkeypatch):
        state, _ = dashboard_state
        state.resolve_slot.return_value = None
        sel_mock = MagicMock()
        monkeypatch.setattr("kiro_crew.slack.gateway.sel", lambda: sel_mock)
        await orchestrator._deliver_result(
            "💓 Heartbeat", "test", "result", "dashboard:chat-99"
        )
        state.notify.assert_not_called()
        # SEL audit event fires on not_found for inject path too
        sel_mock.log_api_access.assert_called_once()
        kwargs = sel_mock.log_api_access.call_args.kwargs
        assert kwargs["outcome"] == "not_found"
        assert kwargs["operation"] == "heartbeat_inject_deliver"


class TestLogTaskException:
    """Direct unit tests for _log_task_exception helper."""

    @pytest.mark.asyncio()
    async def test_cancelled_task_short_circuits(self, caplog):
        """Cancelled tasks must not trigger logging (would raise CancelledError)."""
        import asyncio
        import logging

        from kiro_crew.dashboard.state import _log_task_exception

        caplog.set_level(logging.ERROR)
        task = asyncio.create_task(asyncio.sleep(1))
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        # Must not raise and must not log
        _log_task_exception(task)
        assert not any("Background task failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio()
    async def test_successful_task_does_not_log(self, caplog):
        """Tasks that complete without exception must not log."""
        import asyncio
        import logging

        from kiro_crew.dashboard.state import _log_task_exception

        caplog.set_level(logging.ERROR)

        async def ok():
            return 42

        task = asyncio.create_task(ok())
        await task
        _log_task_exception(task)
        assert not any("Background task failed" in r.message for r in caplog.records)

    @pytest.mark.asyncio()
    async def test_traceback_is_redacted(self, caplog):
        """Credential patterns in the traceback (not just the exception message)
        must be redacted. Prevents the regression where `exc_info=exc` would cause
        the logging framework to append the unredacted traceback to log sinks,
        bypassing redact_credentials.
        """
        import asyncio
        import logging

        from kiro_crew.dashboard.state import _log_task_exception

        caplog.set_level(logging.ERROR)

        async def leaky():
            # AKIA-prefixed key is a recognized credential pattern
            raise RuntimeError("creds leaked: AKIAIOSFODNN7EXAMPLE")

        task = asyncio.create_task(leaky())
        try:
            await task
        except RuntimeError:
            pass
        _log_task_exception(task)
        log_output = "\n".join(r.getMessage() for r in caplog.records)
        assert "Background task failed" in log_output
        # Recognized credential pattern must not appear anywhere in the log output
        # (including in the traceback frames, not just the exception message).
        assert "AKIAIOSFODNN7EXAMPLE" not in log_output
        assert "[REDACTED: credential]" in log_output

    @pytest.mark.asyncio()
    async def test_redaction_failure_falls_back_to_raw(self, caplog, monkeypatch):
        """If redact_credentials raises, fallback logs exc type+message unredacted."""
        import asyncio
        import logging

        from kiro_crew.dashboard import state as state_mod
        from kiro_crew.dashboard.state import _log_task_exception

        caplog.set_level(logging.ERROR)
        monkeypatch.setattr(state_mod, "redact_credentials", lambda _tb: (_ for _ in ()).throw(ValueError("boom")))

        async def failing():
            raise RuntimeError("original error")

        task = asyncio.create_task(failing())
        try:
            await task
        except RuntimeError:
            pass
        _log_task_exception(task)
        log_output = "\n".join(r.getMessage() for r in caplog.records)
        assert "redaction error" in log_output
        assert "RuntimeError" in log_output


class TestSlotRunningIsDerived:
    """slot.running is a @property derived from slot.task (see _ChatSlot.running).

    Documents that the prompt:dashboard:<slot> path cannot race on slot.running:
    setting slot.task = task immediately flips running to True, so a second
    _deliver_result call will hit the queue branch as expected.
    """

    def test_running_is_property_not_assignable_attr(self):
        from kiro_crew.dashboard.state import _ChatSlot

        # running is a property on the class, not a settable instance attribute
        assert isinstance(_ChatSlot.running, property)

    @pytest.mark.asyncio()
    async def test_slot_running_true_as_soon_as_task_assigned(self):
        """As soon as slot.task is set, slot.running returns True — no race window."""
        import asyncio

        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot(key="chat-x")
        assert slot.running is False

        async def sleeper():
            await asyncio.sleep(0.05)

        task = asyncio.create_task(sleeper())
        slot.task = task
        # Immediately after assignment (no awaits in between), running must be True
        assert slot.running is True
        await task
        assert slot.running is False


class TestSlotPrefixMatch:
    """resolve_slot supports bare chat-N prefix matching chat-N-<timestamp>.

    get_slot remains exact-match only (for HTTP handlers that pass the name
    to key-derivation functions like _history_key_for).
    """

    def _state_with_slots(self, slots: dict):
        from unittest.mock import MagicMock
        state = MagicMock()
        state._slots = slots
        return state

    def test_get_slot_is_exact_match_only(self):
        """get_slot must NOT do prefix matching — it's used by HTTP handlers
        that derive keys from the input name."""
        from kiro_crew.dashboard.state import DashboardState, _ChatSlot

        slots = {"chat-2-1776476208": _ChatSlot(key="chat-2-1776476208")}
        state = self._state_with_slots(slots)
        assert DashboardState.get_slot(state, "chat-2") is None

    def test_resolve_slot_exact_match_preferred(self):
        """When both exact and prefix matches exist, exact wins."""
        from kiro_crew.dashboard.state import DashboardState, _ChatSlot

        exact = _ChatSlot(key="chat-2")
        timestamped = _ChatSlot(key="chat-2-1776476208")
        slots = {"chat-2": exact, "chat-2-1776476208": timestamped}
        state = self._state_with_slots(slots)
        assert DashboardState.resolve_slot(state, "chat-2") is exact

    def test_resolve_slot_prefix_fallback(self):
        from kiro_crew.dashboard.state import DashboardState, _ChatSlot

        timestamped = _ChatSlot(key="chat-2-1776476208")
        state = self._state_with_slots({"chat-2-1776476208": timestamped})
        assert DashboardState.resolve_slot(state, "chat-2") is timestamped

    def test_resolve_slot_no_match_returns_none(self):
        from kiro_crew.dashboard.state import DashboardState

        state = self._state_with_slots({})
        assert DashboardState.resolve_slot(state, "chat-99") is None

    def test_resolve_slot_tie_break_prefers_newest_timestamp(self):
        """When multiple slots share the prefix, return the one with the largest
        trailing <timestamp> (newest) — NOT dict-insertion order. A gateway
        restart can leave a stale slot alongside the live one; iteration-order
        tie-break used to route chat-N to the stale slot.
        """
        from kiro_crew.dashboard.state import DashboardState, _ChatSlot

        newer = _ChatSlot(key="chat-2-200")
        oldest = _ChatSlot(key="chat-2-100")
        # Insert the STALE (oldest) slot FIRST so a first-insertion tie-break
        # would wrongly return it; the fix must return the newest instead.
        slots = {"chat-2-100": oldest, "chat-2-200": newer}
        state = self._state_with_slots(slots)
        assert DashboardState.resolve_slot(state, "chat-2") is newer

    def test_resolve_slot_does_not_match_cross_prefix(self):
        """chat-2 must not match chat-20-... — prefix requires trailing '-'."""
        from kiro_crew.dashboard.state import DashboardState, _ChatSlot

        slots = {"chat-20-100": _ChatSlot(key="chat-20-100")}
        state = self._state_with_slots(slots)
        assert DashboardState.resolve_slot(state, "chat-2") is None

    def test_resolve_slot_rejects_non_chat_n_names(self):
        """Prefix fallback must NOT fire for names outside chat-N pattern —
        prevents broad matches like "chat" binding to any slot."""
        from kiro_crew.dashboard.state import DashboardState, _ChatSlot

        slots = {
            "chat-2-100": _ChatSlot(key="chat-2-100"),
            "cron-1-200": _ChatSlot(key="cron-1-200"),
        }
        state = self._state_with_slots(slots)
        # "chat" with no number → no fallback
        assert DashboardState.resolve_slot(state, "chat") is None
        # Non-chat namespace → no fallback
        assert DashboardState.resolve_slot(state, "cron-1") is None
        # Empty string → no fallback
        assert DashboardState.resolve_slot(state, "") is None

    @pytest.mark.asyncio()
    async def test_notify_meta_uses_resolved_slot_key(self, orchestrator, dashboard_state):
        """When delivery is via bare chat-N, notify meta must carry the resolved
        full slot key so jump-to-source links in the UI work correctly.
        """
        state, slot = dashboard_state
        slot.key = "chat-1-1776476208"
        state._slots = {"chat-1-1776476208": slot}
        from kiro_crew.dashboard.state import DashboardState
        state.resolve_slot = lambda name: DashboardState.resolve_slot(state, name)

        await orchestrator._deliver_result(
            "💓 Heartbeat", "test", "result", "prompt:dashboard:chat-1"
        )
        state.notify.assert_called_once()
        meta = state.notify.call_args[1]["meta"]
        assert meta["slot"] == "chat-1-1776476208"

    @pytest.mark.asyncio()
    async def test_heartbeat_deliver_with_bare_prefix(self, orchestrator, dashboard_state):
        """prompt:dashboard:chat-1 resolves via prefix to chat-1-<ts> slot."""
        state, slot = dashboard_state
        slot.key = "chat-1-1776476208"
        state._slots = {"chat-1-1776476208": slot}
        from kiro_crew.dashboard.state import DashboardState
        state.resolve_slot = lambda name: DashboardState.resolve_slot(state, name)

        await orchestrator._deliver_result(
            "💓 Heartbeat", "test", "result", "prompt:dashboard:chat-1"
        )
        slot.enqueue_or_run_prompt.assert_called_once()


class TestEnqueueOrRunPrompt:
    """Unit tests for _ChatSlot.enqueue_or_run_prompt."""

    @pytest.mark.asyncio()
    async def test_runs_when_idle(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot(key="chat-1")
        state = MagicMock()
        state._background_tasks = set()
        mock_coro = AsyncMock(return_value=None)

        ran = slot.enqueue_or_run_prompt("hello", mock_coro, state)
        await asyncio.sleep(0)

        assert ran is True  # idle → ran immediately
        mock_coro.assert_called_once_with(state, slot, "hello")
        assert slot.task is not None
        assert slot.messages[-1]["role"] == "user"
        await slot.task

    @pytest.mark.asyncio()
    async def test_queues_when_running(self):
        import asyncio
        from unittest.mock import AsyncMock

        from kiro_crew.dashboard.state import _ChatSlot

        slot = _ChatSlot(key="chat-1")
        # Make slot appear busy
        slot.task = asyncio.create_task(asyncio.sleep(10))
        state = MagicMock()
        state._background_tasks = set()
        mock_coro = AsyncMock()

        ran = slot.enqueue_or_run_prompt("queued msg", mock_coro, state)

        assert ran is False  # busy → queued, did not run
        mock_coro.assert_not_called()
        # Post-_queue items are dicts with id+content.
        assert len(slot._queue) == 1
        assert slot._queue[0]["content"] == "queued msg"
        assert "id" in slot._queue[0]

        slot.task.cancel()
        try:
            await slot.task
        except asyncio.CancelledError:
            pass
