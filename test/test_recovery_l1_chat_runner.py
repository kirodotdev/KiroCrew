"""L1 of the recovery ladder end to end: handle classifies, chat runner retries.

The ACP handle owns the classification (one place, at the protocol layer);
chat_runner reads ``client.last_infra_error`` at end of turn and re-queues ONE
continuation on the shared schedule. The verdict reaches that consumer through
the provider wrapper chain (AcpProvider -> AcpSessionProvider -> handle), which
is exercised on real objects here. The runner branch is pinned at source level
because its host function is the 12k-line turn loop.

The two transient-5xx recovery waits share the L1 arm's shape — guard, then a
multi-second sleep, then a re-queue — so their post-backoff re-reads are pinned
here too, on the same slot/state fixtures. What each arm re-reads DIFFERS by
arm, and the reason is the payload: a verbatim replay of an un-run user message
is owed to the user, a continuation of a half-finished turn is not.
"""

from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from chat_test_helpers import _make_ready_kiro_prerequisite

from kiro_crew.acp.client import AcpClient, AcpError
from kiro_crew.acp.session_handle import AcpSessionHandle
from kiro_crew.acp.session_provider import AcpSessionProvider
from kiro_crew.acp.types import EVENT_COMPLETE, EVENT_TEXT_CHUNK, JsonRpcMessage
from kiro_crew.dashboard import chat_runner
from kiro_crew.dashboard import session_health as sh
from kiro_crew.dashboard.chat import _run_chat
from kiro_crew.dashboard.chat_utils import (
    SYNTHETIC_RECOVERY_KIND,
    TRANSIENT_NOTICE_GIVE_UP,
    TRANSIENT_NOTICE_META_KEY,
)
from kiro_crew.dashboard.session_control import containment_meta
from kiro_crew.dashboard.state import REFUSAL_RECOVERY_PREFIX, DashboardState, _ChatSlot
from kiro_crew.history import ConversationLog
from kiro_crew.hooks import ToolHookResult
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

    @staticmethod
    def _retry_arm(src: str) -> str:
        """The retry arm, bounded by the give-up arm that follows it.

        A fixed character window silently stops covering the arm as it grows (the
        post-backoff re-check pushed the re-queue out of a 2500-char slice).
        """
        start = src.index("_l1 = default_ladder().observe_failure(")
        return src[start : src.index("_l1_escalated = True", start)]

    def test_retry_waits_then_requeues_a_continuation_not_the_message(self, src):
        body = self._retry_arm(src)
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
        body = self._retry_arm(src)
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


class _RecordingSlot(_ChatSlot):
    """A real slot that records every recovery continuation queued on it.

    ``_queue_recovery`` reaches the queue through ``slot.queue_insert``, and the
    entry it inserts is drained (or blocked) before the turn returns, so reading
    the queue afterwards cannot tell "never queued" from "queued and dispatched".
    """

    def __init__(self, key: str) -> None:
        super().__init__(key)
        self.recovery_inserts: list[str] = []
        # The whole queue immediately after each recovery insert, so a test can
        # read where the entry landed relative to anything queued during the wait.
        self.queue_after_recovery: list[list[str]] = []

    def queue_insert(self, index, content, **kw):  # type: ignore[override]
        queue_id = super().queue_insert(index, content, **kw)
        if kw.get("kind") == SYNTHETIC_RECOVERY_KIND:
            self.recovery_inserts.append(content)
            self.queue_after_recovery.append([q["content"] for q in self._queue])
        return queue_id


def _notices(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "notice"]


def _l1_state(tmp_path):
    """A DashboardState whose client ends every turn on an infra refusal."""
    sessions = MagicMock(count=0)
    sessions.get_pid = MagicMock(return_value=None)
    client = AsyncMock()
    sessions.get_or_create = AsyncMock(return_value=(client, True, False))
    sessions.record_failure = AsyncMock()
    sessions.check_context_usage = MagicMock()
    sessions.stop_generation = MagicMock(return_value=0)
    state = DashboardState(
        sessions=sessions,
        crons=MagicMock(list_jobs=MagicMock(return_value=[]), status=MagicMock(return_value={})),
        lessons=MagicMock(load_all=MagicMock(return_value=[])),
        start_time=0.0,
        conversation_log=ConversationLog(base_dir=tmp_path),
    )
    state.kiro_prerequisite_service = _make_ready_kiro_prerequisite()
    cb = MagicMock()
    cb.hooks.on_tool_call.return_value = ToolHookResult.allow()
    cb.build_message.return_value = ("hello", None)
    state.context_builder = cb
    hook_store = MagicMock()
    hook_store.fire = AsyncMock(return_value=[])
    state._hook_store = hook_store
    state.broadcast_ws = MagicMock()
    state.push_slots_update = MagicMock()
    client.context_usage_pct = MagicMock(return_value=0.0)
    client.context_window_tokens = MagicMock(return_value=0)
    client._client = client
    client.last_prompt_stats = None
    # The handle's verdict, on the real type the runner isinstance-checks.
    client.last_infra_error = lad.InfraError(lad.CLASS_CAPACITY, retry_after_secs=0.01)
    return state, client


async def _turn_interrupted_during_the_backoff(tmp_path, key, deliver):
    """One end_turn turn whose last tool result was an infra refusal.

    ``deliver(state, slot)`` runs INSIDE the L1 backoff seam, so it stands in for
    an interrupt that arrives while the slot waits: the arm's guards were all
    clear when it decided to retry. Returns the slot, how many times the model was
    prompted (1 = the aborted turn was not re-prompted) and every outage duration
    the ladder measured (a run the retry never ran must measure none).
    """
    state, client = _l1_state(tmp_path)
    slot = _RecordingSlot(key)
    lad.default_ladder().forget(lad.L1_TOOL_CALL, slot.key)
    prompts: list[str] = []

    def _stream(message, *_a, **_kw):
        prompts.append(message)
        if len(prompts) > 1:
            # Only the first turn ends on the refusal; a recovery turn that ran
            # must not recurse into the same arm.
            client.last_infra_error = None

        async def _gen():
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="That call was refused for capacity.")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        return _gen()

    client.stream = MagicMock(side_effect=_stream)
    clear: dict[str, object] = {}

    async def _seam(secs):
        # What the arm saw when it committed to the retry, so the interrupt below
        # is the only thing that moved.
        clear["suppressed"] = chat_runner._should_suppress_requeue(slot)
        clear["stop_gen"] = slot._stop_generation
        clear["followup"] = chat_runner._has_user_queued_followup(slot)
        clear["steers"] = bool(slot._pending_steers)
        clear["infra_retries"] = slot._infra_retries
        deliver(state, slot)

    measured: list[float] = []

    def _histogram(name, value, *_a, **_kw):
        if name == lad.RECOVERY_DURATION_SECS:
            measured.append(value)

    try:
        with patch.object(chat_runner, "_recovery_delay", _seam):
            with patch.object(lad, "emit_histogram", _histogram):
                with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
                    mock_sel.return_value = MagicMock()
                    await _run_chat(state, slot, "hello")
                    if slot.task:
                        await slot.task
    finally:
        lad.default_ladder().forget(lad.L1_TOOL_CALL, slot.key)

    assert clear == {
        "suppressed": False,
        "stop_gen": 0,
        "followup": False,
        "steers": False,
        # The wait is counted against the slot's OWN budget while it runs.
        "infra_retries": 1,
    }, "the arm must have entered the wait with every interrupt signal clear"
    return SimpleNamespace(slot=slot, prompts=len(prompts), measured=measured)


class TestAnInterruptDuringTheL1Backoff:
    """The arm re-reads stop / steer / follow-up AFTER the multi-second wait.

    Checking them only before it means a Stop, steer or follow-up that lands
    during the backoff is ignored and the abandoned turn is re-prompted anyway:
    the interrupt resolves while no prompt is active, and the dispatch-point
    purge covers the promise-only and post-compaction continuations only.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "press",
        [
            # Pressed and already resolved back to idle: invisible to the state
            # check, recorded by the monotonic generation.
            pytest.param(lambda slot: setattr(slot, "_stop_generation", 1), id="resolved"),
            pytest.param(lambda slot: setattr(slot, "_stop_state", "soft"), id="in-flight"),
        ],
    )
    async def test_a_stop_drops_the_requeue(self, tmp_path, press):
        run = await _turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-l1stop", lambda _state, slot: press(slot)
        )

        assert run.slot.recovery_inserts == []
        assert run.prompts == 1, "the stopped turn was re-prompted"
        # The attempt paid for a retry that never ran, so it is handed back —
        # a standing count shortens the next real ladder and reads as
        # "recovering" on the health panel for an idle slot.
        assert run.slot._infra_retries == 0
        assert lad.default_ladder().attempts(lad.L1_TOOL_CALL, run.slot.key) == 0
        # The run is dropped, not closed as recovered: no retry ran, so nothing
        # observed the dependency come back and no outage may be measured.
        assert run.measured == []
        # The "retrying it in Ns" card is persisted; it must not stand as the
        # last word on a retry that never happened.
        assert _notices(run.slot)[-1] == (
            "ℹ️ The capacity retry was cancelled — the turn was stopped, "
            "the call was not retried."
        )

    @pytest.mark.asyncio
    async def test_a_steer_drops_the_requeue(self, tmp_path):
        def _steer(_state, slot):
            slot._pending_steers = ["forget that — read the gateway log instead"]

        run = await _turn_interrupted_during_the_backoff(tmp_path, "chat-1-l1steer", _steer)

        assert run.slot.recovery_inserts == []
        assert run.slot._infra_retries == 0
        assert lad.default_ladder().attempts(lad.L1_TOOL_CALL, run.slot.key) == 0
        assert run.measured == []
        assert _notices(run.slot)[-1].endswith("your message takes over.")

    @pytest.mark.asyncio
    async def test_a_user_followup_drops_the_requeue(self, tmp_path):
        def _followup(state, slot):
            slot.queue_insert(
                0,
                "never mind — summarise what you have",
                meta=containment_meta(state, slot),
            )

        run = await _turn_interrupted_during_the_backoff(tmp_path, "chat-1-l1followup", _followup)

        # The continuation would have been inserted at index 0, ahead of the
        # message the user typed while the slot waited.
        assert run.slot.recovery_inserts == []
        assert run.slot._infra_retries == 0
        assert lad.default_ladder().attempts(lad.L1_TOOL_CALL, run.slot.key) == 0
        assert run.measured == []
        assert _notices(run.slot)[-1].endswith("your message takes over.")

    @pytest.mark.asyncio
    async def test_an_uninterrupted_backoff_still_requeues_the_continuation(self, tmp_path):
        run = await _turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-l1plain", lambda _state, _slot: None
        )

        assert len(run.slot.recovery_inserts) == 1
        assert run.slot.recovery_inserts[0].startswith(REFUSAL_RECOVERY_PREFIX)
        assert run.prompts == 2, "the retry never reached the model"
        assert not any("cancelled" in n for n in _notices(run.slot))
        # The retry landed, so THIS run closes as recovered and the outage is
        # measured — the contrast that makes the interrupted cases above mean
        # something.
        assert len(run.measured) == 1


@pytest.mark.asyncio
async def test_recovery_delay_seam_sleeps_only_for_positive_values(monkeypatch):
    slept: list[float] = []

    async def fake_sleep(secs):
        slept.append(secs)

    monkeypatch.setattr(chat_runner.asyncio, "sleep", fake_sleep)
    await chat_runner._recovery_delay(0.0)
    await chat_runner._recovery_delay(0.05)
    assert slept == [0.05]


def _errors(slot) -> list[str]:
    return [m["content"] for m in slot.messages if m.get("role") == "error"]


def _give_up_rows(slot) -> list[dict]:
    return [
        m
        for m in slot.messages
        if (m.get("meta") or {}).get(TRANSIENT_NOTICE_META_KEY) == TRANSIENT_NOTICE_GIVE_UP
    ]


def _transient_error() -> Exception:
    """A backend 5xx the classifier calls transient, via the structured flag."""
    exc = AcpError("Prompt error: {'message': 'Internal error: API Error: Internal server error'}")
    exc.transient = True
    return exc


async def _transient_turn_interrupted_during_the_backoff(tmp_path, key, deliver, *, post_token):
    """One turn that fails with a transient 5xx, interrupted INSIDE the backoff.

    ``post_token`` picks the arm: False streams nothing before the failure (the
    same-model verbatim-replay arm), True streams a partial first (the one-shot
    CONTINUE arm). ``deliver(state, slot)`` runs in the wait seam, so the arm had
    already decided to retry with every signal clear.
    """
    state, client = _l1_state(tmp_path)
    # Not an L1 turn: the infra verdict must not claim this failure.
    client.last_infra_error = None
    slot = _RecordingSlot(key)
    prompts: list[str] = []

    def _stream(message, *_a, **_kw):
        prompts.append(message)
        first = len(prompts) == 1

        async def _gen():
            if post_token:
                yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the first half of the answer ")
            if first:
                raise _transient_error()
            yield LLMEvent(kind=EVENT_TEXT_CHUNK, text="the rest of the answer")
            yield LLMEvent(kind=EVENT_COMPLETE, stop_reason="end_turn")

        return _gen()

    client.stream = MagicMock(side_effect=_stream)
    clear: dict[str, object] = {}

    async def _seam(secs):
        clear["suppressed"] = chat_runner._should_suppress_requeue(slot)
        clear["stop_gen"] = slot._stop_generation
        clear["followup"] = chat_runner._has_user_queued_followup(slot)
        clear["steers"] = bool(slot._pending_steers)
        clear["multi_second_wait"] = secs >= 2.0
        clear["counted_attempt"] = slot._transient_5xx_retries
        clear["one_shot_spent"] = slot._posttoken_retry_used
        deliver(state, slot)

    with patch.object(chat_runner, "_recovery_delay", _seam):
        with patch("kiro_crew.dashboard.chat.sel") as mock_sel:
            mock_sel.return_value = MagicMock()
            await _run_chat(state, slot, "hello")
            if slot.task:
                await slot.task

    assert clear == {
        "suppressed": False,
        "stop_gen": 0,
        "followup": False,
        "steers": False,
        "multi_second_wait": True,
        # The pre-wait state each arm owes back when it drops the re-queue: the
        # replay arm counted an attempt, the CONTINUE arm has not yet spent its
        # one-shot (it is consumed below the wait, which is the whole point).
        "counted_attempt": 1 if not post_token else 0,
        "one_shot_spent": False,
    }, "the arm must have entered the wait with every interrupt signal clear"
    return SimpleNamespace(slot=slot, prompts=len(prompts))


def _press_stop_resolved(_state, slot):
    """Pressed and already back to idle: only the monotonic counter records it."""
    slot._stop_generation = 1


def _press_stop_in_flight(_state, slot):
    slot._stop_state = "soft"


def _steer(_state, slot):
    slot._pending_steers.append("forget that — read the gateway log instead")


def _followup(state, slot):
    slot.queue_insert(0, "never mind — summarise what you have", meta=containment_meta(state, slot))


_STOPS = [
    pytest.param(_press_stop_resolved, id="resolved"),
    pytest.param(_press_stop_in_flight, id="in-flight"),
]


class TestAStopDuringTheSameModelTransientBackoff:
    """The verbatim-replay arm re-reads the STOP signals after its backoff.

    Its guard runs BEFORE a multi-second sleep, and a Stop pressed during that
    sleep resolves while no prompt is active: the dispatch-point purge only
    covers the promise-only and post-compaction continuations, so nothing else
    drops a replay the user has cancelled.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("press", _STOPS)
    async def test_a_stop_drops_the_replay(self, tmp_path, press):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxstop", press, post_token=False
        )

        assert run.slot.recovery_inserts == []
        assert run.prompts == 1, "the cancelled message was re-prompted anyway"
        # A NO-REQUEUE exit ends the turn, so it owes the same per-turn budget
        # refresh as the landed and terminal arms: an attempt left counted for a
        # retry that never ran shortens the next real ladder and inflates its
        # backoff seed, and a stale walk index suppresses the restore probe.
        assert run.slot._transient_5xx_retries == 0
        assert run.slot._infra_retries == 0
        assert run.slot._fallback_candidate_idx == 0
        assert run.slot._fallback_walked == []
        # The "retrying…" row is persisted and carries the PENDING token; without
        # a correction the card reads "retrying…" forever for a retry that never
        # happened. The give-up token is what returns the Continue affordance.
        assert _errors(run.slot)[-1] == "⟳ Connection unstable — please try again."
        assert len(_give_up_rows(run.slot)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deliver",
        [
            # Deliberately NOT re-read by this arm — see the class below.
            pytest.param(_steer, id="steer"),
            pytest.param(_followup, id="followup"),
        ],
    )
    async def test_an_uninterrupted_backoff_still_replays_the_message(self, tmp_path, deliver):
        """And so does a steer or a follow-up: this arm owes the user the replay."""
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxplain", deliver, post_token=False
        )
        assert run.slot.recovery_inserts == ["hello"]
        assert _give_up_rows(run.slot) == []

    @pytest.mark.asyncio
    async def test_no_interrupt_at_all_still_replays_the_message(self, tmp_path):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxnone", lambda _s, _slot: None, post_token=False
        )
        assert run.slot.recovery_inserts == ["hello"]
        assert run.prompts == 2, "the replay never reached the model"
        assert _give_up_rows(run.slot) == []
        assert not any("please try again" in t for t in _errors(run.slot))


class TestTheReplayArmDoesNotDropOnASteerOrAFollowUp:
    """The exclusion is deliberate, and pinned so a consistency sweep cannot add it.

    This arm runs on ``not _turn_emitted``: no token and no tool call landed, so
    there is nothing for a correction to contradict and the user's request is
    still entirely un-run. Dropping it would erase that request with no output
    anywhere in the transcript. Ordering makes both intents survive instead: the
    replay is inserted at the HEAD, so a follow-up typed during the backoff runs
    after it, and an unconsumed steer is degraded to a head card by
    ``_requeue_unconsumed_steers`` in the same finally, which dequeues FIRST.
    """

    @pytest.mark.asyncio
    async def test_a_followup_is_queued_behind_the_replay_not_instead_of_it(self, tmp_path):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxorder", _followup, post_token=False
        )

        # The replay goes in at the HEAD, ahead of the message typed during the
        # wait, so BOTH intents survive and run in the order the user sent them.
        # This ordering is the reason the arm may leave a follow-up un-checked;
        # were the replay queued behind, dropping it would be the only option.
        assert run.slot.queue_after_recovery == [["hello", "never mind — summarise what you have"]]

    @pytest.mark.asyncio
    async def test_an_unconsumed_steer_dequeues_ahead_of_the_replay(self, tmp_path):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-5xxsteerorder", _steer, post_token=False
        )

        # At insert time the replay is alone at the head; the finally's
        # `_requeue_unconsumed_steers` then degrades the steer to a head card
        # ABOVE it, so the correction runs first and the replay follows.
        assert run.slot.queue_after_recovery == [["hello"]]
        assert run.slot.recovery_inserts == ["hello"]


class TestAnInterruptDuringThePostTokenBackoff:
    """The one-shot CONTINUE arm re-reads the FULL set after its backoff.

    Unlike the replay arm, what it re-queues is a continuation of a turn that
    already streamed: the partial is persisted and on screen before the wait, so
    a follow-up typed during it is the user answering that partial, and the
    continuation is inserted AHEAD of that message. Dropping costs nothing.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deliver",
        _STOPS
        + [
            pytest.param(_steer, id="steer"),
            pytest.param(_followup, id="followup"),
        ],
    )
    async def test_an_interrupt_drops_the_continuation(self, tmp_path, deliver):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-ptstop", deliver, post_token=True
        )

        assert run.slot.recovery_inserts == []
        # The one-shot is NOT spent: an allowance burned on a retry that never
        # ran silently disarms the NEXT genuine turn's only recovery.
        assert run.slot._posttoken_retry_used is False
        # Append-only: the streamed partial stays, and the "resuming…" row is
        # corrected rather than retracted.
        assert any("the first half of the answer" in m["content"] for m in run.slot.messages)
        assert _errors(run.slot)[-1] == "⟳ Connection unstable — please try again."
        assert len(_give_up_rows(run.slot)) == 1

    @pytest.mark.asyncio
    async def test_an_uninterrupted_backoff_still_queues_the_continuation(self, tmp_path):
        run = await _transient_turn_interrupted_during_the_backoff(
            tmp_path, "chat-1-ptnone", lambda _s, _slot: None, post_token=True
        )

        assert len(run.slot.recovery_inserts) == 1
        assert run.slot.recovery_inserts[0] == chat_runner._POSTTOKEN_RECOVER_MSG
        assert run.prompts == 2, "the continuation never reached the model"
        # The one-shot IS spent when a recovery really is enqueued — the contrast
        # that makes the interrupted cases above mean something.
        assert run.slot._posttoken_retry_used is True
        assert _give_up_rows(run.slot) == []
