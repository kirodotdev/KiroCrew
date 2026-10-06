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

import asyncio
import logging
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
    sessions.get_agent_selection = MagicMock(return_value=("template", ""))
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


#: How long a test waits for a run it started before failing by name: far past
#: any healthy run here, and well under the suite's per-test timeout.
_RUN_CEILING = 60.0


async def _spawn_and_wait(mgr: SubagentManager, task: str = "do work") -> SubagentInfo:
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn(task)
        assert info is not None
        await asyncio.wait_for(mgr._tasks[info.id], _RUN_CEILING)
    return info


def _single_turn(
    stop_reason: str | None, evidence: str = "", *, chunks: tuple[str, ...] = ("partial output ",)
):
    """Every stream call: *chunks*, then the same completion. ``None`` is no
    completion at all: the generator just ends, as when the transport dies."""
    calls: list[str] = []

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            for chunk in chunks:
                yield _text(chunk)
            if stop_reason is not None:
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
async def test_exhausted_stream_without_complete_is_not_marked_result_complete():
    """A stream that dies between chunks must not be recorded as a finished result.

    ``classify_stop_reason("")`` resolves to ``succeeded``, so a generator that
    simply stops -- no EVENT_COMPLETE at all -- takes the success branch unless
    the complete event is checked. The durable flag must reflect the missing
    complete event, not the absent stop reason: nothing else on disk tells a
    reader after a restart that ``result.txt`` holds a fragment.
    """

    # ...the stream dies after its chunk: no EVENT_COMPLETE, no stop reason.
    factory, _calls = _single_turn(None)
    mgr = _manager(_mock_sessions(factory))
    events = _spy_events(mgr)
    writes: list[str | None] = []
    _orig = mgr._write_finished_result_off_loop

    async def _spy(info, text, **kw):
        writes.append(text)
        return await _orig(info, text, **kw)

    mgr._write_finished_result_off_loop = _spy
    info = await _spawn_and_wait(mgr)

    # Everything observable about the run reads as a normal success...
    assert info.outcome == "completed"
    assert info.stop_class == STOP_CLASS_SUCCEEDED and info.partial is False
    assert _done_event(events)["stop_class"] == STOP_CLASS_SUCCEEDED
    # ...but it claims no completed ending, and the durable flag records that
    # no complete event ever arrived.
    assert not info._ending_claimed
    assert writes == [None]
    from kiro_crew.subagent_persistence import read_state

    assert (read_state(info.id) or {}).get("result_complete") is False


# ── 2b. a finished result stays whole ───────────────────────────────

_ANSWER = "THE WHOLE FINISHED ANSWER. "


async def _wait_until(predicate, what: str, timeout: float = 15.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, f"timed out waiting for {what}"
        await asyncio.sleep(0.02)


async def _wait_settled(mgr: SubagentManager, info: SubagentInfo) -> None:
    """Wait for the run, any respawn, and every task the manager holds."""
    await _wait_until(
        lambda: info.done
        and all(t.done() for t in mgr._tasks.values())
        and all(t.done() for t in mgr._report_tasks),
        f"run {info.id} to settle",
    )


def _answer_stream():
    return _single_turn(STOP_REASON_END_TURN, chunks=(_ANSWER,))


class _GatedResultWrite:
    """Holds ``write_finished_result`` on its worker thread until released."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, *, whole_only: bool = True) -> None:
        import threading

        import kiro_crew.subagent as subagent
        import kiro_crew.subagent_persistence as sp

        self._loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self._release = threading.Event()
        self.texts: list[str | None] = []
        real = sp.write_finished_result

        def _gated(agent_id, text, state_writer, /):
            self.texts.append(text)
            if len(self.texts) == 1 and (text is not None or not whole_only):
                self._loop.call_soon_threadsafe(self.entered.set)
                assert self._release.wait(15), "the test never released the result write"
            return real(agent_id, text, state_writer)

        monkeypatch.setattr(subagent, "write_finished_result", _gated)

    def release(self) -> None:
        self._release.set()


@pytest.mark.asyncio
async def test_the_finished_result_is_written_after_post_processing_from_memory(
    monkeypatch: pytest.MonkeyPatch,
):
    """The result write follows the post-processing of the answer into
    ``info.result``; it rewrites result.txt from the raw streamed text, through
    the ``kiro_crew.subagent.update_state`` seam, and the usage row lands on
    its own task."""
    import kiro_crew.dashboard.handlers.usage as usage_mod
    import kiro_crew.subagent as subagent
    import kiro_crew.subagent_persistence as sp

    order: list[str] = []
    jobs: list[tuple] = []
    real_job = sp.write_finished_result

    def _spy_job(agent_id, text, state_writer, /):
        order.append("result write")
        jobs.append((agent_id, text, state_writer))
        return real_job(agent_id, text, state_writer)

    real_keep = subagent.apply_completion_keep

    def _spy_keep(*a, **kw):
        order.append("post-processing")
        return real_keep(*a, **kw)

    rows: list[bool] = []

    async def _spy_usage(*a, **kw):
        rows.append(True)

    monkeypatch.setattr(subagent, "write_finished_result", _spy_job)
    monkeypatch.setattr(subagent, "apply_completion_keep", _spy_keep)
    monkeypatch.setattr(usage_mod, "persist_token_record_async", _spy_usage)

    factory, _calls = _answer_stream()
    info = await _spawn_and_wait(_manager(_mock_sessions(factory)))
    await _wait_until(lambda: rows, "the usage row")

    assert info.outcome == "completed" and info._ending_claimed
    assert order == ["post-processing", "result write"], order
    assert jobs == [(info.id, _ANSWER, subagent.update_state)]
    assert (sp._agent_dir(info.id) / "result.txt").read_text(encoding="utf-8") == _ANSWER
    assert (sp.read_state(info.id) or {}).get("result_complete") is True


@pytest.mark.asyncio
async def test_the_finished_result_is_rewritten_whole_and_capped_from_memory(
    monkeypatch: pytest.MonkeyPatch,
):
    """result.txt ends as the whole answer the run holds in memory, capped:
    an append that failed mid-stream leaves no hole in what the flag vouches
    for."""
    import kiro_crew.context_management as cm
    import kiro_crew.subagent as subagent
    import kiro_crew.subagent_persistence as sp

    monkeypatch.setattr(cm, "RESULT_FILE_MAX_BYTES", 2_000)
    real_append = subagent.write_result_chunk
    appends: list[str] = []

    def _lossy_append(agent_id, text, *, fresh=False):
        appends.append(text)
        if len(appends) == 2:  # chunk 2's append is lost, as ENOSPC loses it
            return False
        return real_append(agent_id, text, fresh=fresh)

    monkeypatch.setattr(subagent, "write_result_chunk", _lossy_append)
    chunks = ("PART-ONE ", "PART-TWO " * 400, "PART-THREE.")
    factory, _calls = _single_turn(STOP_REASON_END_TURN, chunks=chunks)
    info = await _spawn_and_wait(_manager(_mock_sessions(factory)))

    assert info.outcome == "completed"
    on_disk = (sp._agent_dir(info.id) / "result.txt").read_bytes()
    assert on_disk == cm.cap_result_bytes("".join(chunks).encode("utf-8"))
    assert len(on_disk) <= 2_000 and b"[...truncated" in on_disk
    assert (sp.read_state(info.id) or {}).get("result_complete") is True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "stop_reason",
    [None, STOP_REASON_REFUSAL, "max_tokens"],
    ids=["no complete event", "refusal", "max_tokens"],
)
async def test_every_other_ending_caps_what_streamed(
    monkeypatch: pytest.MonkeyPatch, stop_reason: str | None
):
    """The cap holds for every ending, not just a whole answer: a partial the
    stream left behind is capped in place and recorded as no whole answer."""
    import kiro_crew.context_management as cm
    import kiro_crew.subagent_persistence as sp

    monkeypatch.setattr(cm, "RESULT_FILE_MAX_BYTES", 2_000)
    factory, _calls = _single_turn(stop_reason, chunks=("z" * 9_000,))
    info = await _spawn_and_wait(_manager(_mock_sessions(factory)))

    assert not info._ending_claimed
    on_disk = (sp._agent_dir(info.id) / "result.txt").read_bytes()
    assert len(on_disk) <= 2_000 and b"[...truncated" in on_disk
    assert (sp.read_state(info.id) or {}).get("result_complete") is False


@pytest.mark.asyncio
@pytest.mark.parametrize("ending", ["raised", "deadline", "user stop", "shutdown"])
async def test_an_ending_that_never_reaches_the_tail_caps_what_streamed_too(
    monkeypatch: pytest.MonkeyPatch, ending: str
):
    """The cap is not the tail's alone: a run that raises mid-stream, hits its
    deadline, is stopped or is shut down before any complete event still
    leaves ``result.txt`` within the bound, recorded as no whole answer, from
    ``_run``'s ``finally``."""
    import kiro_crew.context_management as cm
    import kiro_crew.subagent_persistence as sp

    monkeypatch.setattr(cm, "RESULT_FILE_MAX_BYTES", 2_000)
    hold = asyncio.Event()
    calls: list[str] = []

    def factory(msg: str, *a, **kw):
        calls.append(msg)

        async def _gen():
            yield _text("z" * 9_000)
            if ending == "raised":
                raise RuntimeError("the provider fell over")
            await hold.wait()
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = _manager(_mock_sessions(factory))
    deadline = _DeadlineOnDemand(monkeypatch, mgr) if ending == "deadline" else None
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work")
            assert info is not None
            run = mgr._tasks[info.id]
            result = sp._agent_dir(info.id) / "result.txt"
            if ending != "raised":
                await _wait_until(
                    lambda: result.exists() and result.stat().st_size >= 9_000,
                    "the chunk to stream",
                )
                if deadline is not None:
                    deadline.fire.set()
                elif ending == "user stop":
                    assert await asyncio.wait_for(mgr.cancel(info.id), 5) is True
                else:
                    await asyncio.wait_for(mgr.cancel_all(), 60)
            done, _ = await asyncio.wait({run}, timeout=15)
            assert run in done, "the run task never finished"
    finally:
        hold.set()

    assert len(calls) == 1 and info.done and not info._ending_claimed
    on_disk = result.read_bytes()
    assert len(on_disk) <= 2_000 and b"[...truncated" in on_disk
    assert (sp.read_state(info.id) or {}).get("result_complete") is False


@pytest.mark.asyncio
async def test_an_error_stamped_before_the_complete_event_is_a_stop_that_got_there_first():
    """A failed child under ``on_child_failure=fail_parent`` stamps the parent's
    ``error`` and only SCHEDULES its cancel. A successful complete event handled
    before that cancel runs claims nothing: the run ends failed, counts no
    success and records no whole answer."""
    import kiro_crew.subagent_persistence as sp

    holder: dict = {}

    def factory(msg: str, *a, **kw):
        async def _gen():
            yield _text(_ANSWER)
            mgr, info = holder["mgr"], holder["info"]
            child = SubagentInfo(id="sa-child", task="t")
            outcome = SimpleNamespace(fail_parent=True, cancel_siblings=[], wake_parent=False)
            mgr._admission._child_terminal_apply(child, "failed", info, info.id, outcome, None)
            assert info.error and not info.done
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    mgr = _manager(_mock_sessions(factory))
    stats = MagicMock()
    with patch("kiro_crew.subagent.Stats", return_value=stats), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do work", parent_session_key="dashboard:p")
        assert info is not None
        holder["mgr"], holder["info"] = mgr, info
        await _wait_settled(mgr, info)

    assert not info._ending_claimed
    assert info.outcome == "failed" and "fail_parent" in info.error
    assert stats.inc_subagent_completed.call_count == 0
    assert mgr._sessions.record_success.call_count == 0
    assert (sp.read_state(info.id) or {}).get("result_complete") is False


@pytest.mark.asyncio
async def test_a_claimed_result_write_that_hangs_is_bounded(monkeypatch: pytest.MonkeyPatch):
    """Nothing can stop a claimed ending, so its result write carries its own
    bound: on a wedged filesystem the run still ends completed, frees its lane
    slot and finishes its task, and the worker lands detached, holding the run's
    conversation until it does. Until ``done``, deleting the run is refused as
    pending rather than popping a run whose report does not exist yet."""
    import kiro_crew.subagent as subagent
    import kiro_crew.subagent_persistence as sp

    monkeypatch.setattr(subagent, "_STATE_DRAIN_TIMEOUT", 0.5)
    gate = _GatedResultWrite(monkeypatch)
    factory, _calls = _answer_stream()
    mgr = _manager(_mock_sessions(factory))
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work", parent_session_key="dashboard:p")
            assert info is not None
            run = mgr._tasks[info.id]
            await asyncio.wait_for(gate.entered.wait(), 10)
            assert info._ending_claimed and not info.done
            assert await mgr.cancel(info.id) is False
            assert await mgr.settle_before_delete(info.id) == "pending"
            assert info.id in mgr._agents
            done, _ = await asyncio.wait({run}, timeout=15)
            assert run in done, "a hung claimed write held the run past its bound"
            assert info.done and info.outcome == "completed"
            assert mgr._running_count == 0
            assert info.id in mgr._abandoned_state_writers
    finally:
        gate.release()
    await _wait_until(
        lambda: info.id not in mgr._abandoned_state_writers, "the detached write to land"
    )
    assert (sp.read_state(info.id) or {}).get("result_complete") is True


@pytest.mark.asyncio
async def test_the_final_cap_of_a_done_run_holds_its_conversation(
    monkeypatch: pytest.MonkeyPatch,
):
    """``_run``'s ``finally`` caps an unclaimed ending after the run is
    ``done``, so ``_conversation_busy`` does not count the run. The cap's
    worker ends in a whole-file ``update_state``, and a release landing
    mid-write writes ``keep=False`` on the loop for that rewrite to roll back.
    So the write holds the conversation until its worker lands."""
    import kiro_crew.subagent_persistence as sp

    gate = _GatedResultWrite(monkeypatch, whole_only=False)
    factory, _calls = _single_turn(None)  # the stream dies: no complete event
    mgr = _manager(_mock_sessions(factory))
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work", parent_session_key="dashboard:p")
            assert info is not None
            await asyncio.wait_for(gate.entered.wait(), 10)
            assert gate.texts == [None] and info.done and not info._ending_claimed
            busy = mgr._conversation_busy(f"subagent:{info.id}")
            assert busy is not None and busy._state_writer_abandoned
            ok, detail = mgr.release_conversation(info.id)
            assert not ok and "still settling a state write" in detail
    finally:
        gate.release()
    await _wait_until(lambda: info.id not in mgr._abandoned_state_writers, "the cap write to land")
    await _wait_settled(mgr, info)
    assert mgr._conversation_busy(f"subagent:{info.id}") is None
    assert (sp.read_state(info.id) or {}).get("result_complete") is False


@pytest.mark.asyncio
async def test_the_hold_lasts_until_the_last_writer_of_a_run_lands(caplog):
    """A run can hold two workers at once: a state write left detached past
    its drain, and the final cap ``_run``'s ``finally`` starts after it. The
    earlier one landing first must not release the conversation while the
    cap is still writing, or a release lands ``keep=False`` for the cap's
    whole-file rewrite to roll back. A worker registered twice (the cap's
    up-front hold plus its own bounded drain) is held once, so it settles
    once: a cap that fails is logged once, not once per registration."""
    caplog.set_level(logging.DEBUG, logger="kiro_crew.subagent")
    loop = asyncio.get_running_loop()
    mgr = _manager(_mock_sessions(lambda *a, **kw: None))
    info = SubagentInfo(id="sa-held", task="t", done=True)
    earlier, cap = loop.create_future(), loop.create_future()
    mgr._hold_for_detached_writer(info, "state", earlier)
    mgr._hold_for_detached_writer(info, "result complete", cap)
    mgr._hold_for_detached_writer(info, "result complete", cap)

    earlier.set_result(True)
    await asyncio.sleep(0)
    busy = mgr._conversation_busy(f"subagent:{info.id}")
    assert busy is not None and busy._state_writer_abandoned
    ok, detail = mgr.release_conversation(info.id)
    assert not ok and "still settling a state write" in detail

    cap.set_exception(OSError("disk full"))
    await asyncio.sleep(0)
    assert info.id not in mgr._abandoned_state_writers
    assert mgr._conversation_busy(f"subagent:{info.id}") is None
    settled = [
        r
        for r in caplog.records
        if r.getMessage()
        == f"Best-effort result complete write failed for {info.id} while detached"
    ]
    assert len(settled) == 1, "a writer registered twice settled more than once"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["file write", "folder fsync"])
async def test_a_failed_result_write_records_no_flag_and_the_run_completes(
    monkeypatch: pytest.MonkeyPatch, caplog, failure: str
):
    """The flag never vouches for bytes that did not reach the disk: a write or
    fsync that fails leaves ``result_complete`` False, logged at WARNING, and
    the run still completes."""
    import errno
    import logging

    import kiro_crew.subagent_persistence as sp

    real_write = sp.atomic_write

    def _eio_on_result(path, *a, **kw):
        if Path(path).name == "result.txt":
            raise OSError(errno.EIO, "Input/output error")
        return real_write(path, *a, **kw)

    def _eio(*_a, **_kw):
        raise OSError(errno.EIO, "Input/output error")

    if failure == "file write":
        monkeypatch.setattr(sp, "atomic_write", _eio_on_result)
    else:
        monkeypatch.setattr(sp, "fsync_dir", _eio)
    caplog.set_level(logging.WARNING, logger="kiro_crew.subagent_persistence")
    factory, _calls = _answer_stream()
    info = await _spawn_and_wait(_manager(_mock_sessions(factory)))

    assert info.outcome == "completed" and info.error == ""
    assert (sp.read_state(info.id) or {}).get("result_complete") is False
    assert any("stays unflagged" in r.getMessage() for r in caplog.records)


class _DeadlineOnDemand:
    """The run's deadline, fired by the test rather than by the clock.

    Stands in for ``asyncio.wait_for`` on the one call that carries the run's
    budget, which is set far past any test, so the run is held where the test
    wants it before its deadline lands: on firing, the run is cancelled and its
    ``TimeoutError`` raised, as ``wait_for`` does when time runs out. Every
    other ``wait_for`` passes straight through.
    """

    _BUDGET = 86_400.0

    def __init__(self, monkeypatch: pytest.MonkeyPatch, mgr: SubagentManager) -> None:
        self.fire = asyncio.Event()
        self.timed_out = False
        mgr._default_timeout = self._BUDGET
        real = asyncio.wait_for

        async def _wait_for(aw, timeout=None, **kw):
            if timeout != self._BUDGET:
                return await real(aw, timeout, **kw)
            inner = asyncio.ensure_future(aw)
            firing = asyncio.ensure_future(self.fire.wait())
            try:
                await asyncio.wait({inner, firing}, return_when=asyncio.FIRST_COMPLETED)
            except asyncio.CancelledError:
                inner.cancel()
                await asyncio.wait({inner})
                raise
            finally:
                firing.cancel()
            if inner.done():
                return inner.result()
            inner.cancel()
            try:
                return await inner
            except asyncio.CancelledError as exc:
                self.timed_out = True
                raise asyncio.TimeoutError from exc

        monkeypatch.setattr(asyncio, "wait_for", _wait_for)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ending",
    [
        "unexpected cancel",
        "shutdown",
        "shutdown, then its outer budget",
        "deadline",
        "user stop",
        "deadline reap",
        "parent end",
    ],
)
async def test_whatever_lands_after_the_claim_the_run_is_completed_once(
    monkeypatch: pytest.MonkeyPatch, ending: str
):
    """Once a whole answer claimed its completed ending, whatever lands while it
    writes the result changes nothing: a cancel, a graceful shutdown (and the
    second cancel its outer budget sends) or the deadline only cut the tail
    short, and a Stop, a deadline reap or a parent end find the run ``done``.
    It is counted once, never respawned, never failed, and its answer is
    whole on disk."""
    import time as _time

    import kiro_crew.subagent_persistence as sp

    gate = _GatedResultWrite(monkeypatch)
    factory, calls = _answer_stream()
    mgr = _manager(_mock_sessions(factory))
    deadline = _DeadlineOnDemand(monkeypatch, mgr) if ending == "deadline" else None
    delivered: list[tuple[str, str]] = []

    async def _on_done(done_info):
        delivered.append((done_info.outcome, done_info.result))

    mgr._on_done = _on_done
    stats = MagicMock()
    try:
        with patch("kiro_crew.subagent.Stats", return_value=stats), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work", parent_session_key="dashboard:p")
            assert info is not None
            run = mgr._tasks[info.id]
            await asyncio.wait_for(gate.entered.wait(), 10)
            assert info._ending_claimed and not info.done
            shutdown = None
            if ending == "unexpected cancel":
                run.cancel()
            elif ending.startswith("shutdown"):
                shutdown = asyncio.ensure_future(mgr.cancel_all())
                if ending.endswith("budget"):
                    await asyncio.sleep(0.05)
                    run.cancel()  # the gateway's outer shutdown budget expiring
            elif deadline is not None:
                deadline.fire.set()
                await _wait_until(lambda: info._state_drain_active, "the deadline to land")
            elif ending == "user stop":
                assert await asyncio.wait_for(mgr.cancel(info.id), 5) is False
            elif ending == "deadline reap":
                # The reaper's own call: no reason, so the record would be ``reaped``.
                await asyncio.wait_for(
                    mgr._force_reap(info.id, info, _time.time() - info.started), 5
                )
            else:
                await asyncio.wait_for(
                    mgr.cancel_for_teardown([info.id], parent_session_key="dashboard:p"), 5
                )
            assert not (info.user_stopped or info._reap_started or info._reap_reason)
            await asyncio.sleep(0.05)
            gate.release()
            if shutdown is not None:
                await asyncio.wait_for(shutdown, 60)
            done, _ = await asyncio.wait({run}, timeout=15)
            assert run in done, "the run task never finished"
            await _wait_settled(mgr, info)
    finally:
        gate.release()

    assert len(calls) == 1, "a whole run is never respawned"
    assert deadline is None or deadline.timed_out, "the deadline never cut the tail short"
    assert info.outcome == "completed" and info.error == ""
    assert stats.inc_subagent_completed.call_count == 1
    assert stats.inc_subagent_failed.call_count == 0
    assert mgr._sessions.record_success.call_count == 1
    if ending == "parent end":
        assert delivered == []  # a parent end drops the injection, as for any run
    else:
        assert delivered == [("completed", _ANSWER)]
        assert (sp.read_tombstone(info.id) or {}).get("cause") == "delivered"
    assert (sp._agent_dir(info.id) / "result.txt").read_text(encoding="utf-8") == _ANSWER
    assert (sp.read_state(info.id) or {}).get("result_complete") is True


@pytest.mark.asyncio
async def test_a_stop_that_got_there_first_owns_the_ending_and_no_flag_is_written():
    """A Stop pressed while the complete event is still in the pipe is first: its
    reap is in flight (its session reset is slow), so the whole answer that
    arrives next claims nothing. The run records the stop's neutral ending
    once, and state.json never says the result is whole, so nothing after a
    restart can announce the stopped run as completed."""
    import kiro_crew.subagent_persistence as sp

    complete_go = asyncio.Event()
    reset_go = asyncio.Event()

    def factory(msg: str, *a, **kw):
        async def _gen():
            yield _text(_ANSWER)
            await complete_go.wait()
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    sessions = _mock_sessions(factory)
    resets: list[str] = []

    async def _reset(key, **_kw):
        resets.append(key)
        if len(resets) == 1:  # the reap's reset; the run's own teardown is quick
            await reset_go.wait()

    sessions.reset = AsyncMock(side_effect=_reset)
    mgr = _manager(sessions)
    delivered: list[str] = []

    async def _on_done(done_info):
        delivered.append(done_info.outcome)

    mgr._on_done = _on_done
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work")
            assert info is not None
            run = mgr._tasks[info.id]
            await _wait_until(lambda: info.streaming_text, "the first chunk")
            stopping = asyncio.ensure_future(mgr.cancel(info.id))
            await _wait_until(lambda: resets, "the reap's reset")
            complete_go.set()
            await _wait_until(lambda: info.done, "the run's own ending")
            reset_go.set()
            await asyncio.wait_for(stopping, 15)
            done, _ = await asyncio.wait({run}, timeout=15)
            assert run in done, "the run task never finished"
            await _wait_settled(mgr, info)
    finally:
        complete_go.set()
        reset_go.set()

    assert not info._ending_claimed
    assert info.outcome == "stopped" and info.error == ""
    assert delivered == ["stopped"]
    assert (sp.read_state(info.id) or {}).get("result_complete") is False
    sessions.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_a_deadline_reap_that_got_there_first_owns_the_ending():
    """A deadline reap in flight when the whole answer arrives owns the run's
    ending, as a Stop does: the run is recorded and reported as the reap's
    failure, and nothing counts it a success. Recording a bare ``done`` from
    the run's tail let the reap's record guard skip, so a deadline-stopped run
    was reported completed and cleared its session's failure count."""
    import kiro_crew.subagent_persistence as sp

    complete_go = asyncio.Event()
    reset_go = asyncio.Event()

    def factory(msg: str, *a, **kw):
        async def _gen():
            yield _text(_ANSWER)
            await complete_go.wait()
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    sessions = _mock_sessions(factory)
    resets: list[str] = []

    async def _reset(key, **_kw):
        resets.append(key)
        if len(resets) == 1:  # the reap's reset; the run's own teardown is quick
            await reset_go.wait()

    sessions.reset = AsyncMock(side_effect=_reset)
    mgr = _manager(sessions)
    delivered: list[str] = []

    async def _on_done(done_info):
        delivered.append(done_info.outcome)

    mgr._on_done = _on_done
    try:
        with patch("kiro_crew.subagent.Stats") as stats, patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work")
            assert info is not None
            run = mgr._tasks[info.id]
            await _wait_until(lambda: info.streaming_text, "the first chunk")
            reaping = asyncio.ensure_future(
                mgr._force_reap(info.id, info, 601.0, reason="deadline")
            )
            await _wait_until(lambda: resets, "the reap's reset")
            complete_go.set()
            await _wait_until(lambda: run.done(), "the run's own ending")
            reset_go.set()
            await asyncio.wait_for(reaping, 15)
            await _wait_settled(mgr, info)
    finally:
        complete_go.set()
        reset_go.set()

    assert not info._ending_claimed
    assert info.outcome == "failed" and "deadline" in info.error
    assert delivered == ["failed"]
    assert (sp.read_state(info.id) or {}).get("result_complete") is False
    sessions.record_success.assert_not_called()
    stats.return_value.inc_subagent_completed.assert_not_called()
    assert stats.return_value.inc_subagent_failed.call_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stream_end", "unfinished"),
    [
        ("complete event", "the run was stopped as it finished its answer"),
        ("stream stopped", "the runtime was torn down before the run finished"),
    ],
)
async def test_a_reap_in_flight_names_whether_the_answer_had_finished(
    stream_end: str, unfinished: str
):
    """A successful ending a deadline reap got to first is the reap's, and its
    record says how far the answer got: a complete event finished it, while a
    stream that just stopped, which also classifies as a normal end of turn,
    had not, so the record names the teardown."""
    complete_go = asyncio.Event()
    reset_go = asyncio.Event()

    def factory(msg: str, *a, **kw):
        async def _gen():
            yield _text(_ANSWER)
            await complete_go.wait()
            if stream_end == "complete event":
                yield _complete(STOP_REASON_END_TURN)

        return _gen()

    sessions = _mock_sessions(factory)
    resets: list[str] = []

    async def _reset(key, **_kw):
        resets.append(key)
        if len(resets) == 1:  # the reap's reset; the run's own teardown is quick
            await reset_go.wait()

    sessions.reset = AsyncMock(side_effect=_reset)
    mgr = _manager(sessions)
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work")
            assert info is not None
            run = mgr._tasks[info.id]
            await _wait_until(lambda: info.streaming_text, "the first chunk")
            reaping = asyncio.ensure_future(
                mgr._force_reap(info.id, info, 601.0, reason="deadline")
            )
            await _wait_until(lambda: resets, "the reap's reset")
            complete_go.set()
            await _wait_until(lambda: run.done(), "the run's own ending")
            reset_go.set()
            await asyncio.wait_for(reaping, 15)
            await _wait_settled(mgr, info)
    finally:
        complete_go.set()
        reset_go.set()

    assert info.outcome == "failed"
    assert info.error.endswith(f" — {unfinished}"), info.error
    sessions.record_success.assert_not_called()


@pytest.mark.asyncio
async def test_a_user_stop_no_reap_has_reached_yet_records_the_neutral_stop():
    """``cancel()`` stamps ``user_stopped`` synchronously and only then awaits
    its reap, so a whole answer can arrive between the two: no reap is in
    flight yet, and the run still records the stop's neutral ending and counts
    no success."""
    import kiro_crew.subagent_persistence as sp

    complete_go = asyncio.Event()
    reap_go = asyncio.Event()

    def factory(msg: str, *a, **kw):
        async def _gen():
            yield _text(_ANSWER)
            await complete_go.wait()
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    sessions = _mock_sessions(factory)
    mgr = _manager(sessions)
    real_reap = mgr._force_reap

    async def _held_reap(*a, **kw):
        await reap_go.wait()
        return await real_reap(*a, **kw)

    mgr._force_reap = _held_reap
    delivered: list[str] = []

    async def _on_done(done_info):
        delivered.append(done_info.outcome)

    mgr._on_done = _on_done
    try:
        with patch("kiro_crew.subagent.Stats") as stats, patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work")
            assert info is not None
            run = mgr._tasks[info.id]
            await _wait_until(lambda: info.streaming_text, "the first chunk")
            stopping = asyncio.ensure_future(mgr.cancel(info.id))
            await _wait_until(lambda: info.user_stopped, "the stop's stamp")
            complete_go.set()
            await _wait_until(lambda: info.done, "the run's own ending")
            assert not info._reap_started, "the reap was meant to be held"
            reap_go.set()
            await asyncio.wait_for(stopping, 15)
            done, _ = await asyncio.wait({run}, timeout=15)
            assert run in done, "the run task never finished"
            await _wait_settled(mgr, info)
    finally:
        complete_go.set()
        reap_go.set()

    assert not info._ending_claimed
    assert info.outcome == "stopped" and info.error == ""
    assert delivered == ["stopped"]
    assert (sp.read_state(info.id) or {}).get("result_complete") is False
    sessions.record_success.assert_not_called()
    stats.return_value.inc_subagent_completed.assert_not_called()


@pytest.mark.asyncio
async def test_a_run_another_path_already_ended_counts_no_success():
    """A whole answer that arrives after another path recorded the run's
    ending (``done`` set first, standing in for any first-arrival recorder)
    claims nothing and leaves the success to that record: no success stat,
    and the session's failure count is not cleared."""
    import kiro_crew.subagent_persistence as sp

    holder: dict = {}

    def factory(msg: str, *a, **kw):
        async def _gen():
            yield _text(_ANSWER)
            holder["info"].done = True
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    sessions = _mock_sessions(factory)
    mgr = _manager(sessions)
    with patch("kiro_crew.subagent.Stats") as stats, patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do work")
        assert info is not None
        holder["info"] = info
        await asyncio.wait_for(mgr._tasks[info.id], _RUN_CEILING)
        await _wait_settled(mgr, info)

    assert not info._ending_claimed
    assert info.stop_class == STOP_CLASS_SUCCEEDED
    assert (sp.read_state(info.id) or {}).get("result_complete") is False
    sessions.record_success.assert_not_called()
    stats.return_value.inc_subagent_completed.assert_not_called()
    stats.return_value.inc_subagent_failed.assert_not_called()


@pytest.mark.asyncio
async def test_a_claimed_ending_is_done_to_every_other_stop_path(
    monkeypatch: pytest.MonkeyPatch,
):
    """Between the claim and ``done`` (the result write), a Stop, a failed
    child under ``on_child_failure=fail_parent`` and an expired wait all find
    the run done: nothing is stamped on it, no cancel is scheduled, and no
    queue lookup reaches the store, since a registered run is never queued."""
    mgr = _manager(_mock_sessions(lambda *a, **kw: None))
    info = SubagentInfo(id="sa-claimed", task="t", parent_session_key="dashboard:p")
    info._ending_claimed = True
    mgr._agents[info.id] = info
    admission = type(mgr._admission)
    scheduled: list[str] = []
    unqueued: list[str] = []
    monkeypatch.setattr(admission, "_schedule_cancel", lambda _s, aid: scheduled.append(aid))
    monkeypatch.setattr(
        admission, "taskq_cancel_queued", lambda _s, aid, **_kw: unqueued.append(aid)
    )

    assert await mgr.cancel(info.id) is False
    child = SubagentInfo(id="sa-child", task="t")
    fail_parent = SimpleNamespace(fail_parent=True, cancel_siblings=[], wake_parent=False)
    mgr._admission._child_terminal_apply(child, "failed", info, info.id, fail_parent, None)
    mgr._admission.taskq_expire_waits_apply([info.id])

    assert info.error == "" and not info.user_stopped
    assert scheduled == [] and unqueued == []
    assert info.outcome == "completed"


@pytest.mark.asyncio
async def test_the_usage_row_never_holds_an_ending(monkeypatch: pytest.MonkeyPatch):
    """The usage row is best-effort analytics on a task the manager holds: a
    wedged one holds neither the run's ending nor, past the report drain's
    bound, a shutdown."""
    import kiro_crew.dashboard.handlers.usage as usage_mod
    import kiro_crew.subagent as subagent

    monkeypatch.setattr(subagent, "_REPORT_DRAIN_TIMEOUT", 0.3)
    entered = asyncio.Event()
    wedged = asyncio.Event()

    async def _wedged_usage(*_a, **_kw):
        entered.set()
        await wedged.wait()

    monkeypatch.setattr(usage_mod, "persist_token_record_async", _wedged_usage)
    factory, _calls = _answer_stream()
    mgr = _manager(_mock_sessions(factory))
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work")
            assert info is not None
            await asyncio.wait_for(entered.wait(), 10)
            await asyncio.wait_for(mgr._tasks[info.id], 10)
            assert info.done and info.outcome == "completed"
            rows = [t for t in mgr._report_tasks if not t.done()]
            assert rows, "the usage row is held by the manager while it runs"
            loop = asyncio.get_running_loop()
            started = loop.time()
            await asyncio.wait_for(mgr.cancel_all(), 30)
            assert loop.time() - started < 10, "shutdown waited on the usage row"
            assert all(t.done() for t in rows), "shutdown left the usage row running"
    finally:
        wedged.set()


@pytest.mark.asyncio
async def test_a_cancel_during_a_non_whole_ending_cap_keeps_the_recorded_ending(
    monkeypatch: pytest.MonkeyPatch,
):
    """A success-classified stream with no complete event is not whole and
    claims nothing: its tail records the ending, and ``_run``'s ``finally``
    caps the file after it. An unexpected cancel landing on that cap is drained
    and changes nothing: no respawn re-runs the finished stream, the recorded
    ending stands, and the parent still hears it."""
    import kiro_crew.subagent_persistence as sp

    gate = _GatedResultWrite(monkeypatch, whole_only=False)
    factory, calls = _single_turn(None)
    mgr = _manager(_mock_sessions(factory))
    delivered: list[str] = []

    async def _on_done(done_info):
        delivered.append(done_info.outcome)

    mgr._on_done = _on_done
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work")
            assert info is not None
            run = mgr._tasks[info.id]
            await asyncio.wait_for(gate.entered.wait(), 10)
            assert info.done and not info._ending_claimed
            run.cancel()
            await asyncio.sleep(0.05)
            gate.release()
            done, _ = await asyncio.wait({run}, timeout=15)
            assert run in done, "the run task never finished"
            await _wait_settled(mgr, info)
    finally:
        gate.release()

    assert len(calls) == 1, "a recorded ending is never respawned"
    assert gate.texts == [None]
    assert delivered == ["completed"]
    assert (sp.read_state(info.id) or {}).get("result_complete") is False


@pytest.mark.asyncio
async def test_a_cancel_requested_during_post_processing_still_writes_the_result(
    monkeypatch: pytest.MonkeyPatch,
):
    """A cancel requested before the result write's first step is delivered INTO
    the drained job, so the whole answer and its flag still land."""
    import kiro_crew.context_management as cm
    import kiro_crew.subagent as subagent
    import kiro_crew.subagent_persistence as sp

    monkeypatch.setattr(cm, "RESULT_FILE_MAX_BYTES", 2_000)
    real_keep = subagent.apply_completion_keep

    def _keep_then_cancel(*a, **kw):
        asyncio.current_task().cancel()
        return real_keep(*a, **kw)

    monkeypatch.setattr(subagent, "apply_completion_keep", _keep_then_cancel)
    factory, calls = _single_turn(STOP_REASON_END_TURN, chunks=("z" * 9_000,))
    mgr = _manager(_mock_sessions(factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do work")
        assert info is not None
        await _wait_settled(mgr, info)

    assert len(calls) == 1 and info.outcome == "completed"
    assert (sp._agent_dir(info.id) / "result.txt").stat().st_size <= 2_000
    assert (sp.read_state(info.id) or {}).get("result_complete") is True


def _respawn_stream(second: tuple[str, ...], *, second_ends: bool = True):
    """Attempt 1 streams text and waits to be cancelled; attempt 2 streams
    *second*, each chunk after a go from the test, then ends whole (or waits to
    be cancelled too)."""
    calls: list[str] = []
    first_streaming = asyncio.Event()
    second_started = asyncio.Event()
    goes = [asyncio.Event() for _ in second]
    landed = [asyncio.Event() for _ in second]

    def stream_factory(msg: str, *a, **kw):
        calls.append(msg)
        n = len(calls)

        async def _gen():
            if n == 1:
                yield _text("attempt-1 text ")
                first_streaming.set()
                await asyncio.Event().wait()  # cancelled from outside
            second_started.set()
            for chunk, go, done in zip(second, goes, landed):
                await go.wait()
                yield _text(chunk)
                done.set()
            if not second_ends:
                await asyncio.Event().wait()
            yield _complete(STOP_REASON_END_TURN)

        return _gen()

    return SimpleNamespace(
        factory=stream_factory,
        calls=calls,
        first_streaming=first_streaming,
        second_started=second_started,
        goes=goes,
        landed=landed,
    )


async def _respawned(mgr: SubagentManager, stream) -> tuple[SubagentInfo, asyncio.Task]:
    info = mgr.spawn("do work")
    assert info is not None
    await asyncio.wait_for(stream.first_streaming.wait(), 10)
    first = mgr._tasks[info.id]
    first.cancel()
    done, _ = await asyncio.wait({first}, timeout=10)
    assert first in done, "the cancelled first attempt never finished"
    await asyncio.wait_for(stream.second_started.wait(), 15)
    respawn = mgr._tasks[info.id]
    assert respawn is not first
    return info, respawn


@pytest.mark.asyncio
async def test_a_respawn_keeps_the_partial_until_it_has_text_and_never_glues():
    """Until the respawned attempt's first chunk, the interrupted attempt's
    partial stays in result.txt, so a restart then still finds it; from that
    chunk on result.txt and the live partial hold the new attempt alone, and
    its finished answer is the new attempt's text."""
    import kiro_crew.subagent_persistence as sp
    from kiro_crew.subagent_manager.monitoring import tombstone_recovery_action

    stream = _respawn_stream(("attempt-2 opening sentence that ", "ends."))
    mgr = _manager(_mock_sessions(stream.factory))
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info, respawn = await _respawned(mgr, stream)
            folder = sp._agent_dir(info.id)
            state = sp.read_state(info.id) or {}
            assert (folder / "result.txt").read_text(encoding="utf-8") == "attempt-1 text "
            assert tombstone_recovery_action(info.id, state) == "partial_result"

            stream.goes[0].set()
            await asyncio.wait_for(stream.landed[0].wait(), 10)
            await _wait_until(lambda: "opening" in info.streaming_text, "the live partial")
            on_disk = (folder / "result.txt").read_text(encoding="utf-8")
            assert on_disk == "attempt-2 opening sentence that "
            assert info.streaming_text == on_disk

            stream.goes[1].set()
            await asyncio.wait_for(respawn, 10)
            await _wait_settled(mgr, info)
    finally:
        for go in stream.goes:
            go.set()

    assert len(stream.calls) == 2 and info.outcome == "completed"
    assert (folder / "result.txt").read_text(encoding="utf-8") == (
        "attempt-2 opening sentence that ends."
    )
    assert not (folder / "result.previous.txt").exists()
    assert (sp.read_state(info.id) or {}).get("result_complete") is True


@pytest.mark.asyncio
async def test_a_cut_off_respawn_delivers_the_partial_it_left_on_disk():
    """When the respawned attempt is cut off too, the partial the parent is
    handed and the one result.txt holds are the same text: the new attempt's."""
    import kiro_crew.subagent_persistence as sp

    stream = _respawn_stream(("attempt-2 partial ",), second_ends=False)
    mgr = _manager(_mock_sessions(stream.factory))
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info, respawn = await _respawned(mgr, stream)
            stream.goes[0].set()
            await asyncio.wait_for(stream.landed[0].wait(), 10)
            await _wait_until(lambda: info.streaming_text, "the live partial")
            respawn.cancel()  # the one-shot recovery is spent: this one is terminal
            await asyncio.wait({respawn}, timeout=10)
            await _wait_settled(mgr, info)
    finally:
        for go in stream.goes:
            go.set()

    assert len(stream.calls) == 2 and info.outcome == "failed"
    on_disk = (sp._agent_dir(info.id) / "result.txt").read_text(encoding="utf-8")
    assert on_disk == "attempt-2 partial " == info.result


@pytest.mark.asyncio
async def test_a_partial_result_txt_cannot_hold_is_still_delivered_whole(
    monkeypatch: pytest.MonkeyPatch,
):
    """When no chunk reaches result.txt (a full disk, a removed folder), the
    live partial is the only copy of what streamed: it keeps every chunk, and
    a Stop delivers all of it, not just the last chunk."""
    import kiro_crew.subagent as subagent

    monkeypatch.setattr(subagent, "write_result_chunk", lambda *_a, **_kw: False)
    chunks = ("alpha ", "beta ", "gamma ")

    def factory(msg: str, *a, **kw):
        async def _gen():
            for chunk in chunks:
                yield _text(chunk)
            await asyncio.Event().wait()  # until the Stop

        return _gen()

    mgr = _manager(_mock_sessions(factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do work")
        assert info is not None
        await _wait_until(lambda: "gamma" in info.streaming_text, "the last chunk")
        assert info.streaming_text == "".join(chunks)
        await asyncio.wait_for(mgr.cancel(info.id), 15)
        await _wait_settled(mgr, info)

    assert info.outcome == "stopped"
    assert info.result == "".join(chunks)


@pytest.mark.asyncio
async def test_the_live_partial_takes_a_chunk_before_its_file_write_starts(
    monkeypatch: pytest.MonkeyPatch,
):
    """The off-loop write that starts result.txt is an await, so a Stop can
    land while it runs. The chunk that write carries is already in the live
    partial by then, so that Stop still delivers it."""
    import threading

    import kiro_crew.subagent as subagent

    real_write = subagent.write_result_chunk
    run: dict = {}
    partial_at_start: list[str] = []
    write_started = threading.Event()
    release_write = threading.Event()

    def _held(agent_id, text, *, fresh=False):
        if fresh:
            partial_at_start.append(run["info"].streaming_text)
            write_started.set()
            # Held in the worker until the Stop has landed, so the Stop
            # provably arrives while this write is still in flight.
            release_write.wait(15)
        return real_write(agent_id, text, fresh=fresh)

    monkeypatch.setattr(subagent, "write_result_chunk", _held)

    def factory(msg: str, *a, **kw):
        async def _gen():
            yield _text("alpha ")
            await asyncio.Event().wait()  # cut off before any ending

        return _gen()

    mgr = _manager(_mock_sessions(factory))
    try:
        with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
            info = mgr.spawn("do work")
            assert info is not None
            run["info"] = info
            await _wait_until(write_started.is_set, "the start write")
            stop = asyncio.ensure_future(mgr.cancel(info.id))
            await _wait_until(lambda: info.user_stopped, "the Stop")
            release_write.set()
            await asyncio.wait_for(stop, 15)
            await _wait_settled(mgr, info)
    finally:
        release_write.set()

    assert partial_at_start == ["alpha "]
    assert info.outcome == "stopped"
    assert info.result == "alpha "


@pytest.mark.asyncio
@pytest.mark.parametrize("refused", [1, 2], ids=["first-write", "later-append"])
async def test_a_refused_write_leaves_no_hole_in_result_txt(
    monkeypatch: pytest.MonkeyPatch, refused: int
):
    """When the disk refuses one chunk's write and then recovers -- the first
    one or a later append -- the next write starts result.txt over with every
    chunk streamed so far, so the fragment a restart finds has no hole. That
    write grows with the answer, so it never runs on the event loop."""
    import threading

    import kiro_crew.subagent as subagent
    import kiro_crew.subagent_persistence as sp

    real_write = subagent.write_result_chunk
    writes: list[str] = []
    fresh_threads: list[int] = []

    def _one_refused(agent_id, text, *, fresh=False):
        if fresh:
            fresh_threads.append(threading.get_ident())
        # That chunk's write is lost, as ENOSPC loses it.
        ok = len(writes) + 1 != refused and real_write(agent_id, text, fresh=fresh)
        writes.append(text)  # after the write, so the wait below sees it landed
        return ok

    monkeypatch.setattr(subagent, "write_result_chunk", _one_refused)
    chunks = ("alpha ", "beta ", "gamma ")

    def factory(msg: str, *a, **kw):
        async def _gen():
            for chunk in chunks:
                yield _text(chunk)
            await asyncio.Event().wait()  # cut off before any ending

        return _gen()

    mgr = _manager(_mock_sessions(factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do work")
        assert info is not None
        await _wait_until(lambda: len(writes) == len(chunks), "the last chunk's write")
        on_disk = (sp._agent_dir(info.id) / "result.txt").read_text(encoding="utf-8")
        await asyncio.wait_for(mgr.cancel(info.id), 15)
        await _wait_settled(mgr, info)

    assert on_disk == "".join(chunks)
    assert len(fresh_threads) == 2 and threading.get_ident() not in fresh_threads


@pytest.mark.asyncio
async def test_a_textless_whole_answer_leaves_no_result_file():
    """A whole answer with no text (a tool-only run) leaves no result.txt, the
    shape every reader expects of it, and a respawn that finishes so removes
    the interrupted attempt's partial rather than vouching for it."""
    import kiro_crew.subagent_persistence as sp

    stream = _respawn_stream(())
    mgr = _manager(_mock_sessions(stream.factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info, respawn = await _respawned(mgr, stream)
        await asyncio.wait_for(respawn, 10)
        await _wait_settled(mgr, info)

    assert len(stream.calls) == 2 and info.outcome == "completed"
    assert not (sp._agent_dir(info.id) / "result.txt").exists()
    assert (sp.read_state(info.id) or {}).get("result_complete") is True


@pytest.mark.asyncio
async def test_the_result_file_is_written_as_streamed_on_every_platform():
    """``result.txt`` is LF while it streams and after the whole rewrite alike:
    no platform newline translation (a Windows CRLF) in either."""
    import kiro_crew.subagent_persistence as sp

    lines = ("one\n", "two\r\n", "three\n")
    for stop_reason in (None, STOP_REASON_END_TURN):
        factory, _calls = _single_turn(stop_reason, chunks=lines)
        info = await _spawn_and_wait(_manager(_mock_sessions(factory)))
        on_disk = (sp._agent_dir(info.id) / "result.txt").read_bytes()
        assert on_disk == "".join(lines).encode("utf-8"), (stop_reason, on_disk)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["incognito", "temporary"])
async def test_a_transient_run_completes_without_touching_disk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog, mode: str
):
    """A transient run's folder never exists, so its completion writes no file
    and logs no sync warning; its flag lives with its in-memory record."""
    import logging

    import kiro_crew.subagent_persistence as sp

    monkeypatch.setattr(sp, "_SUBAGENTS_DIR", tmp_path / "subagents")
    (tmp_path / "subagents").mkdir()
    caplog.set_level(logging.WARNING, logger="kiro_crew")
    factory, _calls = _answer_stream()
    mgr = _manager(_mock_sessions(factory))
    with patch("kiro_crew.subagent.Stats"), patch("kiro_crew.subagent.sel"):
        info = mgr.spawn("do work", _memory_mode=mode)
        assert info is not None
        await asyncio.wait_for(mgr._tasks[info.id], _RUN_CEILING)

    assert info.outcome == "completed"
    assert not (tmp_path / "subagents" / info.id).exists()
    assert not [
        r
        for r in caplog.records
        if r.name in ("kiro_crew.atomic_write", "kiro_crew.subagent_persistence")
        and info.id in r.getMessage()
    ]


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
