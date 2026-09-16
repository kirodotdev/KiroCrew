"""The background turn loops consume the post-compaction flag.

``session_compaction`` marks ``needs_reinjection`` on a live session after an
in-place compaction dropped its session-start context. The two cron turn loops in
``slack/gateway.py`` (single-agent and ``agent_sequence``) and the task runner's
``execute_task`` loop are their own copies of the turn loop, so each must
read-and-clear the flag itself, forward it to ``build_message``, and put it back
when the turn that consumed it never lands -- the contract the dashboard runner
keeps in its ``finally``.

The cron harness mirrors ``test_cron_acp_retry.py``: a ``GatewayOrchestrator``
built with ``__new__`` and mocked sessions, with the cron callback captured off
``CronService.create``. The task-runner harness mirrors ``test_auto_approve.py``.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Callable
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from kiro_crew.cron import CronJob, CronSchedule
from kiro_crew.providers.base import LLMEvent
from kiro_crew.taskrunner import Step, TaskRun, TaskRunner


@pytest.fixture
def gw_and_cb() -> tuple[Any, Callable[[], Any], Callable[..., Any]]:
    from kiro_crew.slack.gateway import GatewayOrchestrator

    gw = GatewayOrchestrator.__new__(GatewayOrchestrator)
    gw.sessions = MagicMock()
    gw.sessions.get_pid = MagicMock(return_value=None)
    gw.sessions.get_or_create = AsyncMock(return_value=(MagicMock(), False, False))
    gw.sessions.release = MagicMock()
    gw.sessions.reset = AsyncMock()
    # The flag surface under test: armed once, like a session that just compacted.
    gw.sessions.consume_needs_reinjection = MagicMock(side_effect=[True, False, False, False])
    gw.sessions.mark_needs_reinjection = MagicMock()
    gw.ctx_builder = MagicMock()
    gw.ctx_builder.build_message = MagicMock(return_value=("msg", None))
    gw.ctx_builder.hooks = MagicMock()
    gw.slack = None
    gw.conv_log = None
    gw.dashboard_state = None
    gw._owner_id = "U000"
    gw.subagent_mgr = None
    gw._cron_injecting = {}
    gw._no_crons = False
    gw._interactive_approval = MagicMock(return_value="interactive_cb")

    captured_cb: list[Any] = [None]

    def capture_cron(on_job: Any = None, **kw: Any) -> MagicMock:
        captured_cb[0] = on_job
        svc = MagicMock()
        svc.start = AsyncMock()
        return svc

    return gw, lambda: captured_cb[0], capture_cron


def _job(**kw: Any) -> CronJob:
    return CronJob(
        id=kw.pop("id", "j1"),
        name="test",
        message="msg",
        schedule=CronSchedule(kind="every", every_secs=60),
        **kw,
    )


def _run(gw: Any, get_cb: Callable[[], Any], capture_cron: Any, job: CronJob, stream: Any) -> Any:
    with (
        patch("kiro_crew.slack.gateway.stream_and_collect", side_effect=stream),
        patch("kiro_crew.slack.gateway.redact_exfiltration_urls", return_value=("", False)),
        patch("kiro_crew.slack.gateway.redact_credentials", return_value=("", False)),
        patch(
            "kiro_crew.slack.gateway.CronService.create", new=AsyncMock(side_effect=capture_cron)
        ),
    ):

        async def _init_and_run() -> Any:
            await gw._init_cron()
            cb = get_cb()
            assert cb is not None
            return await cb(job)

        return asyncio.run(_init_and_run())


def _reinjection_kwargs(gw: Any) -> list[bool]:
    return [
        call.kwargs["needs_reinjection"] for call in gw.ctx_builder.build_message.call_args_list
    ]


class TestSingleAgentCronTurn:
    def test_a_compacted_session_forwards_the_flag_to_build_message(self, gw_and_cb) -> None:
        gw, get_cb, capture_cron = gw_and_cb

        async def ok(*a: Any, **k: Any) -> str:
            return "done"

        _run(gw, get_cb, capture_cron, _job(), ok)

        gw.sessions.consume_needs_reinjection.assert_called_once_with("cron:j1")
        assert _reinjection_kwargs(gw) == [True]
        # Landed: consumed exactly once, and NOT put back.
        gw.sessions.mark_needs_reinjection.assert_not_called()

    def test_a_session_stand_in_without_the_flag_gets_the_false_default(self, gw_and_cb) -> None:
        gw, get_cb, capture_cron = gw_and_cb
        del gw.sessions.consume_needs_reinjection
        del gw.sessions.mark_needs_reinjection

        async def ok(*a: Any, **k: Any) -> str:
            return "done"

        _run(gw, get_cb, capture_cron, _job(), ok)

        assert _reinjection_kwargs(gw) == [False]

    def test_a_failed_consuming_turn_puts_the_flag_back(self, gw_and_cb) -> None:
        # The flag is cleared BEFORE build_message; a stream error on that very
        # turn discards the prompt carrying the re-injected context. The re-arm
        # runs in the finally, ahead of the session release/reset.
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()

        async def boom(*a: Any, **k: Any) -> str:
            raise RuntimeError("provider fell over")

        with pytest.raises(RuntimeError):
            _run(gw, get_cb, capture_cron, _job(), boom)

        assert _reinjection_kwargs(gw) == [True]
        gw.sessions.mark_needs_reinjection.assert_called_once_with("cron:j1")


class TestAgentSequenceCronTurn:
    def test_each_agent_turn_reads_its_own_key(self, gw_and_cb) -> None:
        gw, get_cb, capture_cron = gw_and_cb

        async def ok(*a: Any, **k: Any) -> str:
            return "done"

        _run(gw, get_cb, capture_cron, _job(agent_sequence=["alpha", "beta"]), ok)

        consumed = [c.args[0] for c in gw.sessions.consume_needs_reinjection.call_args_list]
        assert consumed == ["cron:j1:alpha", "cron:j1:beta"]
        # Only alpha's session had compacted (the fixture arms the flag once).
        assert _reinjection_kwargs(gw) == [True, False]
        gw.sessions.mark_needs_reinjection.assert_not_called()

    def test_a_failed_consuming_agent_turn_puts_the_flag_back(self, gw_and_cb) -> None:
        gw, get_cb, capture_cron = gw_and_cb
        gw.dashboard_state = MagicMock()

        async def boom(*a: Any, **k: Any) -> str:
            raise RuntimeError("provider fell over")

        with pytest.raises(RuntimeError):
            _run(gw, get_cb, capture_cron, _job(agent_sequence=["alpha", "beta"]), boom)

        assert _reinjection_kwargs(gw) == [True]
        gw.sessions.mark_needs_reinjection.assert_called_once_with("cron:j1:alpha")


# ── task runner ──────────────────────────────────────────────────────────────


def _task_sessions() -> MagicMock:
    s = MagicMock()
    s._lock = asyncio.Lock()
    s._sessions = {}
    s.get_or_create = AsyncMock()

    async def _open_task_session(_pk, session_key, *, agent=None, cwd=None, approval_policy=""):
        return await s.get_or_create(session_key, agent=agent, cwd=cwd)

    s.open_task_session = _open_task_session
    s.release = MagicMock()
    s.reset = AsyncMock()
    s.record_success = MagicMock()
    s.record_failure = AsyncMock()
    s.check_context_usage = MagicMock()
    s.compact_if_needed = AsyncMock(return_value="below_threshold")
    # The flag surface under test: armed once, like a session that just
    # compacted; every later read (retry attempts) finds it clear.
    armed = [True]

    def _consume(key: str) -> bool:
        was = armed[0]
        armed[0] = False
        return was

    s.consume_needs_reinjection = MagicMock(side_effect=_consume)
    s.mark_needs_reinjection = MagicMock()
    return s


def _task_provider(*, fail: bool = False, stop_reason: str = "end_turn") -> MagicMock:
    provider = MagicMock()

    async def _stream(msg: str):
        if fail:
            raise RuntimeError("provider fell over")
        yield LLMEvent(kind="text_chunk", text="done")
        yield LLMEvent(kind="complete", stop_reason=stop_reason)

    provider.stream = _stream
    provider.context_usage_pct = MagicMock(return_value=0.0)
    return provider


def _task_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.conversation_log.get_metadata_status.return_value = ({}, True)
    ctx.build_message = MagicMock(return_value=("prompt", {}))
    return ctx


async def _run_one_task(sessions: MagicMock, ctx: MagicMock, tmp_path: Path) -> None:
    runner = TaskRunner(sessions=sessions, context_builder=ctx, auto_test=False, work_dir=tmp_path)
    run = TaskRun(spec_path=str(tmp_path / "t.md"), spec_content="s", status="running")
    step = Step(index=1, title="Write", description="d")
    run.tasks = [step]
    with patch("kiro_crew.task_executor.self_review", return_value=True):
        await runner._execute_single_task(run, step)


def _task_reinjection_kwargs(ctx: MagicMock) -> list[bool]:
    return [call.kwargs["needs_reinjection"] for call in ctx.build_message.call_args_list]


class TestTaskRunnerTurn:
    """``check_context`` compacts the task session in place, so its loop consumes too."""

    @pytest.mark.asyncio
    async def test_a_compacted_session_forwards_the_flag_to_build_message(self, tmp_path):
        sessions = _task_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_task_provider(), False, False))
        ctx = _task_ctx()

        await _run_one_task(sessions, ctx, tmp_path)

        assert _task_reinjection_kwargs(ctx)[0] is True
        consumed = sessions.consume_needs_reinjection.call_args_list[0].args[0]
        assert consumed == ctx.build_message.call_args_list[0].args[2], "same key as the turn"
        # Landed: consumed exactly once, and NOT put back.
        sessions.mark_needs_reinjection.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_session_stand_in_without_the_flag_gets_the_false_default(self, tmp_path):
        sessions = _task_sessions()
        del sessions.consume_needs_reinjection
        del sessions.mark_needs_reinjection
        sessions.get_or_create = AsyncMock(return_value=(_task_provider(), False, False))
        ctx = _task_ctx()

        await _run_one_task(sessions, ctx, tmp_path)

        assert _task_reinjection_kwargs(ctx)[0] is False
        sessions.record_success.assert_called()

    @pytest.mark.asyncio
    async def test_a_failed_consuming_attempt_puts_the_flag_back(self, tmp_path):
        # The flag is cleared BEFORE build_message; a provider error on that
        # attempt discards the prompt carrying the re-injected context. The
        # re-arm runs in the attempt's finally, so the retry re-injects.
        sessions = _task_sessions()
        sessions.get_or_create = AsyncMock(return_value=(_task_provider(fail=True), False, False))
        ctx = _task_ctx()

        await _run_one_task(sessions, ctx, tmp_path)

        kwargs = _task_reinjection_kwargs(ctx)
        assert kwargs[0] is True, "the first attempt consumed the flag"
        sessions.record_failure.assert_awaited()
        sessions.record_success.assert_not_called()
        # Re-armed once per failed consuming attempt: the first one. Later
        # attempts consumed False (the mock flag is single-shot), so they do not
        # re-arm -- a plain failure must not invent a re-injection.
        assert sessions.mark_needs_reinjection.call_count == 1

    @pytest.mark.asyncio
    async def test_a_cancelled_consuming_attempt_puts_the_flag_back(self, tmp_path):
        # A cancel completes the stream normally with stop_reason "cancelled",
        # and the backend drops that turn from its transcript -- the re-injected
        # context goes with it, so the flag must come back like a raised turn.
        sessions = _task_sessions()
        sessions.get_or_create = AsyncMock(
            return_value=(_task_provider(stop_reason="cancelled"), False, False)
        )
        ctx = _task_ctx()

        await _run_one_task(sessions, ctx, tmp_path)

        assert _task_reinjection_kwargs(ctx)[0] is True
        sessions.mark_needs_reinjection.assert_called_once()
