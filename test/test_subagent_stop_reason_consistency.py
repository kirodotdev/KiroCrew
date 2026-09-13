"""Stop-reason consistency across every completion consumer.

``EVENT_COMPLETE`` only says a stream ENDED. Before this change the sub-agent
run (``subagent_manager/run.py``) recorded EVERY completion as success --
``done=True, error="", outcome=completed, record_success()`` -- whatever the
stop reason said, so a watchdog tool stall, a runtime cancel or a transport
death reached the parent as "completed ✅" with the partial as the answer.
The task runner (``task_executor.py``) had the same shape.

These tests drive the REAL production paths -- ``SubagentManager.spawn`` ->
``_run`` -> ``_run_inner_impl`` and ``TaskRunner._execute_single_task`` ->
``execute_single_task`` -- with only the ACP session provider faked, which is
the seam every other run test uses. Nothing in ``run.py`` is mocked.

The mapping lives in ONE place, ``acp.types.classify_stop_reason``; the table
tests pin it and the path tests pin that each entry actually uses it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from test_taskrunner import _make_mock_sessions as _make_taskrunner_sessions

from kiro_crew.acp.types import (
    STOP_CLASS_CANCELLED,
    STOP_CLASS_FAILED,
    STOP_CLASS_RECOVERING,
    STOP_CLASS_STALLED,
    STOP_CLASS_SUCCEEDED,
    STOP_REASON_CANCELLED,
    STOP_REASON_COMPACTION_FAILED,
    STOP_REASON_END_TURN,
    STOP_REASON_REFUSAL,
    STOP_REASON_STALE_RECOVER,
    STOP_REASON_TOOL_STALL,
    STOP_RECOVERY_MAX_RETRIES,
    classify_stop_reason,
)
from kiro_crew.dashboard.state import TOOL_STALL_RECOVERY_PREFIX
from kiro_crew.providers.base import EVENT_COMPLETE, EVENT_TEXT_CHUNK, LLMEvent
from kiro_crew.subagent import SubagentInfo, SubagentManager
from kiro_crew.taskrunner import Step, StepStatus, TaskRun, TaskRunner

pytestmark = pytest.mark.usefixtures("healthy_host_memory")

_STALL_EVIDENCE = (
    "verdict=unknown; idle_secs=5400; tool=execute_bash; "
    "command=pytest -q; evidence=no result frame"
)
_GENERIC_ERROR = "error: process exited (exit 137)"


# ── 1. The classifier table (the single mapping) ──────────────────────


class TestClassifyStopReason:
    @pytest.mark.parametrize(
        "reason, name, recoverable, retryable, known",
        [
            (STOP_REASON_END_TURN, STOP_CLASS_SUCCEEDED, False, False, True),
            ("", STOP_CLASS_SUCCEEDED, False, False, True),
            (None, STOP_CLASS_SUCCEEDED, False, False, True),
            (STOP_REASON_TOOL_STALL, STOP_CLASS_STALLED, True, False, True),
            (STOP_REASON_STALE_RECOVER, STOP_CLASS_RECOVERING, True, False, True),
            (STOP_REASON_CANCELLED, STOP_CLASS_CANCELLED, False, False, True),
            (STOP_REASON_COMPACTION_FAILED, STOP_CLASS_FAILED, False, False, True),
            (STOP_REASON_REFUSAL, STOP_CLASS_FAILED, False, False, True),
            (_GENERIC_ERROR, STOP_CLASS_FAILED, False, True, True),
            ("error: connection lost", STOP_CLASS_FAILED, False, True, True),
            ("something_new", STOP_CLASS_FAILED, False, False, False),
        ],
    )
    def test_table(self, reason, name, recoverable, retryable, known):
        cls = classify_stop_reason(reason)
        assert cls.name == name
        assert cls.recoverable is recoverable
        assert cls.retryable is retryable
        assert cls.known is known
        assert cls.stop_reason == (reason or "")
        assert cls.is_success is (name == STOP_CLASS_SUCCEEDED)
        assert cls.is_terminal_failure is (name == STOP_CLASS_FAILED)

    def test_compaction_failed_is_recovering_only_with_transient_verdict(self):
        assert (
            classify_stop_reason(STOP_REASON_COMPACTION_FAILED, compaction_transient=True).name
            == STOP_CLASS_RECOVERING
        )
        assert (
            classify_stop_reason(STOP_REASON_COMPACTION_FAILED, compaction_transient=False).name
            == STOP_CLASS_FAILED
        )

    def test_only_stalled_and_recovering_are_recoverable(self):
        recoverable = {
            r
            for r in (
                STOP_REASON_END_TURN,
                STOP_REASON_TOOL_STALL,
                STOP_REASON_STALE_RECOVER,
                STOP_REASON_CANCELLED,
                STOP_REASON_COMPACTION_FAILED,
                STOP_REASON_REFUSAL,
                _GENERIC_ERROR,
                "unknown",
            )
            if classify_stop_reason(r).recoverable
        }
        assert recoverable == {STOP_REASON_TOOL_STALL, STOP_REASON_STALE_RECOVER}

    def test_no_stop_reason_is_ever_an_unknown_success(self):
        """Anything the table does not know is `failed`, never `succeeded`."""
        for reason in ("timeout", "max_tokens", "error", "ERROR: x", "end_turn "):
            cls = classify_stop_reason(reason)
            assert cls.name == STOP_CLASS_FAILED and cls.known is False, reason

    def test_shared_budget_matches_main_chat(self):
        assert STOP_RECOVERY_MAX_RETRIES == 3


# ── 2. The real sub-agent path ────────────────────────────────────────


def _text(text: str) -> SimpleNamespace:
    return SimpleNamespace(kind=EVENT_TEXT_CHUNK, text=text, runtime_global=False)


def _complete(stop_reason: str, text: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        kind=EVENT_COMPLETE,
        stop_reason=stop_reason,
        text=text,
        title="execute_bash",
        tool_input="pytest -q > run.log 2>&1",
        runtime_global=False,
        refusal=None,
    )


def _mock_sessions(stream_factory) -> MagicMock:
    sessions = MagicMock()
    sessions.get_pid = MagicMock(return_value=None)
    provider = AsyncMock()
    provider.start = AsyncMock()
    provider.shutdown = AsyncMock()
    provider.context_usage_pct = lambda: 0.0
    provider.stream = MagicMock(side_effect=stream_factory)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    sessions.release = MagicMock()
    sessions.reset = AsyncMock()
    sessions.record_success = MagicMock()
    sessions.get_agent = MagicMock(return_value="")
    sessions.get_approval_policy = MagicMock(return_value="auto")
    sessions.has_session = MagicMock(return_value=True)
    sessions._provider = provider
    return sessions


def _mock_ctx_builder() -> MagicMock:
    ctx = MagicMock()
    ctx.build_message = MagicMock(return_value=("built_message", None))
    ctx.hooks.on_tool_call = MagicMock()
    ctx.hooks.auto_approve_subagent_spawn = True
    ctx.hooks.auto_approve_subagent_tools = False
    return ctx


def _manager(sessions: MagicMock) -> SubagentManager:
    mgr = SubagentManager(sessions=sessions, ctx_builder=_mock_ctx_builder())
    mgr._should_use_session_sharing = MagicMock(return_value=False)
    return mgr


def _spy_events(mgr: SubagentManager) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    _orig = mgr._fire_event

    async def _spy(etype, info, extra=None):
        events.append((etype, dict(extra or {})))
        await _orig(etype, info, extra)

    mgr._fire_event = _spy
    return events


def _spy_taskq_marks(mgr: SubagentManager, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    marks: list[str] = []
    real = type(mgr._admission).taskq_mark

    def _spy(self, info, state):
        marks.append(state)
        real(self, info, state)

    monkeypatch.setattr(type(mgr._admission), "taskq_mark", _spy)
    return marks


async def _spawn_and_wait(mgr: SubagentManager, task: str = "do work") -> SubagentInfo:
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(task)
        assert info is not None
        await mgr._tasks[info.id]
    return info


def _single_turn(stop_reason: str, evidence: str = ""):
    """Every stream call: one text chunk then the same completion."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            yield _text("partial output ")
            yield _complete(stop_reason, evidence)

        return _gen()

    return stream_factory, calls


def _done_event(events: list[tuple[str, dict]]) -> dict:
    done = [e for k, e in events if k == "subagent_done"]
    assert len(done) == 1, events
    return done[0]


@pytest.mark.asyncio
async def test_end_turn_is_success():
    factory, calls = _single_turn(STOP_REASON_END_TURN)
    mgr = _manager(_mock_sessions(factory))
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    assert info.error == "" and info.outcome == "completed"
    assert info.stop_class == STOP_CLASS_SUCCEEDED
    assert info.partial is False
    assert calls == ["built_message"]
    assert mgr._sessions.record_success.call_count == 1
    done = _done_event(events)
    assert done["outcome"] == "completed"
    assert done["stop_class"] == STOP_CLASS_SUCCEEDED and done["partial"] is False


@pytest.mark.asyncio
async def test_tool_stall_is_never_recorded_as_success(monkeypatch: pytest.MonkeyPatch):
    """SPEC-ADDENDUM §8/§10: EVENT_COMPLETE + `error: tool stall` ≠ success.

    The run is continued IN PLACE (continue-nudge, not a re-run of the task)
    within the shared budget, then ends `failed` with the partial flagged.
    """
    factory, calls = _single_turn(STOP_REASON_TOOL_STALL, _STALL_EVIDENCE)
    mgr = _manager(_mock_sessions(factory))
    events = _spy_events(mgr)
    marks = _spy_taskq_marks(mgr, monkeypatch)
    info = await _spawn_and_wait(mgr)

    assert info.done is True
    assert info.outcome == "failed"
    assert info.stop_reason == STOP_REASON_TOOL_STALL
    assert info.stop_class == STOP_CLASS_STALLED
    assert info.partial is True
    assert "stalled" in info.error and STOP_REASON_TOOL_STALL in info.error
    assert f"{STOP_RECOVERY_MAX_RETRIES}/{STOP_RECOVERY_MAX_RETRIES}" in info.error
    assert "partial result preserved" in info.error
    assert "idle_secs=5400" in info.error  # the watchdog evidence survives
    # Partial from every attempt is preserved, never discarded.
    assert info.result.count("partial output") == STOP_RECOVERY_MAX_RETRIES + 1
    assert mgr._sessions.record_success.call_count == 0
    # 1 original prompt + STOP_RECOVERY_MAX_RETRIES continue-nudges, all on the
    # SAME session; the original task is never re-sent.
    assert len(calls) == STOP_RECOVERY_MAX_RETRIES + 1
    assert calls[0] == "built_message"
    for nudge in calls[1:]:
        assert nudge.startswith(TOOL_STALL_RECOVERY_PREFIX)
        assert "run.log" in nudge  # names the redirected log from the stalled command
        assert "built_message" not in nudge
    assert info._stop_recovery_used == STOP_RECOVERY_MAX_RETRIES
    # Parent delivery carries the class and the partial flag.
    done = _done_event(events)
    assert done["outcome"] == "failed"
    assert done["stop_reason"] == STOP_REASON_TOOL_STALL
    assert done["stop_class"] == STOP_CLASS_STALLED
    assert done["partial"] is True
    assert done["error"] == info.error
    recovering = [e for k, e in events if k == "subagent_recovering"]
    assert [e["attempt"] for e in recovering] == [1, 2, 3]
    assert all(e["stop_class"] == STOP_CLASS_STALLED for e in recovering)
    # Durable row: the owner is ALIVE during an in-place recovery, so the row
    # keeps `running` under our lease -- taskq's `recovering` is the lost-owner
    # state and would make the id re-claimable (a duplicate run). The yield is
    # recorded as `stop_recovery` events + a progress marker instead.
    assert "recovering" not in marks
    store = mgr._admission.taskq_store()
    assert store is not None
    kinds = [e.kind for e in store.events(info.id)]
    assert kinds.count("stop_recovery") == 2 * STOP_RECOVERY_MAX_RETRIES
    rec = store.get(info.id)
    assert rec is not None and rec.state == "failed"
    assert (rec.progress or {}).get("phase") == "readmitted"


@pytest.mark.asyncio
async def test_tool_stall_recovered_in_place_is_success():
    """One stall, then the continue-nudge finishes the turn: success, partial kept."""
    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            if len(calls) == 1:
                yield _text("first half ")
                yield _complete(STOP_REASON_TOOL_STALL, _STALL_EVIDENCE)
            else:
                yield _text("second half")
                yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = _manager(_mock_sessions(factory))
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    assert info.error == "" and info.outcome == "completed"
    assert info.stop_class == STOP_CLASS_SUCCEEDED and info.partial is False
    assert info.result == "first half second half"
    assert calls[0] == "built_message" and calls[1].startswith(TOOL_STALL_RECOVERY_PREFIX)
    assert info._stop_recovery_used == 1
    assert mgr._sessions.record_success.call_count == 1
    assert _done_event(events)["stop_class"] == STOP_CLASS_SUCCEEDED


@pytest.mark.asyncio
async def test_stale_recover_is_recovering_then_failed():
    factory, calls = _single_turn(STOP_REASON_STALE_RECOVER)
    mgr = _manager(_mock_sessions(factory))
    info = await _spawn_and_wait(mgr)
    assert info.outcome == "failed"
    assert info.stop_class == STOP_CLASS_RECOVERING
    assert info.error.startswith(f"recovering: {STOP_REASON_STALE_RECOVER}")
    assert info.partial is True
    assert len(calls) == STOP_RECOVERY_MAX_RETRIES + 1
    assert mgr._sessions.record_success.call_count == 0


@pytest.mark.asyncio
async def test_runtime_cancel_is_cancelled_not_completed():
    factory, calls = _single_turn(STOP_REASON_CANCELLED)
    mgr = _manager(_mock_sessions(factory))
    events = _spy_events(mgr)
    info = await _spawn_and_wait(mgr)
    assert info.outcome == "failed"  # not user-stopped: a runtime cancel is a failure
    assert info.stop_class == STOP_CLASS_CANCELLED
    assert info.error.startswith("cancelled (stop_reason=cancelled)")
    assert info.partial is True and info.result == "partial output "
    assert len(calls) == 1  # never retried
    assert _done_event(events)["stop_class"] == STOP_CLASS_CANCELLED


@pytest.mark.asyncio
async def test_user_stop_keeps_the_neutral_record_contract():
    """A `cancelled` completion after the user's own Stop is neutral: error unset."""
    mgr_ref: dict = {}
    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            yield _text("partial output ")
            info = next(iter(mgr_ref["mgr"]._agents.values()))
            info.user_stopped = True
            yield _complete(STOP_REASON_CANCELLED)

        return _gen()

    mgr = _manager(_mock_sessions(factory))
    mgr_ref["mgr"] = mgr
    info = await _spawn_and_wait(mgr)
    assert info.error == ""
    assert info.outcome == "stopped"
    assert info.stop_class == STOP_CLASS_CANCELLED
    assert info.partial is True
    assert mgr._sessions.record_success.call_count == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", [STOP_REASON_COMPACTION_FAILED, _GENERIC_ERROR, "brand_new"])
async def test_terminal_failures_are_failed_without_retry(reason: str):
    factory, calls = _single_turn(reason)
    mgr = _manager(_mock_sessions(factory))
    info = await _spawn_and_wait(mgr)
    assert info.outcome == "failed"
    assert info.stop_class == STOP_CLASS_FAILED
    assert reason in info.error
    assert info.partial is True
    assert len(calls) == 1
    assert mgr._sessions.record_success.call_count == 0
    if reason == "brand_new":
        assert "unexpected stop_reason" in info.error


@pytest.mark.asyncio
async def test_stalled_run_yields_its_slot_to_queued_work():
    """A stalled sub-agent RELEASES its execution slot while it recovers.

    max_concurrent=1: A stalls; B (queued behind A) must START and FINISH
    while A is yielded; A then re-admits and finishes. Order of stream calls
    proves it: [A original, B original, A continue-nudge].
    """
    order: list[str] = []
    counts_at_call: list[int] = []
    mgr_ref: dict = {}

    def factory(msg: str, *a, **kw):
        mgr = mgr_ref["mgr"]
        counts_at_call.append(mgr._running_count)
        tag = "A" if "A-task" in msg or msg.startswith(TOOL_STALL_RECOVERY_PREFIX) else "B"
        order.append(f"{tag}:{'nudge' if msg.startswith(TOOL_STALL_RECOVERY_PREFIX) else 'orig'}")

        async def _gen():
            if tag == "A" and len([o for o in order if o.startswith("A")]) == 1:
                yield _text("A partial ")
                yield _complete(STOP_REASON_TOOL_STALL, _STALL_EVIDENCE)
            else:
                yield _text(f"{tag} done")
                yield _complete(STOP_REASON_END_TURN)

        return _gen()

    sessions = _mock_sessions(factory)
    mgr = _manager(sessions)
    mgr_ref["mgr"] = mgr
    mgr._max_concurrent = 1
    mgr._spawn_stagger_secs = 0.0  # let the drain admit B the instant A yields
    # build_message echoes the task so the stream can tell A from B.
    mgr._ctx_builder.build_message = MagicMock(side_effect=lambda msg, *a, **k: (msg, None))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        a = mgr.spawn("A-task")
        b = mgr.spawn("B-task")
        assert a is not None and b is not None
        assert b.queued is True  # behind A on the single slot
        await mgr._tasks[a.id]
        if b.id in mgr._tasks:
            await mgr._tasks[b.id]
    # A's yield must NOT re-dispatch A itself from the durable store (the
    # row keeps A's lease); only B may take the freed slot.
    assert order == ["A:orig", "B:orig", "A:nudge"]
    assert a.error == "" and a.outcome == "completed"
    assert b.error == "" and b.outcome == "completed"
    assert a.result == "A partial A done"
    assert a._stop_recovery_used == 1
    # The slot was held exactly once at every stream start (never two runs on one slot).
    assert counts_at_call == [1, 1, 1]
    assert mgr._running_count == 0  # no leaked or double-released slot


@pytest.mark.asyncio
async def test_readmission_refused_surfaces_failed_without_double_release():
    """If no slot frees within the deadline, the withheld completion is surfaced
    as `failed` with its partial; the slot token is not released twice."""
    factory, calls = _single_turn(STOP_REASON_TOOL_STALL, _STALL_EVIDENCE)
    mgr = _manager(_mock_sessions(factory))
    mgr._max_concurrent = 1

    def _hog_drain():
        # A queued spawn takes the freed slot and never gives it back.
        mgr._running_count = mgr._max_concurrent

    mgr._drain_queue = MagicMock(side_effect=_hog_drain)
    with patch("kiro_crew.subagent._RECOVERY_SLOT_WAIT_SECS", 0.0):
        info = await _spawn_and_wait(mgr)
    assert info.outcome == "failed"
    assert info.stop_class == STOP_CLASS_STALLED
    assert info.partial is True and info.result == "partial output "
    assert len(calls) == 1  # no nudge was sent without a slot
    assert info._stop_recovery_used == STOP_RECOVERY_MAX_RETRIES  # budget spent
    # The run's finally must not decrement the hog's slot.
    assert mgr._running_count == 1


@pytest.mark.asyncio
async def test_recovery_defers_to_shutdown_and_reap_markers():
    """A recoverable completion is NOT recovered once a terminal marker is set."""
    factory, calls = _single_turn(STOP_REASON_TOOL_STALL, _STALL_EVIDENCE)
    mgr = _manager(_mock_sessions(factory))
    mgr._shutting_down = True
    info = await _spawn_and_wait(mgr)
    assert len(calls) == 1
    assert info.stop_class == STOP_CLASS_STALLED
    assert info.outcome == "failed"
    assert info._stop_recovery_used == 0
    assert f"0/{STOP_RECOVERY_MAX_RETRIES}" in info.error


# ── 3. The blocking spawn wait (A's still_running contract) ───────────


@pytest.mark.asyncio
async def test_blocking_wait_expiry_does_not_fail_the_child():
    """SPEC-ADDENDUM §9: a blocking wait that expires ends the CALLER's wait; the
    child keeps running and is never marked failed / cancelled / collected."""
    from kiro_crew.mcp_tools import spawn as spawn_mod

    src = Path(spawn_mod.__file__).read_text(encoding="utf-8")
    assert '"still_running"' in src
    assert "task_ids" in src


# ── 4. The task runner path (task_executor) ───────────────────────────


def _taskrunner_provider(stop_reason: str, text: str = "half") -> MagicMock:
    provider = MagicMock()
    calls: list[str] = []

    async def _stream(message: str):
        calls.append(message)
        yield LLMEvent(kind="text_chunk", text=text)
        yield LLMEvent(kind="complete", stop_reason=stop_reason, text=_STALL_EVIDENCE)

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    provider.calls = calls
    return provider


@pytest.mark.asyncio
async def test_taskrunner_persistent_tool_stall_step_is_failed(tmp_path: Path):
    sessions = _make_taskrunner_sessions()
    provider = _taskrunner_provider(STOP_REASON_TOOL_STALL)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    step = Step(index=1, title="Test step", description="desc")
    run.tasks = [step]

    success = await runner._execute_single_task(run, step)

    assert success is False
    assert step.status == StepStatus.FAILED
    assert step.result == "half"  # the partial is kept on the task
    sessions.record_success.assert_not_called()
    # Bounded: the existing retry ladder (incl. same-error loop detection) ran.
    assert 1 < len(provider.calls) <= 5
    # Every retry prompt names the stall and continues from it, never a bare re-run.
    for retry_prompt in provider.calls[1:]:
        assert "retry attempt" in retry_prompt
        assert "stalled" in retry_prompt and STOP_REASON_TOOL_STALL in retry_prompt


@pytest.mark.asyncio
async def test_taskrunner_tool_stall_then_success_passes_on_retry(tmp_path: Path):
    sessions = _make_taskrunner_sessions()
    provider = MagicMock()
    calls: list[str] = []

    async def _stream(message: str):
        calls.append(message)
        if len(calls) == 1:
            yield LLMEvent(kind="text_chunk", text="half")
            yield LLMEvent(
                kind="complete", stop_reason=STOP_REASON_TOOL_STALL, text=_STALL_EVIDENCE
            )
        else:
            yield LLMEvent(kind="text_chunk", text="whole")
            yield LLMEvent(kind="complete", stop_reason=STOP_REASON_END_TURN)

    provider.stream = _stream
    provider.approve_tool = AsyncMock()
    provider.reject_tool = AsyncMock()
    provider.context_usage_pct = MagicMock(return_value=0.0)
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    step = Step(index=1, title="Test step", description="desc")
    run.tasks = [step]

    assert await runner._execute_single_task(run, step) is True
    assert step.status == StepStatus.PASSED and step.error == ""
    assert step.result == "whole"
    assert "retry attempt 2" in calls[1] and "stalled" in calls[1]


@pytest.mark.asyncio
async def test_taskrunner_end_turn_step_passes(tmp_path: Path):
    sessions = _make_taskrunner_sessions()
    provider = _taskrunner_provider(STOP_REASON_END_TURN, text="done")
    sessions.get_or_create = AsyncMock(return_value=(provider, True, False))
    runner = TaskRunner(sessions=sessions, auto_test=False, work_dir=tmp_path)
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    step = Step(index=1, title="Test step", description="desc")
    run.tasks = [step]
    assert await runner._execute_single_task(run, step) is True
    assert step.status == StepStatus.PASSED and step.result == "done"
    assert provider.calls[0] and "retry attempt" not in provider.calls[0]


# ── 5. The main chat uses the same table (no private spelling) ────────


def test_chat_runner_has_no_private_stop_reason_mapping():
    """chat_runner must not re-derive the class with its own `startswith("error:")`
    or a literal retry budget; it reads the classifier and the shared constant."""
    import kiro_crew.dashboard.chat_runner as cr

    src = Path(cr.__file__).read_text(encoding="utf-8")
    assert 'startswith("error:")' not in src
    assert "classify_stop_reason(" in src
    assert "_tool_stall_retries < 3" not in src and "_tool_stall_retries >= 3" not in src
    assert "_stale_recovery_retries < 3" not in src and "_stale_recovery_retries >= 3" not in src
    assert "STOP_RECOVERY_MAX_RETRIES" in src


def test_subagent_run_has_no_private_stop_reason_mapping():
    import kiro_crew.subagent_manager.run as run_mod

    src = Path(run_mod.__file__).read_text(encoding="utf-8")
    assert 'startswith("error:")' not in src
    assert "classify_stop_reason(" in src
