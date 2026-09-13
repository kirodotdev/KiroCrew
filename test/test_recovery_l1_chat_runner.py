"""L1 of the recovery ladder end to end: handle classifies, chat runner retries.

The ACP handle owns the classification (one place, at the protocol layer);
chat_runner reads ``client.last_infra_error`` at end of turn and re-queues ONE
continuation on the shared schedule. The verdict reaches that consumer through
the provider wrapper chain (AcpProvider -> AcpSessionProvider -> handle), which
is exercised on real objects here. The runner branch is pinned at source level
because its host function is the 12k-line turn loop.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew.acp.client import AcpClient
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import EVENT_COMPLETE, JsonRpcMessage
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_health as sh
from kiro_crew.dashboard.state import REFUSAL_RECOVERY_PREFIX
from kiro_crew.providers.acp import AcpProvider
from kiro_crew.providers.base import LLMEvent, LLMProvider
from kiro_crew.recovery import ladder as lad


class _Runtime:
    def __init__(self, queue: asyncio.Queue) -> None:
        self.pid = None
        self.is_alive = MagicMock(return_value=True)
        self.send_notification = AsyncMock()
        self.supports_image_prompt = False
        self.acp_backend = ""
        self._queue = queue

    def mark_turn_active(self, session_id: str, active: bool) -> None:
        pass


def _handle() -> AcpSessionHandle:
    queue: asyncio.Queue = asyncio.Queue()
    return AcpSessionHandle("sA", queue, _Runtime(queue))


def _tool_call(tool_id: str) -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": tool_id,
                "title": "call a tool",
                "kind": "other",
                "status": "in_progress",
            },
        },
    )


def _tool_result(tool_id: str, text: str, status: str = "failed") -> JsonRpcMessage:
    return JsonRpcMessage(
        method="session/update",
        params={
            "sessionId": "sA",
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": tool_id,
                "status": status,
                "content": [{"type": "content", "content": {"type": "text", "text": text}}],
            },
        },
    )


_CAPACITY = 'MCP error -32001: {"class": "capacity", "retry_after_secs": 9} — gateway at capacity'


class TestHandleClassifies:
    def test_capacity_refusal_is_recorded_with_its_retry_hint(self):
        h = _handle()
        assert h.last_infra_error is None
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        assert h.last_infra_error is not None
        assert h.last_infra_error.error_class == lad.CLASS_CAPACITY
        assert h.last_infra_error.retry_after_secs == 9.0

    def test_a_later_ordinary_result_clears_it(self):
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        h._handle_update(_tool_call("t2"))
        h._handle_update(_tool_result("t2", "file contents here", status="completed"))
        assert h.last_infra_error is None

    def test_a_later_output_less_completion_clears_it_too(self):
        """A tool that completes with NO output emits no result event at all, so
        the verdict is retired when the next call is DISPATCHED — otherwise the
        turn's consumer re-issues a call an intervening one superseded."""
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        h._handle_update(_tool_call("t2"))
        assert h.last_infra_error is None
        h._handle_update(_tool_result("t2", "", status="completed"))
        assert h.last_infra_error is None

    def test_an_ordinary_failure_is_not_infra(self):
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", "Permission denied: /etc/shadow"))
        assert h.last_infra_error is None

    def test_a_new_turn_starts_clean(self):
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        assert h.last_infra_error is not None
        # The per-turn reset is the same one that clears the stop reason.
        src = inspect.getsource(AcpSessionHandle)
        idx = src.index("self.last_infra_error = None")
        assert 'self._last_stop_reason = ""' in src[:idx]

    def test_gateway_recoverable_infra_marker(self):
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", "BackendGone: the pooled backend exited"))
        assert h.last_infra_error is not None
        assert h.last_infra_error.error_class == lad.CLASS_RECOVERABLE_INFRA


class _BareProvider(LLMProvider):
    """Implements ONLY the abstract surface: every capability is the ABC default."""

    async def start(self) -> None:
        return None

    async def shutdown(self) -> None:
        return None

    async def stream(self, message: str):
        yield LLMEvent(kind=EVENT_COMPLETE)

    async def approve_tool(self, request_id, *, always: bool = False) -> None:
        return None

    async def reject_tool(self, request_id) -> None:
        return None

    def context_usage_pct(self) -> float:
        return 0.0


class TestVerdictReachesTheConsumer:
    """The handle's verdict must survive both provider wrapper hops.

    Real objects, not source strings: the chat and sub-agent consumers hold
    ``AcpProvider`` (whose inner client is an ``AcpSessionProvider`` on the kiro
    path), never the handle, so a verdict that stops at the handle leaves the L1
    branch unreachable.
    """

    def _provider_with_capacity_refusal(self) -> tuple[AcpSessionHandle, AcpSessionProvider]:
        h = _handle()
        h._handle_update(_tool_call("t1"))
        h._handle_update(_tool_result("t1", _CAPACITY))
        return h, AcpSessionProvider(h, h._runtime)

    def test_session_provider_reads_through_to_the_handle(self):
        h, provider = self._provider_with_capacity_refusal()
        assert provider.last_infra_error is h.last_infra_error
        assert provider.last_infra_error.error_class == lad.CLASS_CAPACITY

    def test_the_forward_is_not_cached(self):
        h, provider = self._provider_with_capacity_refusal()
        assert provider.last_infra_error is not None
        h._handle_update(_tool_call("t2"))
        h._handle_update(_tool_result("t2", "file contents here", status="completed"))
        assert provider.last_infra_error is None

    def test_an_empty_output_leaves_no_stale_candidate(self):
        h, provider = self._provider_with_capacity_refusal()
        h._handle_update(_tool_call("t2"))
        h._handle_update(_tool_result("t2", "", status="completed"))
        assert provider.last_infra_error is None

    def test_acp_provider_reads_through_its_inner_client(self):
        _, inner = self._provider_with_capacity_refusal()
        outer = object.__new__(AcpProvider)
        outer._client = inner
        assert outer.last_infra_error is inner.last_infra_error
        assert outer.last_infra_error.error_class == lad.CLASS_CAPACITY

    def test_a_client_that_never_classifies_answers_no_verdict(self):
        """The placeholder AcpClient before the kiro swap, and the claude seam."""
        outer = object.__new__(AcpProvider)
        outer._client = MagicMock(spec=AcpClient)
        assert outer.last_infra_error is None

    def test_the_base_provider_default_is_no_verdict(self):
        assert _BareProvider().last_infra_error is None

    def test_the_forward_is_read_only(self):
        """The classifying handle is the sole writer: nothing else may fabricate a
        verdict, which is what would force a tool re-issue."""
        _, inner = self._provider_with_capacity_refusal()
        outer = object.__new__(AcpProvider)
        outer._client = inner
        for holder in (inner, outer, _BareProvider()):
            with pytest.raises(AttributeError):
                holder.last_infra_error = lad.InfraError(lad.CLASS_CAPACITY)

    def test_an_l1_wait_reads_as_recovering_with_its_own_cause(self):
        slot = SimpleNamespace(key="chat-1-1", running=True, _infra_retries=1)
        snap = sh.snapshot_slot(slot, mono_now=1000.0)
        assert snap.recovery_kinds == ["infra_capacityx1"]


class TestRunnerBranch:
    """Source-level pins on the L1 branch of the turn loop."""

    @pytest.fixture(scope="class")
    def src(self) -> str:
        return inspect.getsource(chat_runner)

    def test_reads_the_handles_verdict_not_a_regex(self, src):
        assert 'isinstance(getattr(client, "last_infra_error", None), InfraError)' in src
        assert "default_ladder().observe_failure(" in src
        assert "L1_TOOL_CALL," in src

    def test_uses_every_sibling_guard(self, src):
        start = src.index('isinstance(getattr(client, "last_infra_error", None), InfraError)')
        branch = src[start : start + 1200]
        for guard in (
            "_prompt_depth == 0",
            "_stop_reason == STOP_REASON_END_TURN",
            "not _armed_final",
            "not slot._in_stage_execution",
            "not _should_suppress_requeue(slot)",
            "_stop_gen_turn_start",
            "not _has_user_queued_followup(slot)",
            "_pending_steers",
        ):
            assert guard in branch or guard in src[start - 600 : start], guard

    def test_retry_waits_then_requeues_a_continuation_not_the_message(self, src):
        start = src.index("_l1 = default_ladder().observe_failure(")
        body = src[start : start + 2500]
        assert "await _recovery_delay(_l1.delay_secs)" in body
        assert "build_infra_retry_prompt(" in body
        assert "payload=RecoveryPayload.CONTINUATION" in body
        assert "build_recovery_requeue(" not in body  # never a verbatim replay
        assert "_recovering_infra = True" in body

    def test_un_landed_turn_is_excluded_from_success_accounting(self, src):
        assert src.count("and not _recovering_infra") >= 3

    def test_a_landed_turn_closes_the_l1_run(self, src):
        assert "default_ladder().observe_success(L1_TOOL_CALL, slot.key)" in src

    def test_an_escalated_run_is_forgotten_not_recorded_as_recovered(self, src):
        assert "_l1_escalated = True" in src
        assert "default_ladder().forget(L1_TOOL_CALL, slot.key)" in src
        # The give-up must NOT reuse _recovering_infra: that flag also suppresses
        # turn settlement, the other budget resets and consolidation.
        esc = src.index("_l1_escalated = True")
        assert "_recovering_infra = True" not in src[esc : esc + 400]

    def test_the_wait_spends_its_own_budget_not_the_transient_5xx_one(self, src):
        start = src.index("_l1 = default_ladder().observe_failure(")
        body = src[start : start + 2500]
        assert "slot._infra_retries += 1" in body
        assert "slot._transient_5xx_retries += 1" not in body

    def test_the_l1_count_is_reset_on_every_no_requeue_exit(self, src):
        # The happy-path reset only runs when a cycle COMPLETES, so each arm that
        # ENDS the turn clears it too — otherwise the slot reads "recovering" on
        # the health panel until some later turn happens to land.
        assert src.count("slot._infra_retries = 0") == src.count("slot._transient_5xx_retries = 0")


class TestContinuationPrompt:
    def test_opens_with_the_refusal_card_marker(self):
        msg = chat_runner.build_infra_retry_prompt("capacity", 9.0)
        assert msg.split("\n", 1)[0] == REFUSAL_RECOVERY_PREFIX
        assert "9s" in msg
        assert "same arguments" in msg
        assert "Do not repeat any earlier tool call" in msg

    def test_without_a_hint(self):
        msg = chat_runner.build_infra_retry_prompt("recoverable_infra", None)
        assert "pause" not in msg
        assert "recoverable_infra" in msg


@pytest.mark.asyncio
async def test_recovery_delay_seam_sleeps_only_for_positive_values(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(chat_runner.asyncio, "sleep", fake_sleep)
    await chat_runner._recovery_delay(0.0)
    await chat_runner._recovery_delay(0.05)
    assert slept == [0.05]
